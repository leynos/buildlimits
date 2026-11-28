"""Sealed-source documents and metadata the retention tests start from.

A fixture that writes only a sealed source leaves it unmeasured, because the
planner trusts the writer's metadata for the counts and span. These helpers
publish both, so every retention test describes a source a real seal produced.
"""

import datetime as dt
import json
import typing as typ

from segment_test_helpers import document, write_private
from segment_test_helpers import store_layout as layout

from buildlimits.outcome_segments import sealed_meta_name, segment_name
from buildlimits.outcome_store_sealing import sealed_metadata

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from buildlimits.outcome_record_format import StoreRecord

NOW = dt.datetime(2026, 11, 19, tzinfo=dt.UTC)

__all__ = ["NOW", "sealed_bytes", "sealed_source", "sealed_source_exists"]


def sealed_source(
    root: Path,
    sequence: int,
    rows: cabc.Sequence[StoreRecord],
    *,
    sealed_at: dt.datetime = NOW,
) -> Path:
    """Write one measured sealed source, as the writer would close it.

    A fixture that writes only the source leaves it unmeasured, because the
    planner trusts the writer's metadata for the counts and span. Publishing
    both keeps every retention test describing a source a real seal produced.

    Parameters
    ----------
    root : Path
        The directory the store lives under.
    sequence : int
        The sequence number the source's name carries.
    rows : Sequence[StoreRecord]
        The rows the writer closed into the source, encoded under the shared
        test salt.
    sealed_at : datetime
        The moment the writer recorded for the seal.

    Returns
    -------
    Path
        The source path.
    """
    directory = layout(root).sealed_dir
    path = write_private(directory / segment_name(sequence), document(tuple(rows)))
    metadata = sealed_metadata(list(rows), sequence, "salt-1", sealed_at)
    (directory / sealed_meta_name(sequence)).write_text(
        json.dumps(metadata), encoding="utf-8"
    )
    return path


def sealed_bytes(root: Path, sequence: int) -> bytes:
    """Return the plain document one sealed source holds.

    Returns
    -------
    bytes
        The source's JSON Lines content.
    """
    return (layout(root).sealed_dir / segment_name(sequence)).read_bytes()


def sealed_source_exists(root: Path, sequence: int) -> bool:
    """Whether one sealed source is still present.

    Returns
    -------
    bool
        True when the source file exists.
    """
    return (layout(root).sealed_dir / segment_name(sequence)).exists()
