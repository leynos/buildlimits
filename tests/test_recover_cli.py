"""The operator command's exit contract, as a script can depend on it.

``build-limits-recover`` fixes the meaning of its exit status so an operator's
wait loop can test the outcome. The outcomes an idle supervisor reports are
covered by the Doctests in :mod:`~buildlimits.recover_cli`; what is exercised
here is ``--wait``, whose poll can outlive the supervisor it is asking. A wait
that reports a finished recovery nobody observed is worse than a loud failure,
so the document or the failure the poll ends on decides the status.
"""

import json
import typing as typ

import pytest

from buildlimits import recover_cli
from buildlimits.recover_client import RecoverQueryError
from buildlimits.status_client import StatusQueryError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from syrupy.assertion import SnapshotAssertion

# A transaction still in flight, so ``--wait`` enters its poll loop before the
# documents below decide the outcome.
_IN_FLIGHT: dict[str, object] = {"phase": "stopping", "last_fault": None}

# The intervals ``--wait`` asked to sleep for, so no test waits on the clock.
_SLEPT: list[float] = []


def _sleep(seconds: float) -> None:
    """Record the interval the poll asked for instead of waiting.

    Parameters
    ----------
    seconds : float
        The interval the caller asked to wait.
    """
    _SLEPT.append(seconds)


def _frozen_clock() -> float:
    """Report a bound no wait can reach, so only the document ends it.

    Returns
    -------
    float
        Zero, always. Asserting that the supervisor's own outcome decides the
        status is only exact if the wait's deadline cannot also decide it: a
        real clock here would let a later poll leave the loop for a reason
        these cases do not name.
    """
    return 0.0


def _settling(status: dict[str, object]) -> cabc.Callable[[], dict[str, object]]:
    """Build a query that finds the transaction settled on its first poll.

    The in-flight state the wait starts from comes from the request's own
    reply, not from this query, which reports ``status`` on every poll.

    Parameters
    ----------
    status : dict[str, object]
        The status document the supervisor reports once polled.

    Returns
    -------
    Callable[[], dict[str, object]]
        A query that always answers ``status``.
    """
    return lambda: status


# The idle transaction exactly as the supervisor reports it, pinned rather than
# inferred so ``RENDERED`` below is built from one member already carrying the
# annotation's type. Left to inference the literal narrows to
# ``dict[str, str | None]`` — the checker sees only its own three members — and
# the table would then be rejected for holding a narrower document than it
# declares.
_SETTLED_TRANSACTION: typ.Final[dict[str, object]] = {
    "phase": "idle",
    "blocked": "",
    "last_fault": None,
}

# The two outcomes an operator's script branches on: the transaction ended
# cleanly, or it ended while the live-work fault still retained the tokens.
_SETTLED: typ.Final[dict[str, object]] = {
    "cache_recovery": _SETTLED_TRANSACTION,
    "drain_fault_kind": None,
}
_STILL_RETAINED: typ.Final[dict[str, object]] = _SETTLED | {
    "drain_fault_kind": "live_work"
}


# The barrier-exhausted transaction, pinned for the same reason as
# ``_SETTLED_TRANSACTION`` above: every member of this one is a string, so
# inference would narrow it and reject the table that holds it.
_BARRIER_EXHAUSTED_TRANSACTION: typ.Final[dict[str, object]] = {
    "phase": "idle",
    "blocked": "",
    "last_fault": "cannot clear the recovery barrier",
}

# Two transactions that exhausted their attempts and gave up: both idle with
# their tokens released, and both leaving the fault visible because a step
# never succeeded. The texts are the ones the machine composes: the barrier
# path in ``_try_start``, and a refused start as ``_control`` reports it.
_FAULTED_IDLE: typ.Final[dict[str, object]] = {
    "cache_recovery": _BARRIER_EXHAUSTED_TRANSACTION,
    "drain_fault_kind": None,
}
_EXHAUSTED_RESTART: typ.Final[dict[str, object]] = _FAULTED_IDLE | {
    "cache_recovery": {
        "phase": "idle",
        "blocked": "",
        "last_fault": "cannot start cache.service: Job failed",
    },
}


@pytest.mark.parametrize(
    ("status", "expected"),
    [(_SETTLED, 0), (_STILL_RETAINED, 1), (_FAULTED_IDLE, 1), (_EXHAUSTED_RESTART, 1)],
    ids=["complete", "retained", "barrier-exhausted", "restart-exhausted"],
)
def test_a_wait_reports_the_outcome_the_supervisor_settled_on(
    status: dict[str, object], expected: int
) -> None:
    """A reachable supervisor's own document decides the status."""
    _SLEPT.clear()
    code = recover_cli.main(
        ["--wait"],
        request=lambda: _IN_FLIGHT,
        query=_settling(status),
        waiting=recover_cli.Waiting(sleep=_sleep, monotonic=_frozen_clock),
    )
    assert code == expected, "the document's own outcome decides the status"
    assert _SLEPT, "the wait polled rather than trusting its first read"


def test_a_wait_whose_supervisor_departs_is_never_reported_as_complete(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A supervisor that becomes unreachable mid-wait exits 2, not 0.

    The transaction was still in flight when the supervisor was next asked, so
    nothing observed it finish. Reporting the idle stand-in here would tell an
    operator's script that a recovery completed and released its tokens when
    the opposite may be true.
    """

    def departed() -> dict[str, object]:
        """Fail the way the client does when the socket disappears."""
        msg = "the supervisor socket disappeared"
        raise StatusQueryError(msg)

    code = recover_cli.main(
        ["--wait"],
        request=lambda: _IN_FLIGHT,
        query=departed,
        waiting=recover_cli.Waiting(sleep=_sleep, monotonic=_frozen_clock),
    )
    assert code == 2, "an unobserved outcome is never a completed recovery"
    assert "supervisor socket disappeared" in capsys.readouterr().err, (
        "the failure reaches the operator rather than the idle summary"
    )


def test_a_wait_that_cannot_open_a_transaction_keeps_its_own_exit_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A request refused at the supervisor never reaches the wait."""

    def refused() -> dict[str, object]:
        """Refuse to open a transaction, as an unreachable supervisor does."""
        msg = "the supervisor socket could not be reached"
        raise RecoverQueryError(msg)

    code = recover_cli.main(["--wait"], request=refused, query=lambda: _IN_FLIGHT)
    assert code == 2, "no transaction exists to wait on"
    assert "could not be reached" in capsys.readouterr().err, (
        "the request's own refusal explains the status"
    )


def test_a_wait_that_outlives_its_timeout_reports_an_unfinished_recovery(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A wait that expires never claims a recovery it did not observe.

    The transaction is still in flight when the window closes, so exit 0 would
    tell an operator's script that tokens were released when nothing has
    released them. The clock is injected rather than the sleep faked: a fake
    sleep would still race a real deadline, so this case could only otherwise
    be exercised by waiting the default out for real.
    """
    ticks = [0.0]

    def clock() -> float:
        """Advance the injected clock past the wait window on the first poll."""
        ticks[0] += recover_cli.WAIT_SECONDS
        return ticks[0]

    code = recover_cli.main(
        ["--wait"],
        request=lambda: _IN_FLIGHT,
        # Never settles: the supervisor keeps reporting the transaction.
        query=lambda: _IN_FLIGHT,
        waiting=recover_cli.Waiting(sleep=lambda _seconds: None, monotonic=clock),
    )
    assert code == 1, "an unobserved outcome is never a completed recovery"
    assert "stopping" in capsys.readouterr().out, (
        "the operator still sees the phase the wait gave up on"
    )
    assert ticks[0] > recover_cli.WAIT_SECONDS, "the wait actually consulted its bound"


# The documents below are the five shapes ``_summary`` renders differently: an
# idle transaction, one still working through a phase, one that ended on a
# fault, one blocked before it could start, and one whose tokens the live-work
# fault still retains. Each carries fixed retry and fault values so the
# snapshot holds no elapsed time or environment-specific text; ``_IN_FLIGHT``
# is reused so the working case matches what ``--wait`` observes in the cases
# above. Every one of them is an already-unwrapped transaction document,
# because that is what ``request`` returns: ``_phase`` unwraps the status
# document's ``cache_recovery`` member and folds the drain fault into
# ``retained``, so a nested document here would pin the summary of a shape the
# command never produces.
_WORKING: typ.Final[dict[str, object]] = {
    "phase": "restarting",
    "attempts": 2,
    "retry_in": 60.0,
    "last_fault": "cannot start cache.service: Job failed",
    "retained": False,
}
_BLOCKED: typ.Final[dict[str, object]] = {
    "phase": "idle",
    "blocked": "2 compiler group(s) are still running",
}
_RETAINED: typ.Final[dict[str, object]] = _SETTLED["cache_recovery"] | {
    "retained": True
}

RENDERED: dict[str, tuple[dict[str, object], int, list[str]]] = {
    "idle": (_SETTLED["cache_recovery"], 0, ["cache recovery idle"]),
    "retained": (
        _RETAINED,
        1,
        ["cache recovery idle", "tokens are held until the cache is verified"],
    ),
    "working": (
        _WORKING,
        1,
        ["cache recovery restarting", "attempts 2", "next attempt in 60.0s"],
    ),
    "blocked": (_BLOCKED, 1, ["recovery cannot start", "2 compiler group(s)"]),
    "faulted": (
        _FAULTED_IDLE["cache_recovery"],
        1,
        ["cache recovery idle", "last fault: cannot clear the recovery barrier"],
    ),
}


@pytest.mark.parametrize("name", RENDERED)
def test_the_operator_text_reports_each_outcome(
    name: str,
    capsys: pytest.CaptureFixture[str],
    snapshot: SnapshotAssertion,
) -> None:
    """The text output an operator reads is pinned whole, per outcome."""
    document, expected, fragments = RENDERED[name]
    code = recover_cli.main([], request=lambda: dict(document))
    captured = capsys.readouterr()

    assert code == expected, "the rendered outcome matches the exit status"
    assert not captured.err, "a reported outcome is not an error"
    for fragment in fragments:
        assert fragment in captured.out, captured.out
    assert captured.out == snapshot, "the rendered recovery summary changed"


@pytest.mark.parametrize("name", ["working", "blocked", "retained"])
def test_the_json_output_is_the_document_an_operator_scripts_against(
    name: str,
    capsys: pytest.CaptureFixture[str],
    snapshot: SnapshotAssertion,
) -> None:
    """``--json`` emits the supervisor's own document, unchanged and parseable.

    The text form is a summary, so a consumer that needs a field the summary
    omits reads this one. Parsing what was printed — rather than the document
    the test supplied — is what proves the emitted JSON round-trips.
    """
    document, _, _ = RENDERED[name]
    recover_cli.main(["--json"], request=lambda: dict(document))
    captured = capsys.readouterr()

    assert not captured.err, "a reported outcome is not an error"
    assert json.loads(captured.out) == json.loads(json.dumps(document)), (
        "the JSON output is the document the supervisor reported"
    )
    assert captured.out == snapshot, "the JSON recovery document changed"
