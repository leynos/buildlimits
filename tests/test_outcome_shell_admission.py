"""Admission time and label time survive the supervisor shell's own path.

The recorder's unit contracts check the reducer directly. This test drives the
same sequence through ``OutcomeShell``: a cached admission whose ``Pending``
entry carries an admission time, a clean group ending, and one completed
statistics reading. The emitted row must carry the entry's admission moment and
the reading's own moment, so nothing downstream has to reconstruct either.
"""

import datetime as dt
import typing as typ

from differential_trace_test_helpers import CompletingStatistics, cached_entry
from outcome_shell_test_support import (
    OutcomeWorld,
    OutcomeWorldOptions,
    admitted_launch,
    make_outcome_world,
)

from buildlimits.effects import Exit
from buildlimits.outcome_record_format import OutcomeRecord
from buildlimits.supervisor_outcome_setup import GroupEnding

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from buildlimits.pending_request import Pending
    from buildlimits.supervisor_outcomes import OutcomeShell

ADMITTED_AT = dt.datetime(2026, 11, 24, 9, 30, tzinfo=dt.UTC)
"""The admission instant the labelled entry states."""

EARLIER_AT = ADMITTED_AT - dt.timedelta(minutes=10)
"""A distinct admission instant for the warm-up group.

The two differ so a genuine copy cannot be confused with a shared default.
"""

READING_AT = dt.datetime(2026, 11, 24, 9, 31, tzinfo=dt.UTC)
"""The wall-clock moment the shell's clock reports for every reading."""

# ``CompletingStatistics`` reports its pre-request baseline for the first two
# completed polls, so two groups are needed. The warm-up group consumes those
# polls and is refused; the second group's window is the first reading that
# reports the hit footprint, and that is the row this test inspects.
BASELINE_TICKS = (0.0, 0.1)
"""The ticks that complete the opening baseline reading."""

WARM_UP_TICK = 1.1
"""The tick the warm-up group launches and ends at."""

WARM_UP_READING_TICKS = (2.1, 2.2)
"""The ticks that complete the warm-up window's reading."""

LABELLED_TICK = 3.3
"""The tick the labelled group launches and ends at."""

LABELLED_READING_TICKS = (4.1, 4.2)
"""The ticks that complete the reading which labels the second group."""


def _fixed_wall_clock(moment: dt.datetime) -> cabc.Callable[[], dt.datetime]:
    """Return a clock that reports one fixed wall-clock moment.

    A closure is used rather than a callable object: the shell only ever calls
    the value, so a class would add nothing but a wrapper to forward.

    Returns
    -------
    collections.abc.Callable[[], datetime]
        A zero-argument callable returning ``moment``.
    """

    def read() -> dt.datetime:
        """Return the moment every reading reports.

        Returns
        -------
        datetime
            The fixed reading moment.
        """
        return moment

    return read


def _entry_for(group_id: str, admitted_at: dt.datetime) -> Pending:
    """Return the cached entry the shell's launch hook is given.

    The harness has no scope to place a real group in, so the entry is built
    here carrying the admission time ``Groups.place`` would have stamped and
    the group identity admission selected.

    Returns
    -------
    Pending
        The cached entry carrying the supplied admission moment.
    """
    entry = cached_entry()
    entry.group_id = group_id
    entry.admitted_at = admitted_at
    return entry


def _run_group(
    world: OutcomeWorld, shell: OutcomeShell, admitted_at: dt.datetime
) -> str:
    """Launch one cached group, end it cleanly and clear its drain.

    Returns
    -------
    str
        The group identifier admission selected.
    """
    launch = admitted_launch(world.admission, 1)
    shell.launched(_entry_for(launch.group_id, admitted_at), launch.group_id)
    world.admission.leader_exited(launch.group_id, Exit(code=0))
    world.admission.group_emptied(launch.group_id)
    shell.emptied(GroupEnding(launch.group_id, True, True))
    # A detached cache keeps the group's token in server drain until the server
    # reports no live compilation, so the quiet gate needs that cleared too.
    world.admission.server_activity(active_compiles=0)
    return launch.group_id


def _run_cached_window(tmp_path: Path) -> tuple[OutcomeRecord, OutcomeRecord]:
    """Drive two cached admissions through the shell to their emitted rows.

    Returns
    -------
    tuple[OutcomeRecord, OutcomeRecord]
        The warm-up group's row and the labelled group's row, in that order.
    """
    world = make_outcome_world(
        tmp_path,
        OutcomeWorldOptions(
            statistics_port=CompletingStatistics(footprint="hit"),
            wall_clock=_fixed_wall_clock(READING_AT),
        ),
    )
    shell = typ.cast("OutcomeShell", world.shell)
    for tick in BASELINE_TICKS:
        shell.on_tick(tick)
    shell.on_tick(WARM_UP_TICK)
    _run_group(world, shell, EARLIER_AT)
    for tick in WARM_UP_READING_TICKS:
        shell.on_tick(tick)
    shell.on_tick(LABELLED_TICK)
    _run_group(world, shell, ADMITTED_AT)
    for tick in LABELLED_READING_TICKS:
        shell.on_tick(tick)
    shell.close(timeout=0.1)
    rows = [
        record for record in world.writer.records if isinstance(record, OutcomeRecord)
    ]
    assert len(rows) == 2, "each reading emits one outcome row per group"
    return rows[0], rows[1]


def test_the_shell_carries_admission_and_reading_times_onto_its_row(
    tmp_path: Path,
) -> None:
    """A cached launch's admission time reaches the emitted store row.

    ``Groups.place`` stamps ``entry.admitted_at`` where admission is decided.
    The shell copies it onto ``Launched`` and the recorder copies it onto every
    row the reading emits. Each group keeps its own admission time, so the
    field is a per-launch observation rather than one shared value. The row's
    ``observed_at`` comes from the shell's wall clock at the reading, not from
    the launch's completion, so a label is never released before the query
    that produced it returned.
    """
    warm_up, labelled = _run_cached_window(tmp_path)

    assert warm_up.outcome == "unattributed", "the warm-up window has no movement"
    assert warm_up.observed_at == READING_AT, "even a refusal carries the reading"
    assert labelled.outcome == "hit", "the second window is attributed"
    assert labelled.admitted_at == ADMITTED_AT, "the entry's admission time is copied"
    assert labelled.admitted_at != warm_up.admitted_at, "each row keeps its own time"
    assert labelled.observed_at == READING_AT, "the reading's moment dates the label"
    assert labelled.observed_at >= ADMITTED_AT, "the decision precedes it"
