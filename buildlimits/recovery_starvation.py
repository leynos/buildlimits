"""Whether a recovery may act at all: the automatic trigger and the block.

A recovery is opened one of two ways, and both are answered here. An operator
asks for one, or the automatic policy decides that live cache work is starving
admission; either way the transaction may only open when the evidence permits
it, and those are the two questions this module answers.

The automatic evidence is narrow: the drain probe must have faulted on *live*
work, it must have established that no compiler root is visible, some token
must be waiting on the server with no cached group still moving, and a request
must be waiting behind them. An unproven reading is not a
weaker reason to act — it is no reason, which is why every clause is a separate
question with a name rather than a term in one long conjunction.

Only cached groups are counted as moving. A transaction denies cache
composition from the moment it opens, so the requests admitted while it runs
compile without the server, and stopping the server cannot interrupt them.
Counting those groups too would let a busy queue refill the free slots faster
than the stop is retried, which held the retained tokens indefinitely.

Both belong beside the transaction they authorize rather than beside the
start-up reclamation that once shared this code's module: that module retires a
barrier an interrupted transaction left behind, and it reads neither the gauges
nor the policy.
"""

import dataclasses
import typing as typ

if typ.TYPE_CHECKING:
    from buildlimits.admission_state import Counts

# A transaction that ended faulted waits out more than the whole bounded retry
# sequence (780 s for the transaction's bounds) before another may start, so a
# cache that is merely slow to restart cannot exhaust its attempts exactly as
# recovery becomes available again.
RECOVERY_COOLDOWN_SECONDS = 900.0
# A clean transaction proved the cache stopped, empty and restarted, so the
# only reason to wait is to let that restart settle. Holding the faulted
# cooldown here left freshly retained tokens idle for a quarter of an hour
# while a queue waited behind them.
RECOVERY_SETTLE_SECONDS = 60.0


def cooldown_after(*, faulted: bool) -> float:
    """Choose how long the automatic trigger rests after a transaction ends.

    Parameters
    ----------
    faulted : bool
        Whether the transaction ended with its restart still refused.

    Returns
    -------
    float
        Seconds before the trigger may open another transaction.

    Examples
    --------
    >>> cooldown_after(faulted=False) < cooldown_after(faulted=True)
    True
    """
    return RECOVERY_COOLDOWN_SECONDS if faulted else RECOVERY_SETTLE_SECONDS


@dataclasses.dataclass(frozen=True, slots=True)
class StarvationReading:
    """Everything the automatic trigger must read to justify acting.

    The fields are one observation of the same instant, which is why they
    travel together: a policy read from one tick and a gauge count from another
    would compose a trigger no single moment justified.

    Attributes
    ----------
    cache_mode : str
        Only ``sccache`` owns the barrier and the managed server.
    policy : str
        ``manual`` never triggers automatically; ``starved`` may.
    cooling : float
        The monotonic instant before which no transaction may start.
    now : float
        Current monotonic time.
    counts : Counts
        The current admission snapshot.
    fault_kind : str | None
        The drain probe's last fault classification, if any.
    zero_visible_roots : bool
        Whether that fault established zero visible compiler roots.
    """

    cache_mode: str
    policy: str
    cooling: float
    now: float
    counts: Counts
    fault_kind: str | None
    zero_visible_roots: bool


def starvation(reading: StarvationReading) -> str | None:
    """Decide whether live cache work is starving admission right now.

    Every clause is evidence, not a heuristic: the drain probe must have
    faulted on live work *and* established that no compiler root is visible,
    some token must be waiting on the server with no cached group moving, and
    a request must be waiting. An unproven reading is not a weaker
    reason to act — it is no reason.

    Parameters
    ----------
    reading : StarvationReading
        The one instant the trigger is judged against.

    Returns
    -------
    str | None
        The reason to trigger, or ``None`` to leave admission alone.
    """
    counts = reading.counts
    # One clause per question the docstring names, each asked in turn rather
    # than composed into a single expression. The evidence is conjunctive, but
    # a reader verifying that every claim is present should not have to hold
    # eight terms at once, and a clause that fails should be attributable to a
    # named question rather than to a position in a chain.
    if not _policy_permits_recovery(reading):
        return None
    if not _the_probe_proved_detached_work(reading):
        return None
    if not _the_server_alone_holds_cached_work(counts):
        return None
    if counts.queued == 0:
        return None
    return (
        f"live cache work is starving admission: {counts.server_drain} token(s) "
        f"await server accounting, and "
        f"{counts.queued} request(s) are waiting"
    )


def blocked(counts: Counts, fault_kind: str | None) -> str:
    """Explain why a transaction must not start.

    A cached running group and a cached draining group each block the stop,
    because the cache serves them and a draining group's leader may still have
    live children. A group composed without the cache does not: stopping the
    server cannot interrupt it, and the transaction itself composes every
    group admitted while it runs that way.

    Parameters
    ----------
    counts : Counts
        The current admission snapshot.
    fault_kind : str | None
        The drain probe's last fault classification, if any.

    Returns
    -------
    str
        The reason it cannot start, or an empty string when it can.
    """
    if counts.cached_moving:
        return (
            f"{counts.cached_moving} cached compiler group(s) are still moving; "
            "stopping the cache would interrupt them"
        )
    if fault_kind == "manager":
        return (
            "the user manager transport is faulted; restore it before "
            "stopping the managed cache"
        )
    return ""


def _policy_permits_recovery(reading: StarvationReading) -> bool:
    """Report whether the cache mode, policy and cooldown all permit acting.

    Returns
    -------
    bool
        Whether every policy condition for automatic recovery holds.
    """
    if reading.cache_mode != "sccache":
        return False
    if reading.policy != "starved":
        return False
    return reading.now >= reading.cooling


def _the_probe_proved_detached_work(reading: StarvationReading) -> bool:
    """Report whether the drain probe proved live work with no visible root.

    Without the zero-root proof a live-work fault alone is no reason to act: it
    is also the ordinary reading for a compile that is simply still running.
    With it, the retained work is provably detached from every visible root.

    Returns
    -------
    bool
        Whether the probe both faulted on live work and found no visible root.
    """
    return reading.fault_kind == "live_work" and reading.zero_visible_roots


def _the_server_alone_holds_cached_work(counts: Counts) -> bool:
    """Report whether the server holds tokens and no cached group is moving.

    A cached running or draining group fails it, since stopping the cache
    would interrupt that group. An uncached group does not: it never reached
    the server, so the stop cannot harm it.

    Returns
    -------
    bool
        Whether a token awaits the server and no cached group is moving.
    """
    if counts.server_drain == 0:
        return False
    return counts.cached_moving == 0
