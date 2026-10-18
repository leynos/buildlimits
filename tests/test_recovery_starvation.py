"""The automatic trigger, decided over every combination it can be asked about.

:func:`~buildlimits.recovery_starvation.starvation` is a conjunction of named
clauses over finite categories, so it has a finite input space and every point
in it can be visited rather than sampled. The parameterized cases in
:mod:`test_cache_recovery_machine` drive the real transaction for a selection
of these points, which is what shows the answer reaches the manager; this
module instead enumerates the space, so a clause that stops discriminating is a
failure here rather than a combination nobody happened to script.

Enumerating is worth the space because the clauses are read as a conjunction
and are easy to *believe* while being wrong in one direction: a clause that
returns True too readily still satisfies every case that expects a trigger, and
the cases that would notice are exactly the ones nobody writes. The table
derives its expectation from the clause definitions rather than restating the
implementation, so a change to either is visible as a disagreement instead of a
tidy update.

The space is the product of the categories ``StarvationReading`` names, minus
the combinations its own types exclude. ``fault_kind`` and
``zero_visible_roots`` are not independent: the live watch records a root count
only for a live-work fault and clears it for every other kind, which
:class:`~cache_recovery_helpers.DrainReading` exists to keep out of the
fixtures. ``cooling`` is compared against ``now`` rather than combined with it,
so it contributes two cases — a cooldown that has passed and one that has not —
instead of a continuum of instants.
"""

import collections.abc as cabc
import dataclasses as dc
import itertools
import pathlib
import typing as typ

import pytest

from buildlimits.admission_state import Counts
from buildlimits.recovery_barrier import RecoveryBarrier
from buildlimits.recovery_starvation import StarvationReading, starvation

# The finite categories, each named for the clause it exercises. They are
# tuples rather than sets so the generated case ids read in the order the
# clauses appear in ``starvation``.
CACHE_MODES = ("sccache", "direct")
POLICIES = ("starved", "manual")
# A cooldown the instant has passed, and one it has not. ``now`` is 100.0 in
# every case below.
COOLINGS = (0.0, 200.0)
# The drain readings a live watch can hold, as ``(fault_kind, roots)`` pairs.
# Only the live-work kind carries a root count, so the other kinds pair with
# the unreadable count rather than with a number the watch never records.
READINGS: typ.Final[tuple[tuple[str | None, int | None], ...]] = (
    ("live_work", 0),
    ("live_work", 2),
    ("live_work", None),
    ("manager", None),
    ("stopped_populated", None),
    (None, None),
)
# The gauge shapes that decide the remaining two clauses. Each is named for
# what it makes of ``server_drain > 0 and cached_moving == 0`` and
# ``queued > 0``:
#
#   starving       one token awaiting the server, one request behind it
#   no-queue       nothing waiting, so the cache cannot be starving anything
#   nothing-held   nothing retained, so there is no server work to account for
#   group-moving   a cached running group the stop would interrupt
#   group-draining a cached draining group, which may still have children
#   uncached-moving an uncached running group, which the stop cannot touch
GAUGES: typ.Final[dict[str, Counts]] = {
    "starving": Counts(1, 1, 0, 0, 1, capacity=2, cached_moving=0),
    "no-queue": Counts(0, 1, 0, 0, 1, capacity=2, cached_moving=0),
    "nothing-held": Counts(1, 0, 0, 0, 0, capacity=2, cached_moving=0),
    "group-moving": Counts(1, 2, 1, 0, 1, capacity=2, cached_moving=1),
    "group-draining": Counts(1, 2, 0, 1, 1, capacity=2, cached_moving=1),
    "uncached-moving": Counts(1, 2, 1, 0, 1, capacity=2, cached_moving=0),
}

BARRIER_PATH = "/run/user/1000/build-limits/cache-recovery"
BARRIER = RecoveryBarrier(pathlib.Path(BARRIER_PATH))


@dc.dataclass(frozen=True, slots=True)
class Case:
    """One point in the trigger's finite input space.

    Attributes
    ----------
    cache_mode : str
        The configured cache mode.
    policy : str
        The configured recovery policy.
    cooling : float
        The cooldown instant, against ``now`` of 100.0.
    fault_kind : str or None
        The drain fault the server watch last reported.
    roots : int or None
        The visible root count recorded beside that fault.
    gauges : str
        The name of the gauge shape in :data:`GAUGES`.
    """

    cache_mode: str
    policy: str
    cooling: float
    fault_kind: str | None
    roots: int | None
    gauges: str

    @property
    def now(self) -> float:
        """The instant the trigger is judged against, for every case.

        Returns
        -------
        float
            The fixed monotonic time, so ``cooling`` alone decides the
            cooldown clause.
        """
        return 100.0

    @property
    def reading(self) -> StarvationReading:
        """Build the reading this case describes.

        Returns
        -------
        StarvationReading
            The one-instant observation the trigger reads.
        """
        return StarvationReading(
            cache_mode=self.cache_mode,
            policy=self.policy,
            cooling=self.cooling,
            now=self.now,
            counts=GAUGES[self.gauges],
            fault_kind=self.fault_kind,
            zero_visible_roots=self.fault_kind == "live_work" and self.roots == 0,
        )


# The clauses of the conjunction, in the order the prose in
# :mod:`~buildlimits.recovery_starvation` states them. Each is a named
# predicate so that every clause stays separately visible: inlined into one
# decision function a clause that stopped discriminating would be a line nobody
# re-reads, and the clause-by-clause witnesses in :data:`_QUESTIONS` could not
# be attributed to a name.
def _permits_mode(case: Case) -> bool:
    """Reject every cache mode that owns no barrier or managed server."""
    return case.cache_mode == "sccache"


def _permits_policy(case: Case) -> bool:
    """Reject every policy other than the automatic one."""
    return case.policy == "starved"


def _permits_cooling(case: Case) -> bool:
    """Refuse to act while the cooldown is still running."""
    return case.now >= case.cooling


def _permits_live_work(case: Case) -> bool:
    """Require the probe to have faulted on live work, not on another kind."""
    return case.fault_kind == "live_work"


def _permits_no_root(case: Case) -> bool:
    """Require the probe to have proved no visible root.

    A live-work fault alone is also the ordinary reading for a compile that is
    simply still running, so this clause is what tells the two apart.
    """
    return case.roots == 0


def _permits_server_alone(case: Case) -> bool:
    """Require a token awaiting the server and no cached group moving."""
    counts = GAUGES[case.gauges]
    return counts.server_drain > 0 and counts.cached_moving == 0


def _permits_queue(case: Case) -> bool:
    """Require something to be waiting behind the retained work."""
    return GAUGES[case.gauges].queued > 0


_CLAUSES: typ.Final[tuple[tuple[str, cabc.Callable[[Case], bool]], ...]] = (
    ("the cache mode owns the barrier", _permits_mode),
    ("the policy permits automatic recovery", _permits_policy),
    ("the cooldown has elapsed", _permits_cooling),
    ("the probe faulted on live work", _permits_live_work),
    ("the probe proved no visible root", _permits_no_root),
    ("only the server holds cached work", _permits_server_alone),
    ("a request is waiting behind them", _permits_queue),
)


def _expected(case: Case) -> str | None:
    """Decide the case from the clause definitions, not the implementation.

    Each clause is restated here as the prose in
    :mod:`~buildlimits.recovery_starvation` states it, so the table fails when
    the module and its documented contract disagree. A clause list that merely
    called ``starvation`` would agree by construction and could not notice a
    clause that stopped discriminating.

    Parameters
    ----------
    case : Case
        The point being decided.

    Returns
    -------
    str or None
        The reason a trigger is due, or None when the reading permits none.
    """
    if not all(clause(case) for _, clause in _CLAUSES):
        return None
    counts = GAUGES[case.gauges]
    return (
        f"live cache work is starving admission: {counts.server_drain} token(s) "
        f"await server accounting behind the barrier {BARRIER.path}, and "
        f"{counts.queued} request(s) are waiting"
    )


def _cases() -> list[Case]:
    """Enumerate every combination the trigger's clause categories produce.

    Returns
    -------
    list[Case]
        One case per point in the finite space, in a stable order.
    """
    return [
        Case(cache_mode, policy, cooling, kind, roots, gauges)
        for cache_mode, policy, cooling, (kind, roots), gauges in itertools.product(
            CACHE_MODES, POLICIES, COOLINGS, READINGS, GAUGES
        )
    ]


CASES = _cases()


@pytest.mark.parametrize(
    "case",
    CASES,
    ids=[
        f"{case.cache_mode}-{case.policy}-"
        f"{'cooled' if case.now >= case.cooling else 'cooling'}-"
        f"{case.fault_kind or 'clean'}-"
        f"roots={case.roots}-{case.gauges}"
        for case in CASES
    ],
)
def test_every_combination_of_the_trigger_categories_decides_correctly(
    case: Case,
) -> None:
    """Every point in the finite input space is visited and decided.

    This is a truth table, not a sample: the space is the product of the
    categories, so a clause that stopped discriminating would have to agree
    with the expectation here at every point rather than at the ones a
    selection happened to include.
    """
    actual = starvation(case.reading, BARRIER)
    expected = _expected(case)

    assert (actual is None) is (expected is None), (
        f"{case} should {'trigger' if expected else 'not trigger'}; got {actual!r}"
    )
    if expected is not None:
        assert actual == expected, "the reason names the evidence that authorized it"


def test_the_table_exercises_both_answers() -> None:
    """Check the table is not degenerate in either direction.

    A table that expected ``None`` everywhere would pass while the trigger
    never fired, and one that expected a reason everywhere would pass while
    every clause was dead. Both answers must appear among the cases for the
    assertions above to be evidence about the trigger.

    This is checked against the expectations rather than against the module,
    because it is the table's own discriminating power being established: an
    unfired clause would collapse the table to one answer and be caught by the
    parametrized test above, and this is what would notice that collapse when
    the failure is read.
    """
    decisions = [_expected(case) is not None for case in CASES]
    triggered = sum(decisions)

    assert triggered > 0, "the table includes combinations that must trigger"
    assert triggered < len(decisions), "the table includes combinations that must not"
    assert len(set(decisions)) == 2, "both answers are represented"


# Each question ``starvation`` asks, paired with the point that satisfies it
# and the point that fails it while leaving every other question satisfied. A
# pair differing in one field is what makes the flip attributable to that
# question rather than to the combination. The probe's pair moves ``fault_kind``
# and ``roots`` together because they are one reading, not two: the live watch
# records a root count only for a live-work fault, which ``DrainReading`` exists
# to keep out of the fixtures.
_PERMITTING: typ.Final[Case] = Case(
    "sccache", "starved", 0.0, "live_work", 0, "starving"
)
_QUESTIONS: typ.Final[dict[str, tuple[Case, Case]]] = {
    "the cache mode owns the barrier": (
        _PERMITTING,
        dc.replace(_PERMITTING, cache_mode="direct"),
    ),
    "the policy permits automatic recovery": (
        _PERMITTING,
        dc.replace(_PERMITTING, policy="manual"),
    ),
    "the cooldown has elapsed": (
        _PERMITTING,
        dc.replace(_PERMITTING, cooling=200.0),
    ),
    "the probe faulted on live work": (
        _PERMITTING,
        dc.replace(_PERMITTING, fault_kind="manager", roots=None),
    ),
    "the probe proved no visible root": (
        _PERMITTING,
        dc.replace(_PERMITTING, roots=2),
    ),
    "only the server holds cached work": (
        _PERMITTING,
        dc.replace(_PERMITTING, gauges="group-moving"),
    ),
    "a request is waiting behind them": (
        _PERMITTING,
        dc.replace(_PERMITTING, gauges="no-queue"),
    ),
}


def test_each_question_is_decided_by_cases_that_differ_only_in_that_question() -> None:
    """Check every clause has a neighbour the trigger flips on it alone.

    A clause with no such neighbour is one the table could not detect the
    absence of: the combination that must trigger would still trigger if that
    clause were deleted, so no case in the space would fail. Pairing each
    question with a neighbour differing in one field is what shows every clause
    carries its own weight rather than being redundant with the rest of the
    conjunction.

    The check runs through ``starvation`` rather than through the clause
    predicates, so it holds for a clause the module inlines as much as for one
    it extracts: what is being established is a property of the decision, not
    of how the module happens to be decomposed.
    """
    assert tuple(_QUESTIONS) == tuple(name for name, _ in _CLAUSES), (
        "the questions named here are the clauses the trigger asks, in order; a "
        "clause added without a witness would otherwise be a silent gap"
    )
    assert len(_QUESTIONS) == 7, (
        "the questions named here are the ones the trigger asks; a clause added "
        "without a witness would otherwise be a silent gap"
    )
    for question, (allowing, forbidding) in _QUESTIONS.items():
        assert starvation(allowing.reading, BARRIER) is not None, (
            f"{question}: the witness must be permitted"
        )
        assert starvation(forbidding.reading, BARRIER) is None, (
            f"{question}: the neighbour must be refused on this question alone"
        )
