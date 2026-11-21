"""Generated lifecycle transitions, replayed and gap-checked after each step.

A closed segment's life is a sequence of transitions: the writer seals it, the
archiver compresses and publishes it, a crash can leave a duplicate source
beside its archive, a record budget trims or deletes it, and the age cutoff
expires it. These properties drive the real writer and archiver over generated
histories and assert after every step what an operator reads: history replays
exactly the rows retention keeps, field for field and in order; the gap list is
exactly the sequences the model's own floor leaves missing; and a repeat of the
same step performs no action. The expectation never reads the store, so a floor
that advances past a lost sequence fails the gap assertion instead of supplying
its own answer. One generated history loses a middle sealed source before the
first run, so gap reporting is exercised rather than assumed.

The schedules genuinely reach each transition: the writer seals multi-row
groups at a record cap, an archive is duplicated as a crash leaves it, a record
budget shortens a multi-row archive as well as deleting whole ones, and an age
cutoff expires a prefix. `event` records which actions each step's report
carried, so `--hypothesis-show-statistics` shows the classes a run exercised
instead of leaving coverage to be assumed.
"""

import dataclasses as dc
import datetime as dt
import lzma
import os
import typing as typ
from pathlib import Path

from hypothesis import HealthCheck, event, example, given, settings
from hypothesis import strategies as st
from outcome_archive_helpers import NOW, policy, run, write
from outcome_lifecycle_model import LifecycleModel, one_row_per_source
from outcome_test_helpers import records_at
from segment_test_helpers import write_private

from buildlimits.outcome_history import read_history
from buildlimits.outcome_segments import SegmentLayout

if typ.TYPE_CHECKING:
    from buildlimits.outcome_archive import RunReport
    from buildlimits.outcome_retention import RetentionPolicy

# The archiver reads a zero age bound as "no age limit", so an archive, trim
# or seal step asks for a bound no generated observation can reach. Only the
# expire step chooses a real cutoff.
_NO_EXPIRY_DAYS = 3_650

# Older rows let an expire step delete a prefix; recent rows survive every
# cutoff it draws, so one store can exercise both outcomes.
_OLDER_DAYS = 40
_RECENT_DAYS = 1

# A pair the archiver settles from either side: it reconciles a duplicate
# source into its verified archive, or archives the source again when the
# archive has already gone.
_SETTLING = frozenset({"reconciled", "re-archived", "archived"})

# A gap is only reportable strictly between two surviving sequences. A fresh
# store derives its floor from the lowest sequence it still holds, so a lost
# first sequence reads as a late start rather than a loss; a lost last
# sequence sits above every survivor, which is indistinguishable from a store
# that simply ended there. The generator therefore withholds a middle source,
# and only from a store with at least three of them.
_GAP_CHOICE = st.one_of(st.none(), st.integers(min_value=0, max_value=2))

# The older rows, recent rows and withheld source describe one starting store,
# so they travel as one draw and keep the property's signature within bounds.
_HISTORY = st.tuples(
    st.integers(min_value=0, max_value=3),
    st.integers(min_value=0, max_value=3),
    _GAP_CHOICE,
)

# A seal transition writes four rows under a record cap of three: the writer
# seals the first three as one source and keeps the fourth active, so a later
# record cap has a multi-row archive to shorten rather than delete whole. A
# record cap below three would leave more active rows than the cap can count,
# which the planner refuses to settle, so trim draws a cap of at least three.
_SEAL_ROWS = 4
_SEAL_RECORD_CAP = 3

_TRANSITION = st.one_of(
    st.tuples(st.just("archive"), st.just(0)),
    st.tuples(st.just("trim"), st.sampled_from([3, 4])),
    st.tuples(st.just("expire"), st.sampled_from([1, 10, 45])),
    st.tuples(st.just("seal"), st.just(0)),
    st.tuples(st.just("duplicate"), st.just(0)),
)


def _prefixed_with_seal(
    rest: list[tuple[str, int]],
) -> list[tuple[str, int]]:
    """Return a schedule that seals one multi-row source before its steps."""
    return [("seal", 0), *rest]


# Half the schedules seal a source first, so a later budget step usually has a
# multi-row archive to shorten rather than only whole sources to delete.
_SCHEDULE = st.one_of(
    st.lists(_TRANSITION, min_size=1, max_size=3),
    st.builds(_prefixed_with_seal, st.lists(_TRANSITION, min_size=0, max_size=2)),
)


@dc.dataclass(slots=True)
class _Store:
    """One generated store and the history retention should leave.

    Attributes
    ----------
    root : Path
        The directory the store and its layout live under.
    path : Path
        The store's active file path.
    layout : SegmentLayout
        The sealed, archive and quarantine directories beside the store.
    model : LifecycleModel
        The sequences, rows and floor the store should hold. The model applies
        each transition's retention rule itself, so replay and the reported
        gaps are compared with an independent expectation rather than with the
        run's own answers.
    seals : int
        How many seal transitions have run, so each one observes new times.
    """

    root: Path
    path: Path
    layout: SegmentLayout
    model: LifecycleModel
    seals: int = 0


def _build(root: Path, older: int, recent: int, choice: int | None) -> _Store:
    """Write a sealed store through the real writer and withhold one source.

    Returns
    -------
    _Store
        The store, its layout and the rows it should still replay.
    """
    root.mkdir(mode=0o700)
    path = root / "cache-outcomes.jsonl"
    rows = (
        *records_at(NOW - dt.timedelta(days=_OLDER_DAYS), older),
        *records_at(NOW - dt.timedelta(days=_RECENT_DAYS), recent),
    )
    write(path, rows, policy(segment_bytes=1, max_age_days=_NO_EXPIRY_DAYS))
    state = _Store(root, path, SegmentLayout.beside(path), one_row_per_source(rows))
    _withhold(state, choice)
    return state


def _sealed_sources(state: _Store) -> list[Path]:
    """Return the sealed source paths in sequence order.

    Returns
    -------
    list[Path]
        Every ``segment-*.jsonl`` file under the sealed directory.
    """
    return sorted(state.layout.sealed_dir.glob("segment-*.jsonl"))


def _withhold(state: _Store, choice: int | None) -> None:
    """Remove one middle sealed source, as a lost file would be missing.

    The store was written one record per segment, so the sources sort into
    sequence order and the removed file is sequence ``index + 1``. The
    counter keeps its value, so the sequence is not reused.
    """
    sources = _sealed_sources(state)
    if choice is None or len(sources) < 3:
        return
    index = 1 + choice % (len(sources) - 2)
    source = sources[index]
    metadata = source.with_name(f"{source.name}.meta.json")
    metadata.unlink(missing_ok=True)
    source.unlink()
    state.model.drop(index + 1)


def _policy_for(name: str, argument: int) -> RetentionPolicy:
    """Return the retention policy one transition runs under.

    Returns
    -------
    RetentionPolicy
        The record cap a trim needs, the cutoff an expire draws, or a policy
        that archives without evicting anything by age.
    """
    if name == "trim":
        return policy(max_age_days=_NO_EXPIRY_DAYS, max_records=argument)
    if name == "expire":
        return policy(max_age_days=argument)
    return policy(max_age_days=_NO_EXPIRY_DAYS)


def _run(state: _Store, chosen: RetentionPolicy) -> RunReport:
    """Run the archiver once at the fixed wall time.

    Returns
    -------
    RunReport
        The run's report.
    """
    return run(state.root, chosen)


def _seal_more(state: _Store) -> None:
    """Append a group the writer seals as one multi-row source.

    The byte water mark is far above a group's size, so only the record cap
    forces a seal; the cap leaves the group's last row active.
    """
    state.seals += 1
    observed = NOW + dt.timedelta(minutes=10 * state.seals)
    rows = records_at(observed, _SEAL_ROWS)
    write(
        state.path,
        rows,
        policy(
            segment_bytes=1_000_000,
            max_age_days=_NO_EXPIRY_DAYS,
            max_records=_SEAL_RECORD_CAP,
        ),
    )
    state.model.seal(rows, _SEAL_RECORD_CAP)


def _leave_duplicate(state: _Store) -> int | None:
    """Recreate a sealed source from a published archive, as a crash can.

    Returns
    -------
    int | None
        The sequence of the source that now sits beside its archive, or None
        when the store holds no rows to archive in the first place.
    """
    archives = sorted(state.layout.archive_dir.glob("segment-*.jsonl.xz"))
    if not archives:
        assert _run(state, _policy_for("archive", 0)).status == "ok", (
            "a duplicate needs an archive to duplicate"
        )
        # The archive run completes like any other and records a floor, but
        # it evicts nothing, so only the model's sequence bookkeeping changes.
        state.model.record_run(())
        archives = sorted(state.layout.archive_dir.glob("segment-*.jsonl.xz"))
    if not archives:
        return None
    archive = archives[0]
    source = state.layout.sealed_dir / archive.name.removesuffix(".xz")
    write_private(source, lzma.decompress(archive.read_bytes()))
    return int(archive.name.removeprefix("segment-").split(".", 1)[0])


def _apply_retention(model: LifecycleModel, name: str, argument: int) -> set[int]:
    """Apply one transition's retention rule to the model.

    An age cutoff expires entries observed before it, and a record budget
    keeps the newest rows after deleting whole entries and shortening the
    first that does not fit. Every other transition evicts nothing.

    Returns
    -------
    set[int]
        The sequences the transition deletes whole.
    """
    if name == "trim":
        return model.trim(argument)
    if name == "expire":
        return model.expire(NOW - dt.timedelta(days=argument))
    return set()


def _assert_observable(state: _Store, report: RunReport) -> None:
    """Assert replay and the report's gaps match the independent model."""
    assert read_history(state.layout).records == tuple(state.model.records), (
        "every row retention keeps replays once, in sequence order"
    )
    assert report.gaps == state.model.gaps, (
        "the report names exactly the sequences the model finds missing"
    )
    assert report.gaps_truncated is False, "a generated history fits the gap list"


def _step(state: _Store, transition: tuple[str, int]) -> tuple[str, ...]:
    """Apply one transition, then require it to settle and stay settled.

    The model applies the transition's own retention rule and records the
    floor the completed run should leave before replay and gaps are compared
    with it, so neither expectation reads the store.

    Returns
    -------
    tuple[str, ...]
        The action names the run reported, in the order it took them.
    """
    name, argument = transition
    duplicated: int | None = None
    match name:
        case "seal":
            _seal_more(state)
        case "duplicate":
            duplicated = _leave_duplicate(state)
    chosen = _policy_for(name, argument)

    report = _run(state, chosen)

    assert report.status == "ok", "every generated transition completes"
    state.model.record_run(_apply_retention(state.model, name, argument))
    if duplicated is not None:
        settled = any(
            action.sequence == duplicated and action.action in _SETTLING
            for action in report.actions
        )
        assert settled, "the duplicate source is settled by the run that follows it"
    _assert_observable(state, report)

    again = _run(state, chosen)

    assert again.status == "ok", "a repeated firing succeeds"
    assert not again.actions, "a settled store performs no action on a repeat"
    _assert_observable(state, again)
    return tuple(action.action for action in report.actions)


@given(history=_HISTORY, schedule=_SCHEDULE)
@example(history=(2, 2, None), schedule=[("archive", 0)])
@example(history=(2, 2, 0), schedule=[("archive", 0)])
@example(history=(2, 2, None), schedule=[("seal", 0)])
@example(history=(2, 2, None), schedule=[("archive", 0), ("trim", 3)])
@example(history=(3, 0, None), schedule=[("expire", 10)])
@example(history=(2, 1, None), schedule=[("archive", 0), ("duplicate", 0)])
@example(history=(1, 0, None), schedule=[("seal", 0), ("expire", 10)])
@example(history=(0, 0, None), schedule=[("seal", 0), ("archive", 0), ("trim", 3)])
# A floor is recorded once and then advanced only past a contiguous deleted
# prefix, so a withheld sequence stays reportable through later steps.
@example(history=(3, 0, 2), schedule=[("seal", 0), ("expire", 10), ("trim", 3)])
@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_generated_lifecycles_settle_and_report_gaps(
    tmp_path: Path,
    history: tuple[int, int, int | None],
    schedule: list[tuple[str, int]],
) -> None:
    """Every generated lifecycle transition leaves replay and gaps consistent."""
    state = _build(tmp_path / os.urandom(4).hex(), *history)

    for transition in schedule:
        name, _ = transition
        actions = _step(state, transition)
        event(name, tuple(sorted(set(actions))))


def test_each_transition_class_is_reachable(tmp_path: Path) -> None:
    """Each transition in the schedules reports the action it names.

    The generated property records which classes a run happened to draw; this
    fixed sequence proves every class is reachable, so a schedule that never
    draws one is a coverage gap rather than an unreachable state.
    """
    state = _build(tmp_path / "lifecycle", older=2, recent=1, choice=None)

    assert _step(state, ("archive", 0)) == ("archived",) * 3, (
        "archiving publishes each sealed source"
    )
    assert _step(state, ("duplicate", 0)) == ("reconciled",), (
        "a duplicate source is reconciled with its archive"
    )
    assert _step(state, ("seal", 0)) == ("archived",), (
        "a source the writer seals is archived next"
    )
    assert _step(state, ("trim", 3)) == (
        "deleted",
        "deleted",
        "deleted",
        "trimmed",
    ), "a budget deletes whole sources and then shortens one"

    expiring = _build(tmp_path / "expiry", older=3, recent=0, choice=None)

    assert _step(expiring, ("expire", 10)) == ("deleted",) * 3, (
        "an age cutoff expires every old source"
    )
