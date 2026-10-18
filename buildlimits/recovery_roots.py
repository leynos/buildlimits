"""The stop's second deadline: how long visible roots may delay an automatic stop.

The zero-root proof gates the automatic stop, but a compile that hangs inside
the managed server keeps a root visible for ever, so the retained tokens would
never be recovered. Once no cached group is moving, a root still visible under
the held barrier serves no admitted client, because every group admitted since
the barrier was written composes without the server. This module owns the
clock that measures that, and the pure verdict that applies
``RECOVERY_ROOTS_GRACE_SECONDS`` to it. It was split from
:mod:`buildlimits.recovery_starvation`, which holds the constant and the
opening policy, to keep both modules inside the size ceiling.
"""

import dataclasses

from buildlimits.recovery_starvation import RECOVERY_ROOTS_GRACE_SECONDS


@dataclasses.dataclass(slots=True)
class VisibleRootsGrace:
    """Measure how long roots have stayed visible with nothing cached moving.

    The clock starts at the first stop attempt of an automatic transaction
    that finds no cached group moving and a root still visible. It restarts
    from nothing when the stop is blocked (a cached group moves again or the
    manager is faulted), when the roots clear, and when a transaction opens or
    ends, so a later transaction can never inherit an earlier one's start.

    Examples
    --------
    >>> grace = VisibleRootsGrace()
    >>> grace.observe(is_visible=True, now=10.0)
    0.0
    >>> grace.observe(is_visible=True, now=25.0)
    15.0
    >>> grace.observe(is_visible=False, now=30.0)
    0.0
    """

    since: float | None = None

    def observe(self, *, is_visible: bool, now: float) -> float:
        """Record one stop attempt's reading and return the elapsed time.

        Parameters
        ----------
        is_visible : bool
            Whether the attempt needed the zero-root proof and lacked it.
        now : float
            Current monotonic time.

        Returns
        -------
        float
            Seconds since the first such attempt of this unbroken run, or zero
            when the reading breaks the run.
        """
        if not is_visible:
            self.since = None
            return 0.0
        if self.since is None:
            self.since = now
        return now - self.since

    def elapsed(self, now: float) -> float:
        """Read the elapsed time without recording an observation.

        Parameters
        ----------
        now : float
            Current monotonic time.

        Returns
        -------
        float
            Seconds since the run began, or zero when none is running.
        """
        return 0.0 if self.since is None else now - self.since

    def reset(self) -> None:
        """Break the run, as a blocked stop or a transaction boundary does."""
        self.since = None


def roots_verdict(
    *, is_automatic: bool, zero_visible_roots: bool, waited: float
) -> str:
    """Decide whether the stop has the evidence about visible roots it needs.

    Only an automatic stop needs the zero-root proof; a manual request carries
    the operator's own evidence. Visible roots do not stop it for ever: once
    nothing cached has moved for ``RECOVERY_ROOTS_GRACE_SECONDS`` they serve
    no admitted client.

    Parameters
    ----------
    is_automatic : bool
        Whether the transaction was opened by the automatic policy.
    zero_visible_roots : bool
        Whether the probe established zero visible compiler roots.
    waited : float
        Seconds roots have stayed visible with nothing cached moving.

    Returns
    -------
    str
        ``exempt`` for a manual transaction, ``proven`` when no root is
        visible, ``deadline`` when the grace has passed, otherwise ``waiting``.

    Examples
    --------
    >>> roots_verdict(is_automatic=True, zero_visible_roots=False, waited=600.0)
    'deadline'
    """
    if not is_automatic:
        return "exempt"
    if zero_visible_roots:
        return "proven"
    return "deadline" if waited >= RECOVERY_ROOTS_GRACE_SECONDS else "waiting"


def stop_verdict(
    grace: VisibleRootsGrace,
    *,
    is_automatic: bool,
    zero_visible_roots: bool,
    now: float,
) -> str:
    """Time the visible roots at one stop attempt and judge them.

    Parameters
    ----------
    grace : VisibleRootsGrace
        The transaction's clock, updated by this attempt.
    is_automatic : bool
        Whether the transaction was opened by the automatic policy.
    zero_visible_roots : bool
        Whether the probe established zero visible compiler roots.
    now : float
        Current monotonic time.

    Returns
    -------
    str
        The :func:`roots_verdict` for this attempt. The clock runs only while
        an automatic stop lacks the zero-root proof.
    """
    is_visible = is_automatic and not zero_visible_roots
    return roots_verdict(
        is_automatic=is_automatic,
        zero_visible_roots=not is_visible,
        waited=grace.observe(is_visible=is_visible, now=now),
    )


def deadline_stop_message(grace: VisibleRootsGrace, now: float) -> str:
    """Word the journal line for a stop taken on the roots deadline.

    Returns
    -------
    str
        The line, carrying how long the roots delayed the stop.
    """
    return (
        "cache recovery: stopping the managed cache with compiler roots still "
        f"visible after {grace.elapsed(now):.0f}s with no cached group moving"
    )
