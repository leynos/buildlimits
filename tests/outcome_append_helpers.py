"""Store fixtures that pause one writer append inside an archive run.

Both append regressions need the same store: a published archive beside active
rows the test chose. The append itself is interrupted inside ``write_all``, so
the run observes exactly the bytes a concurrent writer leaves between a row's
first byte and its commit. Keeping the interruption here lets a test state only
what it leaves in flight and what it asserts about the run.
"""

import errno
import typing as typ

import pytest
from archive_cli_helpers import sealed_measured_source
from outcome_archive_helpers import NOW, layout, policy, records, run, write
from segment_test_helpers import document, write_private

from buildlimits import outcome_record_encode, outcome_store_io

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_archive_report import RunReport
    from buildlimits.outcome_record_format import StoreRecord
    from buildlimits.outcome_retention import RetentionPolicy
    from buildlimits.outcome_segments import SegmentLayout

__all__ = ["archived_store", "run_during_paused_append"]


def archived_store(
    root: Path,
    *,
    retained: int = 5,
    active: tuple[StoreRecord, ...] = (),
) -> SegmentLayout:
    """Build a store with one published archive and the given active rows.

    The active file is written directly so the fixture can hold rows the
    writer's own compaction would have dropped. A real run sees exactly that
    state while it plans, because the writer may not have ticked yet.

    Parameters
    ----------
    root : Path
        The store root to build.
    retained : int
        The number of rows the published archive holds.
    active : tuple[StoreRecord, ...]
        The rows the active file holds after the archive is published.

    Returns
    -------
    SegmentLayout
        The layout of the store the run should use.
    """
    store = root / "cache-outcomes.jsonl"
    write(store, (), policy(segment_bytes=1_000_000))
    sealed_measured_source(store, 1, records(NOW, retained))
    first = run(root, policy(segment_bytes=1_000_000, max_age_days=30))
    if first.status != "ok":
        pytest.fail("the fixture archive is published untrimmed")
    store_layout = layout(root)
    write_private(store_layout.active, document(active))
    return store_layout


def run_during_paused_append(
    root: Path,
    run_policy: RetentionPolicy,
    monkeypatch: pytest.MonkeyPatch,
    *,
    in_flight: bytes | None = None,
) -> RunReport:
    """Run once while an append is paused mid-write, then fail it.

    The append leaves ``in_flight`` bytes in the active file, runs the archiver
    against that file, then raises so the writer's own rollback truncates them
    back out. ``None`` leaves the whole encoded row without its newline: the
    state a writer exposes between a row's bytes and its commit.

    Parameters
    ----------
    root : Path
        The store root to run against.
    run_policy : RetentionPolicy
        The bounds to run with.
    monkeypatch : pytest.MonkeyPatch
        The fixture that injects the paused write.
    in_flight : bytes | None
        The bytes to leave in the active file, or None for the whole row body.

    Returns
    -------
    RunReport
        The report the run produced while the append was in flight.
    """
    reports: list[RunReport] = []
    real_write_all = outcome_store_io.write_all
    in_window = False

    def pause_before_newline(descriptor: int, data: bytes) -> None:
        """Write the uncommitted bytes, let the run observe them, then fail.

        Raises
        ------
        OSError
            To model the write that never reaches its newline.
        """
        nonlocal in_window
        if in_window:
            real_write_all(descriptor, data)
            return
        in_window = True
        try:
            partial = data[:-1] if in_flight is None else in_flight
            real_write_all(descriptor, partial)
            reports.append(run(root, run_policy))
        finally:
            in_window = False
        raise OSError(errno.EIO, "injected newline failure")

    monkeypatch.setattr(outcome_store_io, "write_all", pause_before_newline)
    row = records(NOW, 5)[4]

    with pytest.raises(OSError, match="injected newline failure"):
        outcome_store_io.append_line(
            layout(root).active, outcome_record_encode.encode_line(row).encode()
        )

    if len(reports) != 1:
        pytest.fail("the run did not observe the append before its newline")
    return reports[0]
