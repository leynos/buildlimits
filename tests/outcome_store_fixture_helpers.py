"""Private store fixtures shared by the cache-outcome tests."""

import datetime as dt
import typing as typ
from unittest import mock

import pytest
from outcome_store_helpers import salt_path
from outcome_test_helpers import OBSERVED_AT, attributed_record, window_summary

from buildlimits import (
    outcome_records,
    outcome_store,
    outcome_store_io,
    outcome_store_state,
)
from buildlimits.outcome_key import Salt
from buildlimits.outcome_record_format import StoreHeader
from buildlimits.outcome_store_clock import Clocks
from buildlimits.outcome_store_salt import file_salt

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from buildlimits.outcome_record_format import StoreRecord, WindowSummary
    from buildlimits.outcome_retention import RetentionPolicy


def seed_replay_store(path: Path) -> None:
    """Write one private store holding an attributed row and a summary.

    The replay verdicts need both kinds of row: the attributed compilation is
    one judgeable sample, and the summary is one independently evaluated group.

    Parameters
    ----------
    path : Path
        The store file to create or replace.
    """
    records = (
        attributed_record(observed_at=OBSERVED_AT),
        window_summary(observed_at=OBSERVED_AT),
    )
    outcome_store_io.replace_store(
        path,
        StoreHeader("fixed-salt", dt.datetime(2026, 10, 24, tzinfo=dt.UTC)),
        records,
    )


def append_torn_utf8_tail(path: Path, record: WindowSummary) -> None:
    """Append a JSON row truncated inside its non-ASCII server version.

    Parameters
    ----------
    path : Path
        The store receiving the incomplete final row.
    record : WindowSummary
        A row whose server version contains ``é``.

    Examples
    --------
    ``append_torn_utf8_tail(path, window_summary(server_version="partial-é"))``
    leaves an incomplete final byte sequence for recovery tests.
    """
    encoded = outcome_records.encode_line(record).encode("utf-8")
    character_start = encoded.index("é".encode())
    with path.open("ab") as destination:
        destination.write(encoded[: character_start + 1])


def seed_one_record_store(
    path: Path,
    *,
    server_version: str = "0.14.0",
    torn_tail: str | None = None,
) -> WindowSummary:
    """Seed one private store with a record and an optional torn JSON tail.

    Returns
    -------
    WindowSummary
        The complete record stored before the optional tail.

    Examples
    --------
    ``seed_one_record_store(path, server_version="sccache-é")`` seeds a
    valid non-ASCII record for strict UTF-8 reader tests.
    """
    salt = outcome_store.file_salt(salt_path(path))
    if not isinstance(salt, Salt):
        pytest.fail("the fixture has a readable active salt")
    record = window_summary(server_version=server_version)
    seed_store(path, salt, (record,), torn_tail=torn_tail)
    return record


def write_private_file(path: Path, contents: bytes) -> None:
    """Create one mode-0600 file for store fixtures.

    Parameters
    ----------
    path : Path
        Destination for the fixture.
    contents : bytes
        Exact bytes the test needs at that path.
    """
    path.write_bytes(contents)
    path.chmod(0o600)


def seed_store(
    path: Path,
    salt: Salt,
    records: tuple[StoreRecord, ...],
    *,
    torn_tail: str | None = None,
) -> outcome_records.StoreHeader:
    """Write a valid fixture header and rows, with an optional torn tail.

    Returns
    -------
    StoreHeader
        The header used for the fixture.
    """
    header = outcome_records.StoreHeader(
        salt.salt_id, dt.datetime(2026, 10, 25, tzinfo=dt.UTC)
    )
    lines = [outcome_records.encode_line(header)]
    lines.extend(outcome_records.encode_line(record) for record in records)
    document = "\n".join(lines) + "\n"
    if torn_tail is not None:
        document += torn_tail
    path.write_text(document, encoding="utf-8")
    path.chmod(0o600)
    return header


def loaded_store_files(
    path: Path,
    *,
    policy: RetentionPolicy,
    now: dt.datetime,
    wall_clock: cabc.Callable[[], dt.datetime] | None = None,
) -> outcome_store_state.StoreFiles:
    """Return writer state loaded from a fresh salted store at ``path``.

    A synchronous test that drives the writer's transitions needs the same
    salted, loaded state its worker builds. Loading also installs the header,
    so the returned state is ready for appends and measurements.

    Parameters
    ----------
    path : Path
        The store path.
    policy : RetentionPolicy
        The retention bounds the state applies.
    now : datetime
        The instant the initial load and settle use.
    wall_clock : Callable[[], datetime] | None
        The clock the state reads later, or None to keep returning ``now``.

    Returns
    -------
    StoreFiles
        The loaded writer state.

    Examples
    --------
    ``loaded_store_files(path, policy=policy, now=NOW)`` loads an empty store
    and leaves ``files.records`` empty.
    """
    salted = file_salt(salt_path(path))
    if not isinstance(salted, Salt):
        pytest.fail(f"the salt fixture failed: {salted.reason}")
    clock = (lambda: now) if wall_clock is None else wall_clock
    files = outcome_store_state.StoreFiles(
        path, mock.Mock(), Clocks(wall=clock), policy=policy
    )
    files.salt = salted
    files.load_and_settle(salted, now)
    return files
