"""Retire a restart barrier that an interrupted recovery transaction left.

A supervisor that dies mid-transaction leaves the barrier in place, and the
managed unit's own ``ConditionPathExists=!`` is what reads it: a cache behind a
surviving barrier is never merely slow, its start is skipped outright. Only an
accounted start-up can retire it, so this runs before the transaction state
machine exists rather than as one of its steps — the next supervisor start has
just proved the cache stopped and empty, which is what makes the barrier
obsolete.

The barrier is cleared before the cache is started, because that proof is what
made it obsolete: a refusal must not re-block a cache whose tokens admission
already released. Two failures are the exception, and neither is fatal.

- A barrier that cannot be inspected may hide one the unit still honours, so
  nothing is started and nothing is cleared: start and clear are each unsafe on
  evidence that cannot be read.
- A barrier that cannot be cleared would have its start skipped by the unit's
  own condition, so nothing is queued.

Both are reported rather than guessed at, because the operator who sees no
cache must be told why instead of inferring it from a supervisor quietly
compiling directly.
"""

import contextlib
import typing as typ

from buildlimits.journal import log
from buildlimits.recovery_barrier import RecoveryBarrier
from buildlimits.systemd import SystemdError

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.sccache import ManagedSccache


def new_barrier(marker: Path) -> RecoveryBarrier:
    """Derive the barrier handle for a marker path.

    The managed unit's ``ConditionPathExists=!`` and this handle read the same
    file, so the path is the only fact a caller supplies; that is why the
    transaction derives its handle here rather than accepting one.

    Parameters
    ----------
    marker : Path
        The restart barrier's path.

    Returns
    -------
    RecoveryBarrier
        The handle for that path.
    """
    return RecoveryBarrier(marker)


def restart_fault(error: OSError | SystemdError, barrier: RecoveryBarrier) -> str:
    """Describe a refused restart, naming the barrier only for a barrier.

    Parameters
    ----------
    error : OSError | SystemdError
        The failure the restart step met.
    barrier : RecoveryBarrier
        The barrier whose removal failed, if that is what failed.

    Returns
    -------
    str
        The fault text for the status surface and journal. A ``SystemdError``
        carries the manager's own refusal of the start, so prefixing it with
        the barrier's path would attribute the fault to the wrong step.
    """
    match error:
        case OSError():
            return f"cannot clear {barrier.path}: {error}"
        case _:
            return str(error)


def reclaim_after_accounting(
    *,
    cache_mode: str,
    barrier: RecoveryBarrier,
    cache: ManagedSccache,
) -> None:
    """Restore the managed cache after an accounted start-up released its tokens.

    Parameters
    ----------
    cache_mode : str
        Only ``sccache`` owns the barrier and the managed server; any other
        mode returns without touching either.
    barrier : RecoveryBarrier
        The restart barrier left by an interrupted transaction, if any.
    cache : ManagedSccache
        The managed server whose start is queued once the barrier is gone.

    Notes
    -----
    Every failure is logged and swallowed: accounting has already succeeded,
    so admission must not wait on a cache that is only slow to return.
    """
    if cache_mode != "sccache":
        return
    try:
        # Stat the path rather than asking ``Path.exists``: from Python 3.14
        # ``exists`` answers ``False`` for every ``OSError``, so an unreadable
        # parent directory would read as an absent barrier and a start would be
        # queued on evidence that is merely unknown. ``stat`` keeps that
        # distinction, which is the one this caller must report.
        barrier.path.stat()
        stale = True
    except FileNotFoundError:
        # The barrier is genuinely absent, which is the settled state this
        # reclamation exists to bring about. Nothing to retire.
        stale = False
    except OSError as error:
        # Start and clear are both unsafe here. An unreadable path may hide a
        # barrier the unit can still see, so a start issued now would be
        # accepted and then skipped; it may equally be a barrier already gone
        # with an unreadable parent, where a clear would fail anyway. Leaving
        # the file alone fails closed: nothing returns the cache to work whose
        # token the accounting released only because the cache was proven
        # stopped and empty. The operator gets the reason.
        _report(f"cannot inspect {barrier.path} after an accounted start-up: {error}")
        return
    if not stale:
        return
    try:
        barrier.clear()
        cache.start()
    except OSError as error:
        _report(
            f"reclaimed an interrupted recovery; cannot clear {barrier.path}: {error}"
        )
        return
    except SystemdError as error:
        # The supervisor carries on compiling directly, so the operator who
        # sees no cache must be told why rather than left to guess.
        _report(f"reclaimed an interrupted recovery; cache start refused: {error}")
        return
    _report("reclaimed an interrupted recovery; the managed cache is starting")


def _report(message: str) -> None:
    """Journal one reclaim outcome, never failing the start-up over it."""
    with contextlib.suppress(OSError):
        log(message)
