"""The operator command, ``build-limits-recover``.

The command asks the supervisor to stop the managed sccache service under the
restart barrier, verify the service is stopped and empty, and then release the
tokens that were waiting on it. It never releases a token itself: the existing
drain probe remains the only authority that does, and it still demands that
verified stopped-and-empty snapshot.

Its exit status is meant to be tested by scripts, so the meaning of each value
is fixed:

- ``0`` the transaction finished, and no live cache work retains a token;
- ``1`` the transaction is still running, is blocked, or ended with a fault;
- ``2`` the supervisor could not be asked, so its transaction state is unknown.
  This covers both a supervisor that could not be reached to open the
  transaction and one that became unreachable while ``--wait`` was polling it;
  neither observation may be reported as a completed recovery.

Notes
-----
Recovery is not a way to release tokens held by a *running* compiler, and the
supervisor refuses to start while one exists. Stopping sccache by hand does not
release a token either: only the supervisor's own accounting does.
"""

import argparse
import dataclasses
import json
import sys
import time
import typing as typ

from buildlimits import recover_client, status_client

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# Completion is the transaction ending with the retained-work fault cleared,
# so a wait polls the supervisor's own document rather than trusting elapsed
# time. The bound keeps an unattended invocation from polling for ever.
WAIT_SECONDS = 300.0
POLL_SECONDS = 2.0


@dataclasses.dataclass(frozen=True, slots=True)
class Waiting:
    """The clock and suspension a ``--wait`` loop advances through.

    Sleeping and reading the clock are one concern: a wait that advances time
    without suspending, or suspends without advancing, is a test artefact
    rather than a shorter wait. Grouping them keeps a fake wait honest, since a
    caller supplies both or neither.

    Attributes
    ----------
    sleep : Callable[[float], None]
        Suspends between polls.
    monotonic : Callable[[], float]
        Reads the clock that bounds the wait. It is injected with ``sleep``
        because a wait bounded by an ambient clock cannot be tested: a fake
        sleep would still race a real deadline, so a timeout case could only be
        exercised by waiting the full default out.
    """

    sleep: cabc.Callable[[float], None] = time.sleep
    monotonic: cabc.Callable[[], float] = time.monotonic


def main(
    argv: cabc.Sequence[str] | None = None,
    request: cabc.Callable[[], dict[str, object]] = recover_client.request,
    query: cabc.Callable[[], dict[str, object]] = status_client.query,
    waiting: Waiting | None = None,
) -> int:
    """Ask the supervisor to recover, optionally waiting for the outcome.

    Parameters
    ----------
    argv : Sequence[str] | None
        Command-line arguments; ``sys.argv[1:]`` when ``None``.
    request : Callable[[], dict[str, object]]
        Opens one recovery transaction and returns its first description.
    query : Callable[[], dict[str, object]]
        Fetches the status document, used by ``--wait``.
    waiting : Waiting | None
        The clock and sleep used by ``--wait``; ``Waiting()`` when ``None``.

    Returns
    -------
    int
        0 when recovery completed and no live cache work retains a token, 1
        when it is still running, blocked or ended on a fault, and 2 when the
        supervisor could not be asked.

    Examples
    --------
    Every case below answers from the same idle transaction and differs in one
    field, which is the shape a caller tests the exit status against:

    >>> idle = {"phase": "idle", "blocked": "", "last_fault": None}

    An idle transaction has nothing left to do, so it exits zero:

    >>> main([], lambda: idle)
    cache recovery idle
    0

    A transaction that runs to completion while the live-work fault persists
    still reports failure, because the tokens are still retained:

    >>> main([], lambda: idle | {"retained": True})
    cache recovery idle; tokens are held until the cache is verified stopped and empty
    1

    A refusal to start is not an intervention, so it also exits non-zero:

    >>> blocked = idle | {"blocked": "2 compiler group(s) are still running"}
    >>> main([], lambda: blocked)
    build-limits-recover: recovery cannot start: 2 compiler group(s) are still running
    1

    A transaction whose restart attempts were exhausted also exits non-zero,
    even though it is idle and its tokens were released:

    >>> faulted = idle | {"last_fault": "cannot start cache.service: Job failed"}
    >>> main([], lambda: faulted)
    cache recovery idle; last fault: cannot start cache.service: Job failed
    1
    """
    options = _parse(argv)
    try:
        document = request()
        if options.wait:
            document = _await(document, options, query, waiting or Waiting())
    except recover_client.RecoverQueryError as error:
        print(f"build-limits-recover: {error}", file=sys.stderr)
        return 2
    except status_client.StatusQueryError as error:
        # A supervisor that dies or restarts mid-wait must not be reported as a
        # finished transaction: nothing was observed, let alone completed.
        print(f"build-limits-recover: {error}", file=sys.stderr)
        return 2
    return _report(document, options)


def _parse(argv: cabc.Sequence[str] | None) -> argparse.Namespace:
    """Parse the command line, exiting with usage on a bad invocation.

    Returns
    -------
    argparse.Namespace
        The parsed options, with ``--wait-timeout`` defaulted.
    """
    parser = argparse.ArgumentParser(prog="build-limits-recover")
    parser.add_argument("--json", action="store_true", help="print the raw document")
    parser.add_argument(
        "--wait", action="store_true", help="wait until the transaction finishes"
    )
    parser.add_argument(
        "--wait-timeout",
        type=float,
        default=WAIT_SECONDS,
        help="seconds to wait with --wait before reporting the current phase",
    )
    return parser.parse_args(argv)


def _await(
    document: dict[str, object],
    options: argparse.Namespace,
    query: cabc.Callable[[], dict[str, object]],
    waiting: Waiting,
) -> dict[str, object]:
    """Poll the supervisor until the transaction ends or the wait expires.

    Returns
    -------
    dict[str, object]
        The transaction's last observed description.
    """
    deadline = waiting.monotonic() + options.wait_timeout
    while _running(document) and waiting.monotonic() < deadline:
        waiting.sleep(POLL_SECONDS)
        document = _phase(query)
    return document


def _running(document: cabc.Mapping[str, object]) -> bool:
    """Report whether a transaction is still in flight.

    Returns
    -------
    bool
        Whether the transaction has left the idle phase.
    """
    return document.get("phase") not in {None, "idle"}


def _unfinished(document: cabc.Mapping[str, object]) -> bool:
    """Report whether cache work is still unaccounted for.

    Returns
    -------
    bool
        Whether the transaction is in flight, could not start, or ran to
        completion without the live-work fault clearing. A blocked request
        belongs here even though its phase is idle: refusing to start is the
        opposite of intervening. So does a terminal fault: an idle transaction
        carrying a last fault exhausted its restart or barrier-removal
        attempts, which its phase alone no longer shows.
    """
    return (
        _running(document)
        or bool(document.get("blocked"))
        or document.get("last_fault") is not None
    )


def _phase(query: cabc.Callable[[], dict[str, object]]) -> dict[str, object]:
    """Read the transaction's current description from the status document.

    Returns
    -------
    dict[str, object]
        The transaction document, or a stand-in when the supervisor reported
        none. A supervisor that cannot be asked is not a stand-in: an
        unreachable supervisor says nothing about the transaction's outcome, so
        it is raised for the caller to answer with the documented exit status.
    """
    state = query()
    transaction = state.get("cache_recovery")
    if not isinstance(transaction, dict):
        return {"phase": "idle", "blocked": "", "last_fault": None}
    # The drain fault outlives the transaction, so it is what says whether the
    # recovered tokens were actually released or are still being retained.
    return transaction | {"retained": state.get("drain_fault_kind") == "live_work"}


def _report(document: cabc.Mapping[str, object], options: argparse.Namespace) -> int:
    """Print the transaction and choose the exit status.

    Returns
    -------
    int
        0 when no live cache work retains a token, 1 otherwise.
    """
    if options.json:
        print(json.dumps(dict(document), indent=2))
    elif blocked := document.get("blocked"):
        print(f"build-limits-recover: recovery cannot start: {blocked}")
    else:
        print(_summary(document))
    return 1 if _unfinished(document) or document.get("retained") else 0


def _summary(document: cabc.Mapping[str, object]) -> str:
    """Describe the transaction in one line.

    Returns
    -------
    str
        The phase, its retry, and the fault that stopped its last action. An
        idle transaction reports no retry, because there is nothing left for
        it to attempt.
    """
    phase = document.get("phase")
    if phase == "idle":
        line = "cache recovery idle"
    else:
        line = (
            f"cache recovery {phase}: attempts {document.get('attempts', 0)}, "
            f"next attempt in {document.get('retry_in', 0)}s"
        )
    if fault := document.get("last_fault"):
        line += f"; last fault: {fault}"
    if document.get("retained"):
        line += "; tokens are held until the cache is verified stopped and empty"
    return line
