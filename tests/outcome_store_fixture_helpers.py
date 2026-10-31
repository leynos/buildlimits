"""Private store fixtures shared by cache-outcome reader tests."""

import typing as typ

import pytest
from outcome_store_helpers import salt_path, seed_store, window_summary

from buildlimits import outcome_records, outcome_store
from buildlimits.outcome_key import Salt

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_record_format import WindowSummary


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
