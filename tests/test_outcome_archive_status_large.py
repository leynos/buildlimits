"""The status report stays bounded while a large archive run holds its lock.

Retention maintenance for closed history runs in the archiver, never in the
supervisor, so the inspection command must report retained history without
decoding it. This is the diagnostics half of the large-history acceptance
validation: it is opt-in because generating the store takes minutes.
"""

import json
import os
import subprocess
import typing as typ
from pathlib import Path

import pytest
from archive_cli_helpers import SCRIPT, command_arguments
from test_outcome_archive_memory import (
    _LARGE_ENV,
    _RSS_KIB,
    large_store_and_config,
    measure_large_command,
)

from buildlimits.outcome_archive_lock import ArchiveLock
from buildlimits.outcome_segments import SegmentLayout


def _inspection_report(store: Path, config: Path) -> dict[str, object]:
    """Return the JSON diagnostic an inspection prints for the large store.

    Returns
    -------
    dict[str, object]
        The report fields the command publishes.
    """
    completed = subprocess.run(
        [str(SCRIPT), "inspect", *command_arguments(store, config), "--json"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return typ.cast("dict[str, object]", json.loads(completed.stdout))


@pytest.mark.skipif(
    os.environ.get(_LARGE_ENV) != "1",
    reason=f"set {_LARGE_ENV}=1 to run the several-hundred-megabyte test",
)
@pytest.mark.timeout(1_800)
def test_a_large_history_keeps_the_status_read_bounded_during_maintenance(
    tmp_path: Path,
) -> None:
    """The inspection command stays cheap while a large run holds its lock.

    The command reports the effective limits and retained totals from
    metadata rather than by decoding the archived history, and it takes no
    archive lock, so it completes while another process is maintaining that
    history. The measured peak stays far below the history it describes.
    """
    store, config = large_store_and_config(tmp_path)
    layout = SegmentLayout.beside(store)

    with ArchiveLock(layout.lock_file):
        status, status_rss = measure_large_command(store, config, "inspect")

    assert status == 0, "the inspection completes while an archive run holds the lock"
    assert status_rss < _RSS_KIB, f"the status read peaked at {status_rss} KiB"

    report = _inspection_report(store, config)
    limits = report["limits"]
    assert report["status"] == "ok", "the large history is reportable"
    assert report["retained_logical_bytes"] > 0, "the report names retained bytes"
    assert report["retained_records"] > 0, "the report names retained records"
    assert isinstance(limits, dict), "the report names the effective limits"
    assert limits["max_bytes"] == 15_000_000_000, "the configured budget is reported"
    assert limits["max_records"] is None, "the disabled count cap is reported"
