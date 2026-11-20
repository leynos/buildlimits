"""Decoded views of one store shared by the sealing tests.

A sealing test states what the writer published, so it needs the same three
views every time: the layout beside the store, the sealed names in sequence
order, and the active file decoded as the archive run would see it. The writer
harness lives here too, because every sealing test opens the same segmented
writer and drives the same two appends through it.
"""

import contextlib
import json
import typing as typ

import pytest
from outcome_store_helpers import close_writer, segment_writer, wait_for_results
from outcome_test_helpers import STORE_EPOCH as NOW
from outcome_test_helpers import record_after
from segment_test_helpers import history_policy

from buildlimits.outcome_record_format import DecodedStore
from buildlimits.outcome_segments import SegmentLayout, sealed_meta_name
from buildlimits.outcome_store_read import read_store

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from buildlimits.outcome_ports import StoreResult
    from buildlimits.outcome_retention import RetentionPolicy
    from buildlimits.outcome_store import StoreWriter

__all__ = [
    "active_of",
    "layout_of",
    "metadata_of",
    "open_writer",
    "seal_two_rows",
    "sealed_names",
]


@contextlib.contextmanager
def open_writer(
    store: Path, policy: RetentionPolicy | None = None
) -> cabc.Generator[StoreWriter]:
    """Open the segmented writer a sealing test drives, closing it afterwards.

    Yields
    ------
    StoreWriter
        The writer, ready to accept appends.
    """
    writer = segment_writer(store, history_policy() if policy is None else policy, NOW)
    try:
        yield writer
    finally:
        close_writer(writer)


def seal_two_rows(
    writer: StoreWriter, *, reason: str = "none"
) -> tuple[StoreResult, ...]:
    """Append two rows and wait for the seal the second append triggers.

    Returns
    -------
    tuple[StoreResult, ...]
        Every result the writer published while the expected seal settled.
    """
    writer.append(record_after(0))
    writer.append(record_after(1))
    return wait_for_results(writer, operation="seal", reason=reason)


def layout_of(store: Path) -> SegmentLayout:
    """Return the layout beside one store.

    Returns
    -------
    SegmentLayout
        The store's segment layout.
    """
    return SegmentLayout.beside(store)


def sealed_names(store: Path) -> list[str]:
    """List the sealed source names in sequence order.

    Returns
    -------
    list[str]
        The ``segment-*.jsonl`` names.
    """
    directory = layout_of(store).sealed_dir
    if not directory.exists():
        return []
    return sorted(path.name for path in directory.glob("*.jsonl"))


def metadata_of(store: Path, sequence: int) -> dict[str, object]:
    """Read one sealed segment's writer metadata.

    Returns
    -------
    dict[str, object]
        The metadata document.
    """
    path = layout_of(store).sealed_dir / sealed_meta_name(sequence)
    return json.loads(path.read_text())


def active_of(store: Path) -> DecodedStore:
    """Read the active file, failing the test when it does not decode.

    Returns
    -------
    DecodedStore
        The active header and rows.
    """
    decoded = read_store(store)
    if not isinstance(decoded, DecodedStore):
        pytest.fail(f"the active file did not decode: {decoded.reason}")
    return decoded
