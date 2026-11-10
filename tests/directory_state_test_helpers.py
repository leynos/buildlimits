"""Record a tree's files so a read can be shown to have changed nothing.

Two boundary tests make the same claim about different adapters: the
configuration queries and the inventory query read a directory and leave every
file as it was. Both need the same record of what the tree held beforehand, and
that record has to see a rewrite that restored the original content, so the
modification time and mode are kept beside the bytes.
"""

import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

type FileState = tuple[bytes, int, int, int]
"""One file's bytes, size, modification time in nanoseconds, and mode."""


def snapshot_tree(directory: Path, *, relative_to: Path) -> dict[str, FileState]:
    """Return every file under ``directory`` as its name and current state.

    Parameters
    ----------
    directory : pathlib.Path
        The directory to walk.
    relative_to : pathlib.Path
        The directory the recorded names are made relative to, so that trees
        captured together share one namespace.

    Returns
    -------
    dict[str, FileState]
        One entry per file, keyed by its path relative to ``relative_to``, so a
        renamed, added, removed or rewritten file changes the mapping.

    Examples
    --------
    >>> import tempfile
    >>> from pathlib import Path
    >>> with tempfile.TemporaryDirectory() as scratch:
    ...     root = Path(scratch)
    ...     _ = (root / "kept.txt").write_text("x", encoding="utf-8")
    ...     sorted(snapshot_tree(root, relative_to=root))
    ['kept.txt']
    """
    states: dict[str, FileState] = {}
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        stat = path.stat()
        states[str(path.relative_to(relative_to))] = (
            path.read_bytes(),
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_mode,
        )
    return states
