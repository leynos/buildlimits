"""Durable small documents written beside a cache-outcome store.

The writer's sequence counter and each sealed segment's metadata are single
short values that must be replaced atomically. They share the private
temporary-file and directory-sync primitives in ``outcome_store_io``; keeping
their JSON shapes here leaves that module to the store file itself.
"""

import json
import typing as typ

from buildlimits.outcome_store_io import (
    fsync_directory,
    replace_temp,
    write_private_temporary,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

__all__ = ["write_metadata", "write_sequence"]


def write_sequence(path: Path, value: int) -> None:
    """Atomically store the next sealed sequence number.

    The counter is written before a segment is moved, so a crash leaves a gap
    the inventory reports rather than a sequence two segments can share.
    Filesystem failures propagate as ``OSError``.
    """
    _replace_document(path, f"{value}\n".encode("ascii"))


def write_metadata(path: Path, document: cabc.Mapping[str, object]) -> None:
    """Atomically store one JSON metadata document beside a sealed source.

    Filesystem failures propagate as ``OSError``.
    """
    encoded = json.dumps(
        document, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    _replace_document(path, encoded + b"\n")


def _replace_document(path: Path, data: bytes) -> None:
    """Write bytes through one private temporary file and replace ``path``."""
    replace_temp(write_private_temporary(path, data), path)
    fsync_directory(path.parent)
