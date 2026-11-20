"""Private-file primitives for cache-outcome storage.

The store, the segment sidecars and the history pin all read and open the same
kind of file: one regular file, private to the owner, never reached through a
symlink, and never read past a caller's bound. These primitives keep that
contract in one place so a read cannot silently lose a check that another read
keeps. They are package implementation support, not a standalone public API.
"""

import contextlib
import errno
import os
import stat
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

FILE_MODE = 0o600

# Bytes requested per further read once a file has grown past the size its
# descriptor reported. A file being appended to is read in chunks so a
# concurrent writer costs memory in proportion to what it wrote, not to the
# caller's whole read bound.
_READ_CHUNK = 64 * 1024


class OversizedFileError(OSError):
    """A private file exceeded the caller's read bound."""


def read_private_bytes(path: Path, *, limit: int) -> bytes:
    """Read one private regular file entirely, refusing anything over ``limit``.

    The file is opened without following links and its size is checked both
    from the descriptor's metadata and from the bytes actually read, so a file
    that grows while it is read cannot exceed the caller's bound either. The
    first read asks for one byte past the size the descriptor reported, because
    a buffered read allocates the length it is asked for: requesting the whole
    limit would cost ``limit`` bytes of memory even for a small file. Only a
    file that actually grew is read further, in bounded chunks. Filesystem
    failures other than an oversized file propagate as ``OSError``.

    Parameters
    ----------
    path : Path
        The private regular file to read.
    limit : int
        The largest permitted size in bytes.

    Returns
    -------
    bytes
        The file's contents.

    Raises
    ------
    OversizedFileError
        If the file's size, measured either way, exceeds ``limit``.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor, metadata = open_private_regular(path, flags, enforce_mode=False)
    if metadata.st_size > limit:
        os.close(descriptor)
        raise OversizedFileError(errno.EFBIG, "file exceeds its read bound")
    with os.fdopen(descriptor, "rb") as source:
        data = source.read(metadata.st_size + 1)
        if len(data) > metadata.st_size:
            data = _read_growth(source, data, limit)
    if len(data) > limit:
        raise OversizedFileError(errno.EFBIG, "file exceeds its read bound")
    return data


def _read_growth(source: typ.BinaryIO, first: bytes, limit: int) -> bytes:
    """Finish reading a file that grew after its size was checked.

    The writer may append to the active file while it is read, so growth is
    normal; the bound, not the earlier size, decides whether the read is
    allowed. Growth is read in bounded chunks and only up to one byte past
    ``limit``, so a file that keeps growing is refused without reading all of
    it.

    Parameters
    ----------
    source : BinaryIO
        The open file, positioned just after ``first``.
    first : bytes
        The bytes read before growth was noticed.
    limit : int
        The largest permitted size in bytes.

    Returns
    -------
    bytes
        Everything read, which the caller checks against ``limit``.
    """
    chunks = [first]
    total = len(first)
    while total <= limit:
        chunk = source.read(min(_READ_CHUNK, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def require_regular(metadata: os.stat_result) -> None:
    """Reject filesystem objects that are not regular files.

    Raises
    ------
    OSError
        If the supplied metadata describes another filesystem object.
    """
    if not stat.S_ISREG(metadata.st_mode):
        msg = "outcome store is not a regular file"
        raise OSError(msg)


def open_private_regular(
    path: Path,
    flags: int,
    *,
    enforce_mode: bool = True,
    validate: cabc.Callable[[os.stat_result], None] = require_regular,
) -> tuple[int, os.stat_result]:
    """Open a regular file without following links, optionally enforcing mode.

    ``O_NOFOLLOW`` and ``O_NONBLOCK`` are always added, whatever the caller
    passes, so a substitution the caller did not anticipate is refused at this
    one boundary: a symlink at a store path fails with ``ELOOP`` and a FIFO
    cannot block the open. Each current caller passes ``O_NOFOLLOW`` itself as
    well; keeping the flag here means a new caller cannot lose the check.

    Parameters
    ----------
    path : Path
        The file to open.
    flags : int
        Open flags; ``O_NOFOLLOW`` and ``O_NONBLOCK`` are added.
    enforce_mode : bool, default=True
        Whether to narrow the file to the private owner-only mode.
    validate : Callable[[os.stat_result], None], default=require_regular
        The guarantee the opened object must satisfy. The archiver supplies a
        stricter check than the store's, because it also refuses a file owned
        by another account. One open path serves both, so neither can drift
        from the flags above.

    Returns
    -------
    tuple[int, os.stat_result]
        The open descriptor and the metadata captured from it.

    Raises
    ------
    OSError
        If opening, validating or enforcing the private file mode fails, or if
        the path is not a regular file.
    """
    guarded = flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, guarded)
    try:
        metadata = os.fstat(descriptor)
        validate(metadata)
        if enforce_mode:
            os.fchmod(descriptor, FILE_MODE)
    except OSError:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        raise
    return descriptor, metadata
