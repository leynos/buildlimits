"""The ledger the recovery search records its control events in.

The stateful search in :mod:`cache_recovery_state_machine` counts the events
its rules produce so that a passing search is evidence about the contract
rather than about an empty prefix of it. These records live here, beside the
search rather than inside it, so the search module stays within the size
ceiling and a reader of the ledger need not read the rules.

It is an unnumbered helper module, named like its siblings so pytest collects
nothing from it, and imported by its bare name because the suite puts this
directory on the import path.
"""

import dataclasses as dc


@dc.dataclass(slots=True)
class Events:
    """How many control events a search observed, as counts.

    A count rather than a flag: the assertion the test module makes is that
    each event happened at all, but a ledger that only latched the first
    occurrence could not tell a search that reached the barrier once from one
    that never left it, and that difference is what a later failing run needs
    in order to be read.

    Attributes
    ----------
    opened : int
        Transactions the run started, by operator request or automatic policy.
    stopped : int
        Stop verbs issued to the managed cache.
    started : int
        Start verbs issued to the managed cache.
    opened_stale : int
        Automatic openings taken on the drain's age alone, with a group moving
        or a root visible, which only the staleness proof can authorize.
    stopped_on_deadline : int
        Automatic stops issued with a root still visible, which only the
        roots deadline can authorize.
    """

    opened: int = 0
    stopped: int = 0
    started: int = 0
    opened_stale: int = 0
    stopped_on_deadline: int = 0


@dc.dataclass(slots=True)
class CalmClock:
    """When nothing cached last began to be quiet, as the model sees it.

    It starts no later than the production grace clock does (that waits for a
    stopping attempt), so a stop taken on the roots deadline must have at
    least the grace elapsed on it.

    Attributes
    ----------
    since : float or None
        The instant of the first tick after which nothing cached was moving and
        the manager was readable, unbroken since.
    """

    since: float | None = None

    def observe(self, *, is_calm: bool, now: float) -> None:
        """Record one tick: break the run, or start it if none is running."""
        if not is_calm:
            self.since = None
        elif self.since is None:
            self.since = now


@dc.dataclass(frozen=True, slots=True)
class Moment:
    """The phase and control-verb counts at one instant of the run.

    Attributes
    ----------
    phase : str
        The phase the transaction reported.
    stops : int
        Stop verbs issued so far.
    starts : int
        Start verbs issued so far.
    """

    phase: str
    stops: int
    starts: int
