"""Reading and writing the persisted clock baseline.

The guard compares each firing with a small record of the last locked firing.
These tests pin the document, the states a reader reports when it is absent,
damaged or unreadable, and the platform clock sources that supply each reading.
"""

import datetime as dt
import json
import typing as typ
from pathlib import Path

from outcome_archive_helpers import layout
from test_outcome_archive_clock_guard import BOOTTIME, reading

from buildlimits.outcome_archive_clock_state import (
    read_baseline,
    write_baseline,
)
from buildlimits.outcome_segments import CLOCK_BASELINE_NAME

if typ.TYPE_CHECKING:
    import pytest


def test_the_baseline_round_trips_through_its_document(tmp_path: Path) -> None:
    """The recorded reading is read back exactly."""
    store_layout = layout(tmp_path)
    recorded = reading(dt.timedelta(hours=2), BOOTTIME + 60.0)

    write_baseline(store_layout, recorded)

    assert read_baseline(store_layout).reading == recorded, "the reading is preserved"
    document_ = json.loads(
        (store_layout.archive_dir / CLOCK_BASELINE_NAME).read_text(encoding="utf-8")
    )
    assert document_["schema"] == 1, "the document names its schema"


def test_a_damaged_baseline_is_treated_as_absent(tmp_path: Path) -> None:
    """A record this code did not write cannot make a firing suspect."""
    store_layout = layout(tmp_path)
    store_layout.archive_dir.mkdir(parents=True)
    (store_layout.archive_dir / CLOCK_BASELINE_NAME).write_text(
        "{not json", encoding="utf-8"
    )

    assert read_baseline(store_layout).state == "damaged", (
        "the damaged record is refused as damaged, not read as absent"
    )


def test_a_baseline_with_an_unrepresentable_boot_time_is_refused(
    tmp_path: Path,
) -> None:
    """A huge integer or an infinite exponent is damage, not an exception."""
    store_layout = layout(tmp_path)
    store_layout.archive_dir.mkdir(parents=True)
    for boottime in ("1e400", "9" * 400):
        (store_layout.archive_dir / CLOCK_BASELINE_NAME).write_text(
            '{"schema":1,"boot_id":"boot",'
            '"wall":"2026-11-19T00:00:00Z","boottime":' + boottime + "}",
            encoding="utf-8",
        )

        assert read_baseline(store_layout).state == "damaged", (
            f"the unrepresentable reading {boottime[:8]}... is refused"
        )


def test_an_unavailable_boot_identity_is_reported_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A platform without a readable boot identity names no identity."""
    from buildlimits import outcome_store_clock

    class _Unreadable:
        """A boot-identity source that refuses every read."""

        def read_text(self, **_kwargs: object) -> typ.NoReturn:
            """Refuse the read as an unreadable procfs entry would."""
            raise OSError("injected read failure")

    monkeypatch.setattr(outcome_store_clock, "BOOT_ID_PATH", _Unreadable())

    assert outcome_store_clock.system_boot_id() is None, (
        "an unreadable boot identity is unknown, not an empty string"
    )


def test_an_unavailable_boot_clock_is_reported_as_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A platform without CLOCK_BOOTTIME names no reading."""
    import time

    from buildlimits import outcome_store_clock

    def refuse(_clock: object) -> typ.NoReturn:
        """Refuse the reading as an unavailable clock would."""
        raise OSError("injected clock failure")

    monkeypatch.setattr(time, "CLOCK_BOOTTIME", 1, raising=False)
    monkeypatch.setattr(time, "clock_gettime", refuse)

    assert outcome_store_clock.system_boottime() is None, (
        "an unavailable clock is unknown, not zero"
    )


def test_a_platform_without_a_boot_clock_reads_no_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A platform without CLOCK_BOOTTIME names no reading."""
    import time

    from buildlimits import outcome_store_clock

    monkeypatch.setattr(time, "CLOCK_BOOTTIME", None, raising=False)

    assert outcome_store_clock.system_boottime() is None, (
        "an absent boot clock is unknown, not zero"
    )


def test_the_host_boot_identity_and_boot_clock_are_readable() -> None:
    """A Linux host names both the boot and its elapsed seconds."""
    from buildlimits import outcome_store_clock

    identity = outcome_store_clock.system_boot_id()
    boottime = outcome_store_clock.system_boottime()

    assert isinstance(identity, str) and identity, "the host names its boot"
    assert boottime is not None and boottime >= 0, "the boot clock has advanced"
