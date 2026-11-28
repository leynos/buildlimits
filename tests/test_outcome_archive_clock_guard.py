"""Withholding age eviction when the wall clock moved on its own.

The archiver keeps no process between firings, so it compares each reading
with a persisted baseline of boot identity, wall time and ``CLOCK_BOOTTIME``.
A firing that meets a jump of more than a day withholds the age bound, keeps
the byte and record bounds, and records its own reading so the next undisturbed
firing resumes age eviction.
"""

import datetime as dt
import json
import typing as typ
from pathlib import Path

from outcome_archive_helpers import NOW, document, layout, policy
from outcome_sealed_helpers import sealed_source, sealed_source_exists
from outcome_test_helpers import attributed_record

from buildlimits.outcome_archive import RunOptions, run_archive, run_retain
from buildlimits.outcome_archive_clock_guard import (
    ClockReading,
    is_skewed,
    read_baseline,
    write_baseline,
)
from buildlimits.outcome_segments import CLOCK_BASELINE_NAME

if typ.TYPE_CHECKING:
    import pytest

    from buildlimits.outcome_archive_report import RunReport
    from buildlimits.outcome_retention import RetentionPolicy

CUTOFF = NOW - dt.timedelta(days=30)
BOOT = "boot-one"
BOOTTIME = 10_000.0
DAY = 86_400.0
EXPIRED = attributed_record(observed_at=CUTOFF - dt.timedelta(days=1))
FRESH = attributed_record(observed_at=NOW)


def reading(skew: dt.timedelta, boottime: float, boot: str = BOOT) -> ClockReading:
    """Build one reading relative to the shared test moment.

    Returns
    -------
    ClockReading
        The reading the guard compares.
    """
    return ClockReading(boot_id=boot, wall=NOW + skew, boottime=boottime)


def test_a_jump_of_more_than_a_day_is_detected_in_either_direction() -> None:
    """Wall time that outruns or falls behind boot time is suspect."""
    base = reading(dt.timedelta(0), BOOTTIME)

    assert is_skewed(base, reading(dt.timedelta(days=2), BOOTTIME + 0.5 * DAY)), (
        "a forward jump is detected"
    )
    assert is_skewed(base, reading(dt.timedelta(days=-2), BOOTTIME + 0.5 * DAY)), (
        "a backward jump is detected"
    )
    assert not is_skewed(base, reading(dt.timedelta(hours=1), BOOTTIME + 3_540.0)), (
        "a quiet host is not suspect"
    )


def test_a_reading_without_a_comparable_baseline_is_not_suspect() -> None:
    """A missing, damaged or different-boot baseline applies no guard."""
    jumped = reading(dt.timedelta(days=3), BOOTTIME + 1.0)

    assert not is_skewed(None, jumped), "a store with no baseline is fresh"
    assert not is_skewed(reading(dt.timedelta(0), BOOTTIME, "boot-two"), jumped), (
        "boot time restarted, so the pair cannot be compared"
    )
    assert not is_skewed(ClockReading(None, NOW, BOOTTIME), jumped), (
        "a platform without a boot identity cannot be compared"
    )


def test_the_baseline_round_trips_through_its_document(tmp_path: Path) -> None:
    """The recorded reading is read back exactly."""
    store_layout = layout(tmp_path)
    recorded = reading(dt.timedelta(hours=2), BOOTTIME + 60.0)

    write_baseline(store_layout, recorded)

    assert read_baseline(store_layout) == recorded, "the reading is preserved"
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

    assert read_baseline(store_layout) is None, "the damaged record is refused"


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

        assert read_baseline(store_layout) is None, (
            f"the unrepresentable reading {boottime[:8]}... is refused"
        )


def test_a_damaged_boot_time_does_not_stop_a_firing(tmp_path: Path) -> None:
    """A firing reads a damaged baseline as absent instead of failing."""
    store_layout = layout(tmp_path)
    store_layout.archive_dir.mkdir(parents=True)
    (store_layout.archive_dir / CLOCK_BASELINE_NAME).write_text(
        '{"schema":1,"boot_id":"boot","wall":"2026-11-19T00:00:00Z","boottime":1e400}',
        encoding="utf-8",
    )
    sealed_source(tmp_path, 1, (FRESH,))

    report = retain(tmp_path, no_age(), reading=reading(dt.timedelta(0), BOOTTIME))

    assert report.status == "ok", "the damaged record applies no guard"
    assert report.clock_skew is False, "a record that cannot be read is not a jump"
    recorded = read_baseline(store_layout)
    assert recorded is not None and recorded.boottime == BOOTTIME, (
        "the firing replaces the damaged record"
    )


def retain(
    root: Path,
    retention: RetentionPolicy,
    *,
    reading: ClockReading,
    dry_run: bool = False,
) -> RunReport:
    """Run retention with the clock reading the test supplies.

    Returns
    -------
    RunReport
        The run report.
    """
    return run_retain(
        layout(root),
        retention,
        RunOptions(
            now=reading.wall,
            dry_run=dry_run,
            boot_id=lambda: reading.boot_id,
            boottime=lambda: reading.boottime,
        ),
    )


def no_age(retention_bytes: int = 0) -> RetentionPolicy:
    """Return a policy that leaves age eviction out of the way.

    Returns
    -------
    RetentionPolicy
        The fixture policy.
    """
    return policy(1_000_000, max_age_days=0, max_bytes=retention_bytes)


def _skewed_pair(root: Path) -> tuple[RunReport, RunReport]:
    """Record a baseline, then meet a forward jump on one expired source.

    Returns
    -------
    tuple[RunReport, RunReport]
        The baseline firing and the firing that met the jump.
    """
    sealed_source(root, 1, (EXPIRED,))
    baseline = retain(root, no_age(), reading=reading(dt.timedelta(0), BOOTTIME))
    skewed = retain(
        root,
        policy(1_000_000, max_age_days=30, max_bytes=0),
        reading=reading(dt.timedelta(days=2), BOOTTIME + 0.5 * DAY),
    )
    assert baseline.status == "ok", "the fixture's baseline firing completes"
    assert skewed.clock_skew is True, "the fixture's jump is detected"
    return baseline, skewed


def test_a_skewed_firing_withholds_age_eviction_and_keeps_capacity_bounds(
    tmp_path: Path,
) -> None:
    """A configured age bound is withheld; the byte bound still applies."""
    sealed_source(tmp_path, 1, (EXPIRED,))
    sealed_source(tmp_path, 2, (EXPIRED,))
    _baseline, second = _skewed_pair(tmp_path)

    assert second.actions == (), "no age eviction is attempted"
    assert sealed_source_exists(tmp_path, 1), "the expired source survives the guard"
    third = retain(
        tmp_path,
        # Both bounds are configured. The byte bound fits one source, so the
        # oldest goes and the remaining expired source stays only because the
        # age bound is withheld.
        policy(
            1_000_000,
            max_age_days=30,
            max_bytes=len(document((EXPIRED,))),
        ),
        # Another forward jump: the capacity bound is asserted while the age
        # bound is still withheld, not on a firing that no longer meets a skew.
        reading=reading(dt.timedelta(days=4), BOOTTIME + 0.5 * DAY + 3_600.0),
    )
    assert third.clock_skew is True, "the capacity bound runs on a skewed firing"
    assert third.status == "ok", "the byte bound still runs on a skewed firing"
    assert not sealed_source_exists(tmp_path, 1), (
        "the byte bound evicted the oldest source despite the clock guard"
    )
    assert sealed_source_exists(tmp_path, 2), (
        "the remaining expired source stays because the age bound was withheld"
    )


def test_the_next_undisturbed_firing_resumes_age_eviction(tmp_path: Path) -> None:
    """The skewed firing records its own reading, so the guard lasts one run."""
    _baseline, _skewed = _skewed_pair(tmp_path)

    settled = retain(
        tmp_path,
        policy(1_000_000, max_age_days=30, max_bytes=0),
        reading=reading(dt.timedelta(days=2, hours=1), BOOTTIME + 0.5 * DAY + 3_600.0),
    )

    assert settled.clock_skew is False, "the next quiet reading is trusted"
    assert not sealed_source_exists(tmp_path, 1), "age eviction resumes"


def test_a_reboot_applies_no_guard_and_records_a_new_baseline(tmp_path: Path) -> None:
    """A different boot identity cannot be compared with the old reading."""
    sealed_source(tmp_path, 1, (EXPIRED,))
    retain(tmp_path, no_age(), reading=reading(dt.timedelta(0), BOOTTIME))

    rebooted = retain(
        tmp_path,
        policy(1_000_000, max_age_days=30, max_bytes=0),
        reading=reading(dt.timedelta(days=5), 25.0, "boot-two"),
    )

    assert rebooted.clock_skew is False, "no guard is invented after a reboot"
    assert not sealed_source_exists(tmp_path, 1), "age eviction runs normally"
    recorded = read_baseline(layout(tmp_path))
    assert recorded is not None and recorded.boot_id == "boot-two", (
        "the new boot's reading is recorded"
    )


def test_a_retained_dry_run_reports_the_decision_without_withholding(
    tmp_path: Path,
) -> None:
    """A preview names the jump but plans the age eviction a real run would."""
    sealed_source(tmp_path, 1, (EXPIRED,))
    retain(tmp_path, no_age(), reading=reading(dt.timedelta(0), BOOTTIME))

    preview = retain(
        tmp_path,
        policy(1_000_000, max_age_days=30, max_bytes=0),
        reading=reading(dt.timedelta(days=2), BOOTTIME + 0.5 * DAY),
        dry_run=True,
    )

    assert preview.clock_skew is True, "the decision is reported"
    assert preview.plan is not None and preview.plan.delete == 1, (
        "the preview still plans the age eviction"
    )
    assert sealed_source_exists(tmp_path, 1), "a preview changes no file"
    recorded = read_baseline(layout(tmp_path))
    assert recorded is not None and recorded.wall == NOW, (
        "a preview does not replace the baseline"
    )


def test_an_archive_run_shares_the_same_clock_guard(tmp_path: Path) -> None:
    """The guard is a property of the firing, not of one operation."""
    sealed_source(tmp_path, 1, (EXPIRED,))
    run_archive(
        layout(tmp_path),
        no_age(),
        RunOptions(now=NOW, boot_id=lambda: BOOT, boottime=lambda: BOOTTIME),
    )
    sealed_source(tmp_path, 2, (EXPIRED,))

    report = run_archive(
        layout(tmp_path),
        policy(1_000_000, max_age_days=30, max_bytes=0),
        RunOptions(
            now=NOW + dt.timedelta(days=2),
            boot_id=lambda: BOOT,
            boottime=lambda: BOOTTIME + 0.5 * DAY,
        ),
    )

    assert report.clock_skew is True, "the archive operation reports the jump"
    assert report.plan is not None and report.plan.delete == 0, (
        "no source is removed by age"
    )
    assert all(action.action != "deleted" for action in report.actions), (
        "the second source is archived rather than expired"
    )
    assert any(action.action == "archived" for action in report.actions), (
        "archival work still runs on a skewed firing"
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
