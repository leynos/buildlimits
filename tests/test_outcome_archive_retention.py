"""Retention and history integrity across an end-to-end archive run.

One timer firing both compresses sealed history and applies the age and byte
policy to the result. These tests pin that the archive shrinks, that a repeat
run is a no-op, that eviction settles in the same run, and that the replayable
history is identical before and after compression.
"""

import datetime as dt
import errno
import json
import lzma
import os
import typing as typ
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from outcome_archive_helpers import (
    NOW,
    archives,
    document,
    layout,
    policy,
    records,
    run,
    write,
)
from segment_test_helpers import write_private

from buildlimits import outcome_archive_files, outcome_history
from buildlimits.outcome_segment_inventory import SegmentEntry, read_inventory
from buildlimits.outcome_segments import (
    SegmentLayout,
    archive_name,
    segment_name,
    sidecar_name,
)

if typ.TYPE_CHECKING:
    import pytest

    from buildlimits.outcome_archive_context import ActionResult, ArchiveContext


def test_archiving_releases_every_sealed_source(tmp_path: Path) -> None:
    """The first run compresses every source and leaves the archive directory."""
    store = tmp_path / "cache-outcomes.jsonl"
    rows = records(NOW, 3)
    write(store, rows, policy())
    store_layout = layout(tmp_path)
    logical = sum(
        path.stat().st_size for path in store_layout.sealed_dir.glob("*.jsonl")
    )
    active = store_layout.active.stat().st_size

    report = run(tmp_path, policy())

    assert report.status == "ok", "the run succeeds"
    assert [action.action for action in report.actions] == ["archived"] * 3, (
        "each source is archived once"
    )
    assert not list(store_layout.sealed_dir.glob("*.jsonl")), "no source remains"
    assert len(archives(tmp_path)) == 3, "every segment has an archive"
    assert report.retained_bytes == logical + active, (
        "the report counts logical history and the active header, not XZ bytes"
    )
    assert report.retained_bytes > sum(
        path.stat().st_size for path in archives(tmp_path)
    ), "the XZ archives are smaller than the history they hold"


def test_a_second_run_finds_no_work(tmp_path: Path) -> None:
    """Archiving is idempotent: a repeat run neither rewrites nor deletes."""
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 2), policy())
    run(tmp_path, policy())
    before = {path.name: path.read_bytes() for path in archives(tmp_path)}

    report = run(tmp_path, policy())

    assert report.status == "ok", "the second run succeeds"
    assert not report.actions, "it has nothing to do"
    assert {path.name: path.read_bytes() for path in archives(tmp_path)} == before, (
        "the archives are untouched"
    )


def test_age_eviction_removes_expired_sources_without_archiving(
    tmp_path: Path,
) -> None:
    """A wholly expired source is evicted, not compressed only to be deleted.

    Compressing history the policy has already expired would spend the run's
    work on an archive that the same plan removes.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    old = NOW - dt.timedelta(days=40)
    # The writer enforces the age bound on its own appends, so the fixture is
    # sealed under a policy that keeps it; the archiver then applies the
    # shorter bound this test is about.
    write(store, records(old, 2), policy(max_age_days=60))

    report = run(tmp_path, policy(max_age_days=30))

    assert report.status == "ok", "expiry is not a failure"
    assert [action.action for action in report.actions] == ["deleted", "deleted"], (
        "both expired sources are removed whole"
    )
    assert not archives(tmp_path), "no expired history remains"
    assert not read_inventory(layout(tmp_path)).sealed, "no source remains"
    assert (
        json.loads(layout(tmp_path).retention_file.read_text(encoding="utf-8"))[
            "floor_sequence"
        ]
        == 3
    ), "the floor starts past the sequences retention removed"


def test_a_heal_only_run_replans_retention_with_real_logical_bytes(
    tmp_path: Path,
) -> None:
    """A repaired sidecar lets the same run see the bytes the archive holds.

    The first plan had to fall back to the archive's physical size, which
    understates what it represents, so the budget is only true after the
    repaired inventory is planned again.
    """
    store_layout = layout(tmp_path)
    store_layout.sealed_dir.mkdir(parents=True)
    for sequence in (1, 2):
        write_private(
            store_layout.sealed_dir / segment_name(sequence),
            document(records(NOW, 20)),
        )
    run(tmp_path, policy())
    first, second = read_inventory(store_layout).archived
    (store_layout.archive_dir / sidecar_name(first.sequence)).unlink()
    # The physical-size plan fits this budget, so only the repaired inventory
    # shows an excess; half of the first archive is then trimmed away.
    budget = first.logical_bytes // 2 + second.logical_bytes
    assert first.physical_bytes < budget < first.logical_bytes + second.logical_bytes, (
        "the fixture needs a budget between the two estimates"
    )

    report = run(tmp_path, policy(max_bytes=budget))

    actions = [(action.action, action.sequence) for action in report.actions]
    assert ("repaired", first.sequence) in actions, "the sidecar is rebuilt"
    assert ("trimmed", first.sequence) in actions, (
        "the healed bytes change the plan in the same run"
    )
    assert report.retained_bytes <= budget, "the budget is met in one firing"


def test_a_record_cap_evicts_the_oldest_sealed_source_in_one_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source older than a published archive is evicted, not compressed.

    The first run cannot archive sequence 1, so it leaves a sealed source
    beside the newer archive it publishes. A cap of one row must then drop the
    older source whole; compressing it first would keep the older row and
    delete the newer archive instead.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 2), policy())
    real = outcome_archive_files.archive_source

    def refuse_first(context: ArchiveContext, entry: SegmentEntry) -> ActionResult:
        """Archive every source but the oldest."""
        if entry.sequence == 1:
            raise OSError(errno.EIO, "injected failure")
        return real(context, entry)

    monkeypatch.setattr(outcome_archive_files, "archive_source", refuse_first)
    first = run(tmp_path, policy())

    assert [(action.action, action.sequence) for action in first.actions] == [
        ("archived", 2)
    ], "the fixture leaves the older source sealed"

    monkeypatch.setattr(outcome_archive_files, "archive_source", real)
    report = run(tmp_path, policy(max_records=1))

    assert [(action.action, action.sequence) for action in report.actions] == [
        ("deleted", 1)
    ], "the older source is evicted without being archived"
    assert not list(layout(tmp_path).sealed_dir.glob("*.jsonl")), "no source remains"
    assert [path.name for path in archives(tmp_path)] == [archive_name(2)], (
        "the newer archive survives"
    )


def test_byte_eviction_deletes_the_oldest_archive_first(tmp_path: Path) -> None:
    """A byte budget smaller than the history removes whole oldest archives."""
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 3), policy())
    run(tmp_path, policy())
    one_segment = read_inventory(layout(tmp_path)).archived[0].logical_bytes
    store_layout = layout(tmp_path)
    # The budget counts the active file's header too, so include it when
    # choosing an exact budget: only the oldest archive then has to go.
    active = store_layout.active.stat().st_size

    report = run(tmp_path, policy(max_bytes=one_segment * 2 + active))

    assert report.status == "ok", "eviction is a successful run"
    assert [
        action.sequence for action in report.actions if action.action == "deleted"
    ] == [1], "the oldest archive alone is evicted"
    assert [path.name for path in archives(tmp_path)] == [
        archive_name(2),
        archive_name(3),
    ], "the newest two archives remain"
    assert store_layout.archive_dir.joinpath(sidecar_name(1)).exists() is False, (
        "the evicted sidecar is gone"
    )
    assert report.retained_bytes == one_segment * 2 + active, (
        "the budget is met exactly"
    )


def test_history_is_identical_before_and_after_archiving(tmp_path: Path) -> None:
    """Compression changes the encoding, never the replayable history."""
    store = tmp_path / "cache-outcomes.jsonl"
    rows = records(NOW, 4)
    write(store, rows, policy())
    before = outcome_history.read_history_beside(store)

    run(tmp_path, policy())
    after = outcome_history.read_history_beside(store)

    assert len(before.records) == 4, "the store holds four rows"
    assert before.records == after.records, "every field and the order are unchanged"
    assert before.header == after.header, "the salt domain and header survive"


@given(
    salt_id=st.text(alphabet="abc", min_size=1, max_size=4),
    counts=st.lists(st.integers(min_value=0, max_value=4), min_size=1, max_size=3),
)
@settings(
    max_examples=10,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
def test_archiving_preserves_every_generated_document(
    tmp_path: Path,
    salt_id: str,
    counts: list[int],
) -> None:
    """Every generated segment decompresses to exactly the bytes it held."""
    root = tmp_path / os.urandom(4).hex()
    store_layout = SegmentLayout.beside(root / "cache-outcomes.jsonl")
    store_layout.sealed_dir.mkdir(parents=True)
    documents: dict[int, bytes] = {}
    for sequence, count in enumerate(counts, start=1):
        source = document(records(NOW, count), salt_id)
        path = store_layout.sealed_dir / archive_name(sequence).removesuffix(".xz")
        path.write_bytes(source)
        path.chmod(0o600)
        documents[sequence] = source

    report = run(root, policy(max_age_days=0))

    assert report.status == "ok", "every generated document is archivable"
    assert len(archives(root)) == len(counts), "one archive per segment"
    for sequence, source in documents.items():
        archived = store_layout.archive_dir / archive_name(sequence)
        assert lzma.decompress(archived.read_bytes()) == source, (
            "the archive holds the source bytes unchanged"
        )
    assert not read_inventory(store_layout).sealed, "no source is left behind"
