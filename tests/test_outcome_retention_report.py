"""The machine-readable retention limits and retained-history facts.

These are the values an operator compares between supervisor status and the
archive command's report. The tests pin the exact fields, the byte accounting
and the completeness marker, so a diagnostic can never present a lower bound
as an exact retained count.
"""

import datetime as dt
import json
import typing as typ
from pathlib import Path

from outcome_retention_helpers import settings

from buildlimits.outcome_retention import (
    LEGACY_MAX_RECORDS,
    policy_from_settings,
)
from buildlimits.outcome_retention_history import retained_history
from buildlimits.outcome_retention_report import ActiveHistory, RetentionLimits
from buildlimits.outcome_segment_inventory import Inventory, SegmentEntry

if typ.TYPE_CHECKING:
    from syrupy.assertion import SnapshotAssertion

    from buildlimits import config

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)
ROHGA_MAX_BYTES = 15_000_000_000


def _limits(loaded: config.SupervisorConfig) -> RetentionLimits:
    """Derive the reported limits from one setting set.

    Returns
    -------
    RetentionLimits
        The effective limits.
    """
    return RetentionLimits.of(policy_from_settings(loaded))


def test_omitted_retention_keys_keep_the_legacy_policy(
    tmp_path: Path, snapshot: SnapshotAssertion
) -> None:
    """A deployment that writes no retention key sees the old store shape."""
    limits = _limits(settings(tmp_path))

    assert limits.as_dict() == snapshot, (
        "the defaults agree with the documented legacy policy"
    )
    assert limits.max_records == LEGACY_MAX_RECORDS, (
        "the historical 10,000-row bound is still reported"
    )
    assert limits.segmented is False, "legacy mode still compacts in place"


def test_the_rohga_policy_is_reported_exactly(
    tmp_path: Path, snapshot: SnapshotAssertion
) -> None:
    """Thirty days and fifteen gigabytes need no record cap or patching."""
    limits = _limits(
        settings(
            tmp_path,
            "cache_outcomes_max_age_days = 30\n"
            "cache_outcomes_max_bytes = 15000000000\n"
            "cache_outcomes_max_records = 0\n",
        )
    )

    assert limits.as_dict() == snapshot, (
        "the requested policy is reported without a hidden count cap"
    )
    assert limits.max_bytes == ROHGA_MAX_BYTES, "bytes are decimal, not binary"
    assert limits.max_records is None, "no count cap truncates the history"


def test_an_explicit_record_cap_is_reported(tmp_path: Path) -> None:
    """A positive cap applies even when the store is segmented."""
    limits = _limits(
        settings(
            tmp_path,
            "cache_outcomes_max_bytes = 15000000000\n"
            "cache_outcomes_max_records = 50000\n",
        )
    )

    assert limits.max_records == 50_000, "the explicit cap is effective"


def test_zero_bounds_disable_their_limits(tmp_path: Path) -> None:
    """Zero age and zero bytes are reported as disabled, not as eviction."""
    limits = _limits(
        settings(
            tmp_path,
            "cache_outcomes_max_age_days = 0\ncache_outcomes_max_bytes = 0\n",
        )
    )

    assert limits.age_eviction is False, "zero days disables age eviction"
    assert limits.byte_eviction is False, "zero bytes disables byte eviction"
    assert limits.segmented is True, "an unbounded file still rotates"
    assert limits.max_age_seconds == 0, "the exact seconds are reported"


def test_retained_history_counts_every_state(tmp_path: Path) -> None:
    """Active, sealed and archived history contribute to one retained view."""
    sealed = SegmentEntry(
        sequence=1,
        path=tmp_path / "sealed",
        state="sealed",
        physical_bytes=500,
        logical_bytes=500,
        record_count=2,
        first_observed_at=NOW - dt.timedelta(hours=5),
        last_observed_at=NOW - dt.timedelta(hours=4),
        has_metadata=True,
    )
    archived = SegmentEntry(
        sequence=2,
        path=tmp_path / "archived",
        state="archived",
        physical_bytes=100,
        logical_bytes=900,
        record_count=5,
        first_observed_at=NOW - dt.timedelta(hours=10),
        last_observed_at=NOW - dt.timedelta(hours=9),
        has_metadata=True,
    )
    active = ActiveHistory(
        record_count=1,
        logical_bytes=120,
        first_observed_at=NOW - dt.timedelta(hours=2),
        last_observed_at=NOW,
    )

    history = retained_history(
        Inventory(sealed=(sealed,), archived=(archived,)), active
    )

    assert history.record_count == 8, "every retained row is counted"
    assert history.logical_bytes == 1520, "logical bytes, not XZ bytes, are summed"
    assert history.active_bytes == 120, "the active share is reported"
    assert history.sealed_bytes == 500, "the sealed share is reported"
    assert history.archived_logical_bytes == 900, "the archived share is reported"
    assert history.archived_physical_bytes == 100, "compression is reported apart"
    assert history.closed_segments == 2, "both closed states are counted"
    assert history.active_records == 1, "active rows are separable"
    assert history.span_seconds == 36_000, "the span covers all states"
    assert history.complete is True, "every row and time was known"


def test_missing_metadata_makes_the_count_incomplete(tmp_path: Path) -> None:
    """A retained segment without metadata keeps its bytes but hides its rows."""
    archived = SegmentEntry(
        sequence=1,
        path=tmp_path / "archived",
        state="archived",
        physical_bytes=100,
        logical_bytes=100,
        has_metadata=False,
    )
    active = ActiveHistory(
        record_count=1,
        logical_bytes=120,
        first_observed_at=NOW,
        last_observed_at=NOW,
    )

    history = retained_history(Inventory(archived=(archived,)), active)

    assert history.record_count == 1, "only the measured row is counted"
    assert history.logical_bytes == 220, "the unmeasured bytes are still retained"
    assert history.complete is False, "the lower bound is labelled as such"


def test_an_unreadable_active_file_reports_its_reason() -> None:
    """An incomplete snapshot carries the closed reason a reader can act on."""
    history = retained_history(
        Inventory(),
        ActiveHistory(logical_bytes=900, complete=False, reason="corrupt"),
    )

    assert history.logical_bytes == 900, "the file size is still a fact"
    assert history.record_count == 0, "no row is invented"
    assert history.complete is False, "the snapshot is incomplete"
    assert history.span_seconds is None, "no span is invented"


def test_an_unmeasured_active_file_leaves_the_result_incomplete() -> None:
    """An unmeasured active file is unknown history, not an empty one."""
    history = retained_history(Inventory())

    assert history.record_count == 0, "no row is invented"
    assert history.active_bytes == 0, "no active bytes are attributed"
    assert history.complete is False, "the missing measurement is disclosed"


def test_the_retained_document_is_closed_json(
    snapshot: SnapshotAssertion,
) -> None:
    """The retained view round-trips as JSON with exactly its documented keys."""
    history = retained_history(Inventory())

    rendered = history.as_dict()

    assert json.loads(json.dumps(rendered, sort_keys=True)) == rendered, (
        "every reported field is JSON-compatible"
    )
    assert sorted(rendered) == snapshot, "the retained schema is closed"


def test_the_limits_document_is_closed_json(
    tmp_path: Path, snapshot: SnapshotAssertion
) -> None:
    """The limits view round-trips as JSON with exactly its documented keys."""
    rendered = _limits(settings(tmp_path)).as_dict()

    assert json.loads(json.dumps(rendered, sort_keys=True)) == rendered, (
        "every reported field is JSON-compatible"
    )
    assert sorted(rendered) == snapshot, "the limits schema is closed"
