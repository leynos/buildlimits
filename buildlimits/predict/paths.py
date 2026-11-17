"""Path identity shared by the prediction commands.

Both commands write files beside the history they read, so each has to be able
to tell when an output path names the same file as its input. Comparing the
spellings is not enough: a relative path, a symbolic link or an existing hard
link can name one file twice, and writing the output would then replace the
history that a rerun needs.
"""

from pathlib import Path


def same_file(first: Path, second: Path) -> bool:
    """Return whether two paths name one file.

    Both paths are resolved, so a relative spelling, a symbolic link or an
    existing hard link cannot disguise the input as a separate output. A path
    that does not exist yet has no inode to compare, so its resolved form is
    compared instead.

    Returns
    -------
    bool
        Whether writing ``second`` would replace ``first``.

    Examples
    --------
    A symlink names the file it points at:

    >>> import tempfile
    >>> from pathlib import Path
    >>> with tempfile.TemporaryDirectory() as root:
    ...     target = Path(root, "history.jsonl")
    ...     _ = target.write_text("rows", encoding="utf-8")
    ...     alias = Path(root, "alias.jsonl")
    ...     _ = alias.symlink_to(target)
    ...     same_file(target, alias)
    True
    """
    try:
        return Path(first).samefile(second)
    except OSError:
        return Path(first).resolve() == Path(second).resolve()
