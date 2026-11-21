"""Path identity shared by the prediction commands.

Both commands write files beside the history they read, so each has to be able
to tell when an output path names the same file as its input. Comparing the
spellings is not enough: a relative path, a symbolic link or an existing hard
link can name one file twice, and writing the output would then replace the
history that a rerun needs. A history read also consumes segments that may be
created while it runs, so an output that lands in one of the history
directories is refused even when no inventory has seen its name yet.
"""

from pathlib import Path

from buildlimits.outcome_segments import SegmentLayout


def inside_history_directories(output: Path, store: Path) -> bool:
    """Return whether an output path lands in a directory history is read from.

    An inventory taken before a history read can only list the segments that
    already exist. The archiver may seal the active file or publish an archive
    while the read is running, and a snapshot written to that name would
    replace history the read either consumed or was about to consume. Refusing
    the directories themselves, rather than only the files an inventory
    listed, also covers a segment that appears after the inventory.

    Parameters
    ----------
    output : Path
        The requested snapshot path.
    store : Path
        The configured active store path, which names the layout.

    Returns
    -------
    bool
        Whether the output resolves inside the sealed or archive directory.

    Examples
    --------
    >>> from pathlib import Path
    >>> inside_history_directories(
    ...     Path("/state/cache-outcomes.archive/snapshot.jsonl"),
    ...     Path("/state/cache-outcomes.jsonl"),
    ... )
    True
    """
    layout = SegmentLayout.beside(store)
    return any(
        _is_within(output, directory)
        for directory in (layout.sealed_dir, layout.archive_dir)
    )


def _is_within(path: Path, directory: Path) -> bool:
    """Return whether one path resolves inside a directory.

    The comparison uses resolved paths, so a relative spelling or a symbolic
    link cannot place the output in a directory it does not really name.

    Returns
    -------
    bool
        Whether ``path`` names the directory or something below it.
    """
    try:
        return Path(path).resolve().is_relative_to(Path(directory).resolve())
    except OSError:
        # A path that cannot be resolved cannot be shown to be safe, so the
        # caller treats it as inside rather than allowing the write.
        return True


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
