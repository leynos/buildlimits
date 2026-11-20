"""Per-segment faults a full archive run records and survives.

Every planned operation runs behind one fault boundary: a closed fault
quarantines the artefact it names, any other filesystem failure is recorded as
``unreadable``, and the remaining plan still runs. These tests drive those
boundaries through a real run rather than a single operation, because the
per-operation suites cannot show that a failure leaves the rest of the plan
intact.
"""

import dataclasses as dc
import datetime as dt
import lzma
import typing as typ
from pathlib import Path

from outcome_archive_helpers import (
    NOW,
    archives,
    layout,
    policy,
    records,
    refuse,
    run,
    write,
)

from buildlimits import (
    outcome_archive_encode,
    outcome_archive_files,
    outcome_archive_run,
)
from buildlimits.outcome_archive_verify import OutputFaultError, SegmentFaultError
from buildlimits.outcome_segment_inventory import read_inventory
from buildlimits.outcome_segments import archive_name, segment_name

if typ.TYPE_CHECKING:
    import pytest

    from buildlimits.outcome_archive_context import ActionResult, ArchiveContext
    from buildlimits.outcome_segment_inventory import SegmentEntry
    from buildlimits.outcome_segments import SegmentLayout


def _restore_source(store_layout: SegmentLayout, sequence: int) -> Path:
    """Rebuild one sealed source from the archive that replaced it.

    Restoring the source models the crash window a reconciling run settles: an
    archive published while its sealed source still exists.

    Parameters
    ----------
    store_layout : SegmentLayout
        The layout of the store the archive belongs to.
    sequence : int
        The archive sequence to restore.

    Returns
    -------
    Path
        The rebuilt sealed source.
    """
    archive = store_layout.archive_dir / archive_name(sequence)
    source = store_layout.sealed_dir / segment_name(sequence)
    source.write_bytes(lzma.decompress(archive.read_bytes()))
    source.chmod(0o600)
    return source


def test_a_duplicate_pair_is_settled_by_the_run(tmp_path: Path) -> None:
    """A source left beside its archive is reconciled in a normal run."""
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 1), policy())
    run(tmp_path, policy())
    source = _restore_source(layout(tmp_path), 1)

    report = run(tmp_path, policy())

    assert report.status == "ok", "settling a duplicate is not a failure"
    assert [(action.action, action.sequence) for action in report.actions] == [
        ("reconciled", 1)
    ], "the redundant source is settled instead of archived twice"
    assert not source.exists(), "only the verified archive remains"


def test_a_failed_reconcile_is_recorded_and_the_run_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pair the run cannot settle is reported while the next pair is settled."""
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 2), policy())
    run(tmp_path, policy())
    store_layout = layout(tmp_path)
    for sequence in (1, 2):
        _restore_source(store_layout, sequence)
    real = outcome_archive_files.reconcile_duplicate

    def refuse_first(
        context: ArchiveContext, sealed: SegmentEntry, archived: SegmentEntry
    ) -> ActionResult:
        """Fail only the first pair, as a closed fault in its output would."""
        if sealed.sequence == 1:
            raise OutputFaultError("truncated_xz")
        return real(context, sealed, archived)

    monkeypatch.setattr(outcome_archive_files, "reconcile_duplicate", refuse_first)

    report = run(tmp_path, policy())

    assert (1, "truncated_xz") in report.failures, "the fault is recorded"
    assert [action.sequence for action in report.actions] == [2], (
        "the pair after the failure is still settled"
    )
    assert report.status == "corrupt", "the run names the fault it met"


def test_a_temporary_that_will_not_decode_is_not_quarantined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fault in the run's own output leaves the readable source for retry."""

    def refuse_scan(*_args: object, **_kwargs: object) -> object:
        """Fail the verification pass over the temporary archive."""
        raise SegmentFaultError("truncated_xz")

    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 1), policy())
    monkeypatch.setattr(outcome_archive_encode, "read_archive_facts", refuse_scan)

    report = run(tmp_path, policy())

    assert report.failures == ((1, "truncated_xz"),), "the output fault is reported"
    assert layout(tmp_path).sealed_dir.joinpath(segment_name(1)).exists(), (
        "the source is not quarantined"
    )
    assert not layout(tmp_path).quarantine_dir.exists(), "nothing is quarantined"


def test_an_unreadable_source_is_recorded_as_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filesystem failure during archiving is reported, not raised."""
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 1), policy())

    monkeypatch.setattr(outcome_archive_files, "archive_source", refuse)

    report = run(tmp_path, policy())

    assert report.failures == ((1, "unreadable"),), "the failure is reported"
    assert not archives(tmp_path), "nothing is published from a failed read"
    assert report.status == "corrupt", "the run names the fault it met"


def test_a_failed_delete_is_recorded_and_keeps_the_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An archive that cannot be removed is reported and left in place."""
    store = tmp_path / "cache-outcomes.jsonl"
    old = NOW - dt.timedelta(days=40)
    # The writer drops a row that arrives already expired, so expired history
    # on disk is sealed under a longer bound than the run applies.
    write(store, records(old, 1), policy(max_age_days=60))

    monkeypatch.setattr(outcome_archive_files, "delete_archive", refuse)

    report = run(tmp_path, policy(max_age_days=30))

    assert report.failures == ((1, "unreadable"),), "the failure is recorded"
    assert [action.action for action in report.actions] == ["archived"], (
        "the archive is published even though eviction failed"
    )
    assert archives(tmp_path), "the unremovable archive stays"


def test_a_record_cap_trims_an_archive_in_the_planning_run(tmp_path: Path) -> None:
    """A cap is met by deleting whole archives and trimming the straddler.

    The cap counts the rows the writer still holds in the active file, so the
    plan removes more from the archives than the closed count alone suggests.
    The trim is planned by the run's own plan, so this covers the run's trim
    step rather than the retention re-plan a heal-only run performs.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    base = policy(segment_bytes=2000)
    write(store, records(NOW, 12), base)
    active = outcome_archive_run.active_history(layout(tmp_path).active)

    report = run(tmp_path, dc.replace(base, max_records=8))

    assert report.status == "ok", "a trim is not a failure"
    expected = [("archived", sequence) for sequence in (1, 2, 3)]
    expected += [("trimmed", 2), ("deleted", 1)]
    assert [
        (action.action, action.sequence) for action in report.actions
    ] == expected, "the oldest archive goes and the straddler is trimmed"
    kept = read_inventory(layout(tmp_path)).archived
    assert [entry.record_count for entry in kept] == [2, 3], (
        "the straddler keeps exactly the rows the cap allows"
    )
    assert active.records + sum(entry.record_count or 0 for entry in kept) == 8, (
        "the active rows count towards the cap"
    )
