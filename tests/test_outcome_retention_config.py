"""Retention settings are validated before any record can be deleted.

The configuration is the only place an operator states a retention policy, so
every rejection here happens before the archive command opens a store. A test
for each class of mistake pins the message an operator will read, and the final
test proves a refused policy leaves recorded history untouched.
"""

from pathlib import Path

import pytest
from outcome_archive_helpers import NOW, records
from outcome_retention_helpers import settings
from segment_test_helpers import document, store_layout, write_private

from buildlimits import config, outcome_archive_cli
from buildlimits.outcome_record_encode import header_floor_bytes
from buildlimits.outcome_record_format import MAX_COMPACT_BYTES
from buildlimits.outcome_archive_exit import EXIT_USAGE
from buildlimits.outcome_retention import (
    DEFAULT_HEADROOM_BYTES,
    DEFAULT_MAX_AGE_DAYS,
    LEGACY_MAX_BYTES,
    LEGACY_MAX_RECORDS,
    MAX_AGE_DAYS,
    MAX_POLICY_VALUE,
    policy_from_settings,
)

ROHGA_POLICY = (
    "cache_outcomes_max_age_days = 30\n"
    "cache_outcomes_max_bytes = 15000000000\n"
    "cache_outcomes_max_records = 0\n"
)
INVALID_DOCUMENTS = (
    ("cache_outcomes_max_age_days = true\n", "has the wrong type"),
    ('cache_outcomes_max_age_days = "30"\n', "has the wrong type"),
    ("cache_outcomes_max_bytes = 1.5\n", "has the wrong type"),
    ("cache_outcomes_max_records = true\n", "has the wrong type"),
    ("cache_outcomes_max_age_days = -1\n", f"between 0 and {MAX_AGE_DAYS}"),
    (f"cache_outcomes_max_age_days = {MAX_AGE_DAYS + 1}\n", "between 0 and"),
    ("cache_outcomes_max_bytes = -1\n", "outside the supported range"),
    ("cache_outcomes_max_records = -1\n", "outside the supported range"),
    (f"cache_outcomes_max_bytes = {MAX_POLICY_VALUE + 1}\n", "outside"),
    (f"cache_outcomes_max_records = {MAX_POLICY_VALUE + 1}\n", "outside"),
    ("cache_outcomes_archive_headroom_bytes = 1\n", "at least one segment"),
    ("cache_outcomes_archive_preset = 10\n", "between 0 and 9"),
    ("cache_outcomes_archive_preset = -1\n", "between 0 and 9"),
)


def test_omitted_settings_keep_the_documented_legacy_policy() -> None:
    """A host that writes no retention key keeps the history it had before."""
    loaded = config.load(None)

    assert loaded.cache_outcomes_max_age_days == DEFAULT_MAX_AGE_DAYS, (
        "omitted age keeps the documented default"
    )
    assert loaded.cache_outcomes_max_bytes == LEGACY_MAX_BYTES, (
        "omitted bytes keep the legacy budget"
    )
    assert loaded.cache_outcomes_max_records == 0, (
        "zero states that no count cap was configured"
    )
    assert loaded.cache_outcomes_archive_headroom_bytes == DEFAULT_HEADROOM_BYTES, (
        "omitted headroom keeps the documented reservation"
    )
    policy = policy_from_settings(
        loaded, header_floor=header_floor_bytes(), segment_limit=MAX_COMPACT_BYTES
    )
    assert policy.is_segmented is False, "legacy mode still compacts in place"
    assert policy.max_records == LEGACY_MAX_RECORDS, "the 10,000-row bound is kept"


def test_the_rohga_policy_is_accepted_unchanged(tmp_path: Path) -> None:
    """Thirty days and fifteen decimal gigabytes need no record cap."""
    policy = policy_from_settings(
        settings(tmp_path, ROHGA_POLICY),
        header_floor=header_floor_bytes(),
        segment_limit=MAX_COMPACT_BYTES,
    )

    assert policy.max_age.days == 30, "the requested age bound is effective"
    assert policy.max_bytes == 15_000_000_000, "bytes are decimal, not binary"
    assert policy.max_records is None, "no count cap truncates the history"
    assert policy.is_segmented is True, "a large budget must rotate"


def test_the_largest_supported_value_is_accepted(tmp_path: Path) -> None:
    """The documented upper bound itself is a valid policy."""
    loaded = settings(tmp_path, f"cache_outcomes_max_bytes = {MAX_POLICY_VALUE}\n")

    assert policy_from_settings(
        loaded, header_floor=header_floor_bytes(), segment_limit=MAX_COMPACT_BYTES
    ).max_bytes == MAX_POLICY_VALUE, (
        "the documented upper bound is accepted"
    )


def test_a_retention_key_needs_record_mode(tmp_path: Path) -> None:
    """A retention key that could not take effect is refused, not ignored."""
    path = tmp_path / "supervisor.toml"
    path.write_text("cache_outcomes_max_bytes = 1000\n", encoding="utf-8")

    with pytest.raises(config.ConfigError, match="only valid when cache_outcomes"):
        config.load(path)


@pytest.mark.parametrize(
    ("document", "message"),
    INVALID_DOCUMENTS,
    ids=[
        "bool-age",
        "string-age",
        "float-bytes",
        "bool-records",
        "negative-age",
        "oversized-age",
        "negative-bytes",
        "negative-records",
        "overflowing-bytes",
        "overflowing-records",
        "small-headroom",
        "large-preset",
        "negative-preset",
    ],
)
def test_an_invalid_policy_is_refused(
    tmp_path: Path, document: str, message: str
) -> None:
    """Each malformed bound names the setting an operator must change."""
    with pytest.raises(config.ConfigError, match=message):
        settings(tmp_path, document)


def test_a_refused_policy_leaves_recorded_history_intact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Validation runs before the store is opened, so nothing can be deleted."""
    store = write_private(tmp_path / "cache-outcomes.jsonl", document(records(NOW, 3)))
    before = store.read_bytes()
    path = tmp_path / "supervisor.toml"
    path.write_text(
        'cache_outcomes = "record"\ncache_outcomes_max_records = -1\n',
        encoding="utf-8",
    )

    status = outcome_archive_cli.main([
        "run",
        "--store",
        str(store),
        "--config",
        str(path),
    ])

    assert status == EXIT_USAGE, "a refused policy is a usage error"
    assert store.read_bytes() == before, "no recorded row was rewritten"
    layout = store_layout(tmp_path)
    assert not layout.archive_dir.exists(), "no archive state was created"
    captured = capsys.readouterr()
    assert "outside the supported range" in captured.err, (
        "the operator is told which bound is wrong"
    )
    assert str(store) not in captured.err, "no host path reaches the journal"
