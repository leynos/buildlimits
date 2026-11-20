"""Repair an interrupted final row in the active store.

A writer that stopped mid-append leaves a fragment with no newline. The store
reader ignores that fragment, and the next append would join a new row to it,
producing one line that is not valid JSON Lines. This module owns the bounded
truncation that makes the file on disk match the rows the decoder accepted.
"""

import os
import typing as typ

from buildlimits.outcome_store_io import open_private_regular

if typ.TYPE_CHECKING:
    from pathlib import Path

__all__ = ["drop_torn_tail"]

_TAIL_CHUNK_BYTES = 64 * 1024


def drop_torn_tail(path: Path) -> bool:
    r"""Remove an unterminated final row from a private store.

    The scan reads backwards in fixed-size chunks, so the bound holds however
    long the file is, and a file that cannot be opened, inspected or truncated
    propagates ``OSError``.

    Parameters
    ----------
    path : Path
        The store to repair.

    Returns
    -------
    bool
        Whether an unterminated tail was removed.

    Examples
    --------
    >>> import tempfile
    >>> from pathlib import Path
    >>> with tempfile.TemporaryDirectory() as root:
    ...     store = Path(root) / "cache-outcomes.jsonl"
    ...     _ = store.write_bytes(b'{"a":1}\n{"half":')
    ...     store.chmod(0o600)
    ...     (drop_torn_tail(store), store.read_bytes())
    (True, b'{"a":1}\n')
    """
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor, metadata = open_private_regular(path, flags)
    except FileNotFoundError:
        return False
    try:
        end = metadata.st_size
        if end == 0:
            return False
        position = end
        while position > 0:
            start = max(0, position - _TAIL_CHUNK_BYTES)
            os.lseek(descriptor, start, os.SEEK_SET)
            data = os.read(descriptor, position - start)
            newline = data.rfind(b"\n")
            if newline >= 0:
                cut = start + newline + 1
                if cut == end:
                    return False
                os.ftruncate(descriptor, cut)
                os.fsync(descriptor)
                return True
            position = start
        os.ftruncate(descriptor, 0)
        os.fsync(descriptor)
        return True
    finally:
        os.close(descriptor)
