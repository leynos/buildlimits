"""The dataset command's closed-history inventory evidence.

A history read can succeed while the scan that describes the segments it could
not see fails. That is not the same as reading the active file alone: the rows
already read are in the snapshot, and only the count of what was left out is
unavailable. The command records that distinction rather than guessing, and it
reads the retention floor an archive run recorded so an intentional prefix
deletion is not reported as lost history.
"""

import typing as typ
from pathlib import Path
from unittest import mock

from segment_test_helpers import store_layout, write_private

from buildlimits import outcome_segment_inventory
from buildlimits.outcome_segments import segment_name
from buildlimits.predict import dataset_cli

if typ.TYPE_CHECKING:
    import pytest

    from buildlimits.predict.accounting import HistoryLoss

STORE = "cache-outcomes.jsonl"


def _loss_after(
    tmp_path: Path,
    *,
    sealed: tuple[int, ...] = (),
    retention: bytes | None = None,
    retention_is_a_directory: bool = False,
) -> HistoryLoss:
    """Build a store shape and return the dataset command's loss statement.

    Parameters
    ----------
    tmp_path : Path
        The directory the store lives under.
    sealed : tuple[int, ...]
        Sequences to write as sealed sources.
    retention : bytes or None
        The retention record's content, or None to leave it absent.
    retention_is_a_directory : bool
        Whether a directory stands where the retention record belongs, which
        is the unreadable shape a real filesystem fault leaves.

    Returns
    -------
    HistoryLoss
        The statement the dataset command would record for that store.
    """
    layout = store_layout(tmp_path)
    for sequence in sealed:
        write_private(layout.sealed_dir / segment_name(sequence), b"rows")
    if retention_is_a_directory:
        layout.retention_file.mkdir(parents=True)
    elif retention is not None:
        write_private(layout.retention_file, retention)
    return dataset_cli._read_inventory(_store(tmp_path))


def _store(tmp_path: Path) -> Path:
    """Return the active store path of one layout below ``tmp_path``.

    Returns
    -------
    Path
        The active store path the inventory is derived from.
    """
    return tmp_path / STORE


def test_a_failed_inventory_scan_is_not_reported_as_active_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rows already read stay; only their closed-history evidence is lost."""
    monkeypatch.setattr(
        outcome_segment_inventory,
        "read_inventory",
        mock.Mock(side_effect=OSError("the segment directory cannot be listed")),
    )

    loss = dataset_cli._read_inventory(_store(tmp_path))

    assert loss.inventory_unavailable is True, "the failure is recorded"
    assert loss.active_only is False, "a successful history read was not active-only"


def test_a_recorded_retention_floor_suppresses_intentional_gaps(
    tmp_path: Path,
) -> None:
    """A prefix archive retention deleted is not reported as lost history.

    Sequences one and four survive and the archiver recorded that three was the
    lowest sequence it kept, so only sequence three is a real gap.
    """
    loss = _loss_after(tmp_path, sealed=(1, 4), retention=b'{"floor_sequence": 3}')

    assert loss.inventory_unavailable is False, "the scan could state its counts"
    assert loss.retention_floor == 3, "the recorded floor is adopted"
    assert loss.gaps == 1, "the evicted prefix is not counted"
    assert loss.sealed == 2, "both sealed sources are counted"


def test_an_unreadable_retention_floor_withholds_the_gap_count(
    tmp_path: Path,
) -> None:
    """A floor that cannot be read leaves no trustworthy gap count.

    The store recorded a floor and the scan can no longer read it, so an absent
    sequence below the survivors could be retention's work or real damage.
    Printing either count would assert a measurement this run did not make.
    """
    loss = _loss_after(tmp_path, sealed=(1, 4), retention_is_a_directory=True)

    assert loss.inventory_unavailable is True, "the unknown floor is stated"
    assert loss.active_only is False, "the history read was not active-only"


def test_an_invalid_retention_floor_withholds_the_gap_count(tmp_path: Path) -> None:
    """A damaged floor is as untrustworthy as one that cannot be opened."""
    loss = _loss_after(tmp_path, sealed=(4,), retention=b'{"floor_sequence": "3"}')

    assert loss.inventory_unavailable is True, "the damaged record is stated"


def test_quarantined_files_are_reported_as_measured_evidence(tmp_path: Path) -> None:
    """The quarantine directory's files are counted, not reported as zero.

    ``quarantine_file`` moves corrupt history aside rather than deleting it, so
    the count of those files is the evidence an extraction did lose rows.
    """
    layout = store_layout(tmp_path)
    layout.quarantine_dir.mkdir(parents=True)
    for name in ("segment-000000000001.jsonl.corrupt.ab12cd", "orphan.json"):
        write_private(layout.quarantine_dir / name, b"evidence")

    loss = dataset_cli._read_inventory(_store(tmp_path))

    assert loss.quarantined == 2, "both quarantined files are measured"


def test_an_unlistable_quarantine_directory_withholds_the_counts(
    tmp_path: Path,
) -> None:
    """A quarantine directory that cannot be listed is unavailable evidence.

    Reporting zero would state a count this run never measured.
    """
    layout = store_layout(tmp_path)
    layout.archive_dir.mkdir(parents=True)
    write_private(layout.quarantine_dir, b"not a directory")

    loss = dataset_cli._read_inventory(_store(tmp_path))

    assert loss.inventory_unavailable is True, "the listing failure is stated"
    assert loss.quarantined == 0, "no count is invented, but none is printed either"


def test_a_fresh_store_scans_without_a_floor(tmp_path: Path) -> None:
    """A store that never recorded a floor is data, not damage.

    ``missing`` is a fresh store, so the scan runs with no floor and its counts
    are measured; treating it as unavailable would hide a real empty history.
    """
    loss = _loss_after(tmp_path, sealed=(1,))

    assert loss.inventory_unavailable is False, "the fresh store is measured"
    assert loss.retention_floor is None, "no floor is invented"
    assert loss.sealed == 1, "the one source is counted"
