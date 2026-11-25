"""Fixtures that reproduce the exact shape an older store release wrote.

A test that loads an older document must reproduce what that release really
wrote, not a current row with a downgraded header. The shapes come from the
encoder's own version table, so a field added later is dropped here without any
list being edited, and a fixture cannot silently become a hybrid no decoder
accepts.
"""

import json
import typing as typ

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import StoreRecord


VERSION_ONE = 1
"""The oldest store format, which predates admission and classification."""

VERSION_TWO = 2
"""The admission format, which predates the label-time provenance marker."""

VERSION_THREE = 3
"""The provenance format, which predates the generation digest."""

VERSION_FOUR = 4
"""The digest format, which predates the version 5 correlation group."""

OLDER_VERSIONS = (VERSION_ONE, VERSION_TWO, VERSION_THREE, VERSION_FOUR)


def older_document_row(row: StoreRecord, *, version: int) -> dict[str, object]:
    """Encode one row in the shape an older release wrote it.

    Parameters
    ----------
    row : StoreRecord
        The store record to encode.
    version : int
        The format version whose shape the row should take, one of
        :data:`OLDER_VERSIONS`.

    Returns
    -------
    dict[str, object]
        The row's JSON object restricted to that version's fields.

    Raises
    ------
    ValueError
        If ``version`` is not an older format version.

    Examples
    --------
    >>> from outcome_test_helpers import STORE_EPOCH, attributed_record
    >>> row = attributed_record(observed_at=STORE_EPOCH)
    >>> "admitted_at" in older_document_row(row, version=VERSION_ONE)
    False
    >>> "admitted_at" in older_document_row(row, version=VERSION_TWO)
    True
    """
    from buildlimits.outcome_record_encode import _ROW_FIELDS_BY_VERSION, encode_line

    allowed = _ROW_FIELDS_BY_VERSION.get(version)
    if version not in OLDER_VERSIONS or allowed is None:
        msg = f"version {version} is not an older store format"
        raise ValueError(msg)
    document = typ.cast("dict[str, object]", json.loads(encode_line(row)))
    # "kind" is the row discriminator, not a versioned field: the encoder adds
    # it after the field filter, so it is never in the table.
    return {
        name: value
        for name, value in document.items()
        if name == "kind" or name in allowed
    }


__all__ = [
    "OLDER_VERSIONS",
    "VERSION_FOUR",
    "VERSION_ONE",
    "VERSION_THREE",
    "VERSION_TWO",
    "older_document_row",
]
