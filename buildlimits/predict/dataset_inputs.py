"""The files a dataset snapshot reads, and whether its output would replace one.

A snapshot is written beside the history it describes, so the command has to
name every input before it writes anything. A history read can also consume a
segment the archiver creates after the inventory was taken, so the directories
themselves are refused rather than only the names an inventory listed.
"""

import typing as typ

from buildlimits import outcome_segment_inventory
from buildlimits.outcome_segments import SegmentLayout, archive_name
from buildlimits.predict.paths import inside_history_directories, same_file

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.outcome_history import HistorySelection

__all__ = ["output_is_an_input", "read_inputs"]


def output_is_an_input(
    output: Path,
    inputs: tuple[Path, ...],
    store: Path,
    selection: HistorySelection | None,
) -> bool:
    """Return whether the output would replace, or land beside, history read.

    A history read consumes segments that may be created after the inventory
    was taken, so an output inside a history directory is refused even when no
    listed input names it. Without ``--history`` the command reads only the
    active store, and its output may sit anywhere else.

    Parameters
    ----------
    output : Path
        The requested snapshot path.
    inputs : tuple[Path, ...]
        The files the command will read.
    store : Path
        The configured active store path, which names the layout.
    selection : HistorySelection | None
        The salt domain a history read covers, or None when only the active
        store is read.

    Returns
    -------
    bool
        Whether writing ``output`` would replace or sit beside an input.
    """
    if any(same_file(path, output) for path in inputs):
        return True
    return selection is not None and inside_history_directories(output, store)


def read_inputs(store: Path, *, with_history: bool) -> tuple[Path, ...]:
    """Return every file the command reads, so the output can avoid them.

    Parameters
    ----------
    store : Path
        The configured active store path.
    with_history : bool
        Whether the command also reads closed segments.

    Returns
    -------
    tuple[Path, ...]
        The active store, plus every closed segment when history is read; a
        listing failure propagates ``OSError``.
    """
    if not with_history:
        return (store,)
    layout = SegmentLayout.beside(store)
    inventory = outcome_segment_inventory.read_inventory(layout)
    # Every archive is protected, not only the ones an effective read would
    # choose: a sealed source shadows its archive for reading, but the
    # snapshot must never replace an archive either copy of which exists.
    #
    # The archive a sealed source will become is protected too. The archiver
    # may publish it and remove the source while the history is being read,
    # and writing the snapshot to that path would then replace the freshly
    # published archive with a document the store cannot read back.
    paths = (
        layout.active,
        *(entry.path for entry in inventory.effective),
        *(entry.path for entry in inventory.archived),
        *(
            layout.archive_dir / archive_name(entry.sequence)
            for entry in inventory.effective
        ),
    )
    return tuple(dict.fromkeys(paths))
