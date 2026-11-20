"""The timer-facing command's output, configuration and exit statuses.

A systemd timer records only the process status and the journal lines, so the
command must map every outcome to a closed status, keep paths out of its report,
and read the same configuration file as the supervisor. These tests exercise
the command through ``main`` so argument parsing and rendering are included.
"""

import datetime as dt
import json
import re
import typing as typ
from pathlib import Path

import pytest
from outcome_test_helpers import attributed_record
from segment_test_helpers import document as encode_document

from buildlimits.outcome_archive_cli import main
from buildlimits.outcome_archive_lock import ArchiveLock
from buildlimits.outcome_segments import (
    SegmentLayout,
    archive_name,
    segment_name,
    sidecar_name,
)

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

    from buildlimits.outcome_record_format import StoreRecord

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)


def _records(count: int) -> tuple[StoreRecord, ...]:
    """Build ``count`` records one minute apart.

    Returns
    -------
    tuple[StoreRecord, ...]
        The records, oldest first.
    """
    return tuple(
        attributed_record(observed_at=NOW + dt.timedelta(minutes=index))
        for index in range(count)
    )


def _sealed(store: Path, sequence: int, data: bytes) -> Path:
    """Write one sealed source beside ``store``.

    Returns
    -------
    Path
        The source path.
    """
    directory = SegmentLayout.beside(store).sealed_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / segment_name(sequence)
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def _config(
    path: Path,
    *,
    preset: int = 0,
    max_bytes: int = 15_000_000_000,
) -> Path:
    """Write a configuration that enables recorded, archived outcomes.

    Returns
    -------
    Path
        The configuration path.
    """
    path.write_text(
        "\n".join([
            'cache_outcomes = "record"',
            'cache_mode = "sccache"',
            # The fixtures are dated, so a real age window would start evicting
            # them once the wall clock passed it. Zero disables age eviction.
            "cache_outcomes_max_age_days = 0",
            f"cache_outcomes_max_bytes = {max_bytes}",
            f"cache_outcomes_archive_preset = {preset}",
            "",
        ])
    )
    return path


def _arguments(store: Path, config: Path) -> list[str]:
    """Return the options every command shares.

    Returns
    -------
    list[str]
        The store and configuration arguments.
    """
    return ["--store", str(store), "--config", str(config)]


def test_inspect_reports_the_plan_as_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Inspection is a dry run and its JSON is free of host detail."""
    store = tmp_path / "cache-outcomes.jsonl"
    _sealed(store, 1, encode_document(_records(1)))
    config = _config(tmp_path / "supervisor.toml")

    status = main(["inspect", *_arguments(store, config), "--json"])
    output = capsys.readouterr().out

    document = json.loads(output)
    assert status == 0, "inspection succeeds"
    assert document["status"] == "ok", "the store is healthy"
    assert document["dry_run"] is True, "inspection never executes"
    assert document["plan"]["archive"] == 1, "the pending source is planned"
    assert str(tmp_path) not in output, "the report names no path"


def test_inspect_prints_bounded_operator_lines(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    snapshot: SnapshotAssertion,
) -> None:
    """The plain report carries the plan and the dry-run marker."""
    store = tmp_path / "cache-outcomes.jsonl"
    _sealed(store, 1, encode_document(_records(1)))
    config = _config(tmp_path / "supervisor.toml")

    status = main(["inspect", *_arguments(store, config)])
    output = capsys.readouterr().out
    # The command reads the system clock; the cutoff is the only field that
    # changes between runs, so redact it before snapshotting the contract.
    normalized = re.sub(r"(?m)^cutoff: .*$", "cutoff: <utc now>", output)

    assert status == 0, "inspection succeeds"
    assert normalized == snapshot, "the plain report is a closed operator contract"


def test_run_archives_and_reports_the_action(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A successful run publishes the archive and prints what it did."""
    store = tmp_path / "cache-outcomes.jsonl"
    _sealed(store, 1, encode_document(_records(1)))
    config = _config(tmp_path / "supervisor.toml")

    status = main(["run", *_arguments(store, config)])
    output = capsys.readouterr().out

    assert status == 0, "the run succeeds"
    assert "status: ok" in output, "the status is printed"
    assert "action archived: 1" in output, "the action is counted, not detailed"
    assert (SegmentLayout.beside(store).archive_dir / archive_name(1)).exists(), (
        "the archive is published"
    )


def test_the_configured_preset_is_used(
    tmp_path: Path,
) -> None:
    """The command reads the archive preset from the supervisor config."""
    store = tmp_path / "cache-outcomes.jsonl"
    _sealed(store, 1, encode_document(_records(1)))
    config = _config(tmp_path / "supervisor.toml", preset=1)

    main(["run", *_arguments(store, config)])

    sidecar = SegmentLayout.beside(store).archive_dir / sidecar_name(1)
    assert '"xz_preset":1' in sidecar.read_text(), "the configured preset is used"


def test_run_dry_run_changes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``run --dry-run`` plans without taking the lock or writing state."""
    store = tmp_path / "cache-outcomes.jsonl"
    source = _sealed(store, 1, encode_document(_records(1)))
    config = _config(tmp_path / "supervisor.toml")

    status = main(["run", "--dry-run", *_arguments(store, config)])
    output = capsys.readouterr().out

    assert status == 0, "a dry run succeeds"
    assert "dry run: no file was changed" in output, "the marker is explicit"
    assert source.exists(), "the source is untouched"
    assert not SegmentLayout.beside(store).archive_dir.exists(), (
        "no archive state is created"
    )


def test_a_corrupt_source_exits_three(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A segment that cannot be decoded is an operator-actionable failure."""
    store = tmp_path / "cache-outcomes.jsonl"
    _sealed(store, 1, b"not a header\n")
    config = _config(tmp_path / "supervisor.toml")

    status = main(["run", *_arguments(store, config)])
    output = capsys.readouterr().out

    assert status == 3, "corruption has its own exit status"
    assert "status: corrupt" in output, "the status is printed"
    assert "failure: sequence 1: corrupt" in output, "the reason is bounded"


def test_a_held_lock_exits_seventy_five(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A concurrent run is retryable, never waited on."""
    store = tmp_path / "cache-outcomes.jsonl"
    _sealed(store, 1, encode_document(_records(1)))
    config = _config(tmp_path / "supervisor.toml")
    layout = SegmentLayout.beside(store)

    with ArchiveLock(layout.lock_file):
        status = main(["run", *_arguments(store, config)])
    output = capsys.readouterr().out

    assert status == 75, "the timer records a temporary failure"
    assert "status: busy" in output, "the status is printed"
    assert "reason: busy" in output, "the reason is closed"


def test_a_missing_configuration_uses_defaults(tmp_path: Path) -> None:
    """An absent configuration file is not an error."""
    store = tmp_path / "cache-outcomes.jsonl"
    _sealed(store, 1, encode_document(_records(1)))

    status = main([
        "inspect",
        "--store",
        str(store),
        "--config",
        str(tmp_path / "absent.toml"),
    ])

    assert status == 0, "the defaults apply"


def test_an_unusable_configuration_value_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A retention setting without record mode is rejected before a run."""
    store = tmp_path / "cache-outcomes.jsonl"
    config = tmp_path / "supervisor.toml"
    config.write_text("cache_outcomes_max_bytes = 1\n")

    status = main(["inspect", *_arguments(store, config)])
    error = capsys.readouterr().err

    assert status == 2, "the configuration is unusable"
    assert "cache_outcomes" in error, "the rejected key is named"


def test_an_unreadable_configuration_names_its_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The configuration error is the one documented path exception."""
    store = tmp_path / "cache-outcomes.jsonl"
    directory = tmp_path / "config-directory"
    directory.mkdir()

    status = main(["inspect", *_arguments(store, directory)])
    error = capsys.readouterr().err

    assert status == 2, "an unreadable configuration is a usage error"
    assert str(directory) in error, "the operator learns which file failed"


def test_a_store_without_an_environment_is_a_usage_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With no ``--store`` and no usable environment there is nothing to read."""
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("HOME", raising=False)
    config = _config(tmp_path / "supervisor.toml")

    status = main(["inspect", "--config", str(config)])
    error = capsys.readouterr().err

    assert status == 2, "the command cannot resolve a store"
    assert "absolute HOME" in error, "the missing input is named"


@pytest.mark.parametrize("arguments", [[], ["unknown"]], ids=["missing", "unknown"])
def test_a_missing_or_unknown_subcommand_is_usage(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """The parser rejects an incomplete or unknown command with status two."""
    status = main(arguments)

    assert status == 2, "the command is not runnable"
    assert capsys.readouterr().err, "the usage message is printed"
