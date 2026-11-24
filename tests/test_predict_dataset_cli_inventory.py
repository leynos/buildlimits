"""The dataset command's closed-history inventory evidence.

A history read can succeed while the scan that describes the segments it could
not see fails. That is not the same as reading the active file alone: the rows
already read are in the snapshot, and only the count of what was left out is
unavailable. The command records that distinction rather than guessing.
"""

import typing as typ
from unittest import mock

from buildlimits import outcome_segment_inventory
from buildlimits.predict import dataset_cli

if typ.TYPE_CHECKING:
    from pathlib import Path

    import pytest


def test_a_failed_inventory_scan_is_not_reported_as_active_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rows already read stay; only their closed-history evidence is lost."""
    monkeypatch.setattr(
        outcome_segment_inventory,
        "read_inventory",
        mock.Mock(side_effect=OSError("the segment directory cannot be listed")),
    )

    loss = dataset_cli._read_inventory(tmp_path / "cache-outcomes.jsonl")

    assert loss.inventory_unavailable is True, "the failure is recorded"
    assert loss.active_only is False, "a successful history read was not active-only"
