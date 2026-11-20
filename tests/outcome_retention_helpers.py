"""Shared retention fixtures for the store-writer and archiver contracts.

Both the writer's in-place pass and the archiver apply the same policy object,
so the fixtures that seed a store, start one writer and size an encoded row are
shared rather than restated in every retention test module.
"""

import contextlib
import dataclasses as dc
import datetime as dt
import typing as typ

import pytest
from outcome_archive_helpers import NOW, records
from outcome_store_helpers import (
    close_writer,
    salt_path,
    wait_for_results,
    wait_for_salt,
)

from buildlimits import config, outcome_records, outcome_store, outcome_store_io
from buildlimits.outcome_record_encode import header_floor_bytes
from buildlimits.outcome_key import Salt
from buildlimits.outcome_record_format import (
    MAX_COMPACT_BYTES,
    StoreHeader,
    StoreRecord,
)
from buildlimits.outcome_retention import (
    SEGMENT_BYTES,
    RetentionPolicy,
    policy_from_settings,
)
from buildlimits.outcome_store import StoreWriter

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from buildlimits.outcome_ports import StoreResult

ROHGA_MAX_BYTES = 15_000_000_000
SMALL_SEGMENT = 64 * 1024
# A startup pass over ten thousand rows is seconds of work on an idle host and
# longer on a busy one; the wait is load tolerance, not a behaviour change.
LARGE_STORE_TIMEOUT = 30.0


def policy(
    *,
    max_age: dt.timedelta = dt.timedelta(days=30),
    max_bytes: int = SEGMENT_BYTES,
    max_records: int | None = None,
) -> RetentionPolicy:
    """Build one in-place policy with the bounds a test needs.

    Returns
    -------
    RetentionPolicy
        A legacy-shaped policy whose bounds the caller may replace.
    """
    return RetentionPolicy(
        max_age=max_age,
        max_bytes=max_bytes,
        max_records=max_records,
        headroom_bytes=2 * SEGMENT_BYTES,
        segment_bytes=SEGMENT_BYTES,
    )


def decoded(rows: tuple[StoreRecord, ...]) -> outcome_records.DecodedStore:
    """Wrap rows in the decoded shape the compactor accepts.

    Returns
    -------
    DecodedStore
        The header and rows that would have been read from a store.
    """
    return outcome_records.DecodedStore(StoreHeader("salt-1", NOW), rows)


def seed(path: Path, rows: tuple[StoreRecord, ...]) -> None:
    """Write a store whose header uses the writer's own future salt.

    Seeding through the salt file keeps the rows in the active domain, so a
    startup pass decides about them on their age rather than discarding them
    as history from a previous salt.

    Parameters
    ----------
    path : Path
        The store to write.
    rows : tuple[StoreRecord, ...]
        The rows, oldest first.
    """
    salt = outcome_store.file_salt(salt_path(path))
    if not isinstance(salt, Salt):
        pytest.fail("the fixture has a readable active salt")
    outcome_store_io.replace_store(path, StoreHeader(salt.salt_id, NOW), rows)


@contextlib.contextmanager
def running_writer(
    path: Path,
    retention: RetentionPolicy,
    now: dt.datetime,
    *,
    timeout: float = 2.0,
) -> cabc.Generator[StoreWriter]:
    """Run one writer for the body of the ``with`` statement.

    The writer publishes its salt before the body starts, so a test can wait
    for a result without racing startup, and the writer is always closed
    within the same bound.

    Parameters
    ----------
    path : Path
        The store to open.
    retention : RetentionPolicy
        The bounds the writer applies.
    now : datetime
        The injected wall clock.
    timeout : float
        Seconds to allow a startup pass over a large store to report and close.

    Yields
    ------
    StoreWriter
        The started writer.
    """
    writer = StoreWriter(path, policy=retention, wall_clock=lambda: now)
    try:
        if not isinstance(wait_for_salt(writer), Salt):
            pytest.fail("the writer publishes its active salt")
        yield writer
    finally:
        close_writer(writer, timeout)


def large_store(tmp_path: Path) -> tuple[Path, tuple[StoreRecord, ...]]:
    """Seed a store holding more rows than the legacy count cap.

    Returns
    -------
    tuple[Path, tuple[StoreRecord, ...]]
        The store path and the rows written, oldest first.
    """
    path = tmp_path / "cache-outcomes.jsonl"
    rows = records(NOW - dt.timedelta(minutes=10_001), 10_001)
    seed(path, rows)
    return path, rows


def startup(
    path: Path,
    retention: RetentionPolicy,
    now: dt.datetime,
    *,
    timeout: float = 2.0,
) -> tuple[StoreResult, ...]:
    """Start one writer, collect its startup pass and close it.

    Parameters
    ----------
    path : Path
        The store to open.
    retention : RetentionPolicy
        The bounds the startup pass applies.
    now : datetime
        The injected wall clock.
    timeout : float
        Seconds to allow a startup pass over a large store to report and close.

    Returns
    -------
    tuple[StoreResult, ...]
        Every result the writer published during startup.
    """
    with running_writer(path, retention, now, timeout=timeout) as writer:
        return wait_for_results(
            writer, operation="compact", reason=None, timeout=timeout
        )


def encoded_size(rows: tuple[StoreRecord, ...]) -> int:
    """Return the exact on-disk size of a header and its rows.

    Returns
    -------
    int
        Every encoded byte plus its terminating newline.
    """
    lines = [decoded(rows).header, *rows]
    return sum(
        len(outcome_records.encode_line(line).encode("utf-8")) + 1 for line in lines
    )


def settings(tmp_path: Path, extra: str = "") -> config.SupervisorConfig:
    """Load record mode with optional retention keys.

    Parameters
    ----------
    tmp_path : Path
        The directory that receives ``supervisor.toml``.
    extra : str
        Retention keys to append, or an empty string for the defaults.

    Returns
    -------
    SupervisorConfig
        The validated settings.
    """
    path = tmp_path / "supervisor.toml"
    path.write_text(f'cache_outcomes = "record"\n{extra}', encoding="utf-8")
    return config.load(path)


def rohga_settings(tmp_path: Path) -> config.SupervisorConfig:
    """Load the thirty-day policy the operator asked for.

    Returns
    -------
    SupervisorConfig
        Settings carrying 30 days, 15 GB and no explicit record cap.
    """
    return settings(
        tmp_path,
        "cache_outcomes_max_age_days = 30\n"
        f"cache_outcomes_max_bytes = {ROHGA_MAX_BYTES}\n"
        "cache_outcomes_max_records = 0\n",
    )


def rohga_policy(tmp_path: Path) -> RetentionPolicy:
    """Build the segmented policy with small segments for a fast rotation test.

    Returns
    -------
    RetentionPolicy
        Thirty days and 15 GB, with 64 KiB segments so a fixture rotates.
    """
    return dc.replace(
        policy_from_settings(
            rohga_settings(tmp_path),
            header_floor=header_floor_bytes(),
            segment_limit=MAX_COMPACT_BYTES,
        ),
        headroom_bytes=4 * SMALL_SEGMENT,
        segment_bytes=SMALL_SEGMENT,
    )
