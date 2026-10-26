"""What the automatic recovery trigger is waiting for, for the status document.

An agent reading ``build-limits-status`` needs to tell a recovery that is about
to happen by itself from one that needs an operator. Both answers come from the
clauses :func:`buildlimits.recovery_starvation.starvation` judges, asked of the
same :class:`~buildlimits.recovery_starvation.StarvationReading`, so status
cannot promise a recovery the trigger would not perform.

Only two clauses are matters of time: the cooldown after a transaction and the
staleness proof. Every other clause is asked by
:func:`buildlimits.recovery_starvation.permits_but_for_time`. The stop that
follows an opening is gated by :func:`buildlimits.recovery_starvation.blocked`
and has no part here: an opening is safe while cached work runs, because the
open transaction holds new compiles off the cache.
"""

import dataclasses as dc

from buildlimits import recovery_starvation
from buildlimits.cache_backend import is_detached_mode
from buildlimits.recovery_starvation import RECOVERY_STALE_DRAIN_SECONDS


def opens_in(reading: recovery_starvation.StarvationReading) -> float | None:
    """Say how long the automatic trigger must still wait on time alone.

    The trigger opens at the later of its cooldown and the stale proof, and at
    the cooldown alone when the prompt proof already holds.

    Parameters
    ----------
    reading : StarvationReading
        The one instant the trigger is judged against.

    Returns
    -------
    float | None
        ``None`` when waiting will not open the trigger, because a clause that
        time cannot change refuses it; ``0.0`` when
        :func:`~buildlimits.recovery_starvation.starvation` acts on this very
        reading; otherwise the seconds until it will, provided nothing else
        changes.

    Examples
    --------
    >>> opens_in.__name__
    'opens_in'
    """
    if not recovery_starvation.permits_but_for_time(reading):
        return None
    cooling = max(0.0, reading.cooling - reading.now)
    if recovery_starvation.proof(reading) is not None:
        return cooling
    return max(cooling, RECOVERY_STALE_DRAIN_SECONDS - reading.drain_age)


@dc.dataclass(frozen=True, slots=True)
class Outlook:
    """Whether recovery is automatic, how long it waits, and whether it is due.

    Attributes
    ----------
    automatic : bool
        Whether the cache mode and policy let the trigger act at all.
    opens_in : float | None
        Seconds the idle trigger must still wait on time alone, or ``None``
        when waiting will not open it or a transaction is running.
    pending : bool
        Whether a recovery is due or under way without an operator.
    """

    automatic: bool
    opens_in: float | None
    pending: bool

    def rounded_opens_in(self) -> float | None:
        """Round the wait as the status document reports every duration.

        Returns
        -------
        float | None
            The wait to the millisecond, or ``None`` when there is none.
        """
        return None if self.opens_in is None else round(self.opens_in, 3)


def outlook(
    reading: recovery_starvation.StarvationReading,
    *,
    is_idle: bool,
    has_attempts_left: bool,
) -> Outlook:
    """Describe what the trigger and the running transaction will do.

    Parameters
    ----------
    reading : StarvationReading
        The one instant the trigger is judged against.
    is_idle : bool
        Whether no transaction is running.
    has_attempts_left : bool
        Whether a running transaction may still retry its failing step.

    Returns
    -------
    Outlook
        Pending while the idle trigger waits on time alone, or while a
        transaction has attempts left. A trigger that waiting will never open
        and a transaction that used every attempt need the operator.
    """
    wait = opens_in(reading) if is_idle else None
    return Outlook(
        automatic=(
            is_detached_mode(reading.cache_mode) and reading.policy == "starved"
        ),
        opens_in=wait,
        pending=wait is not None if is_idle else has_attempts_left,
    )
