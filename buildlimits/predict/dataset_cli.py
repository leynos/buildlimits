"""The build-limits-predict-dataset command.

It reads this host's live cache-outcome store, redacts it into the versioned
snapshot, and says how much history the snapshot really represents.
"""

import argparse
import dataclasses as dc
import os
import sys
import typing as typ
from pathlib import Path

from buildlimits import outcome_history, outcome_segment_inventory, outcome_store_io
from buildlimits.outcome_history_args import add_history_arguments, selection_from
from buildlimits.outcome_record_format import RecordFailure
from buildlimits.outcome_segments import SegmentLayout, archive_name
from buildlimits.outcome_store_salt import store_path
from buildlimits.predict.config_loader import load_config as _read_config
from buildlimits.predict.dataset import Snapshot, build_snapshot
from buildlimits.predict.dataset_journal import (
    journal_note,
    journal_span,
    journal_window,
)
from buildlimits.predict.journal_provenance import (
    JournalProvenance,
    JournalUnavailable,
)
from buildlimits.predict.paths import inside_history_directories, same_file
from buildlimits.predict.records import decode_history
from buildlimits.predict.snapshot_store import save_snapshot
from buildlimits.retry_notice import report_retry
from buildlimits.utc_text import utc_now, utc_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.metrics_query import Window
    from buildlimits.outcome_history import HistorySelection
    from buildlimits.outcome_record_format import DecodedStore
    from buildlimits.predict.config import Config

DEFAULT_OUTPUT = Path("build/predict/history.jsonl")
DEFAULT_JOURNAL_SPAN = "4w"


def main(argv: list[str] | None = None, *, edges: Edges | None = None) -> int:
    """Extract the live history into the benchmark snapshot.

    Parameters
    ----------
    argv : list[str] | None
        Command-line arguments, or process arguments when absent.
    edges : Edges | None
        The journal, clock, store resolver and reader, parameter loader and
        snapshot writer. None uses this host's, which is what the installed
        command does.

    Returns
    -------
    int
        0 on success, 1 when the store cannot be read or the snapshot cannot
        be written, 2 on invalid usage or an unreadable parameter file.
    """
    chosen = Edges() if edges is None else edges
    parser = argparse.ArgumentParser(prog="build-limits-predict-dataset")
    parser.add_argument(
        "--store",
        type=Path,
        default=None,
        help="cache-outcome store to read; defaults to this host's store",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"snapshot to write (default {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--journal-span",
        type=journal_span,
        default=DEFAULT_JOURNAL_SPAN,
        help="journal lookback used only for the provenance note",
    )
    parser.add_argument(
        "--skip-journal",
        action="store_true",
        help="do not query the user journal at all",
    )
    add_history_arguments(parser)
    try:
        options = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    settings = _settings(options, chosen)
    if isinstance(settings, int):
        return settings
    config, store = settings
    selection = selection_from(options)
    try:
        inputs = _read_inputs(store, with_history=selection is not None)
    except OSError as error:
        print(f"build-limits-predict-dataset: {error}", file=sys.stderr)
        return 1
    if _output_is_an_input(options.output, inputs, store, selection):
        print(
            "build-limits-predict-dataset: --output must not name a file the "
            f"command reads ({options.output}); choose another path",
            file=sys.stderr,
        )
        return 2
    decoded = (
        chosen.read_store(store)
        if selection is None
        else chosen.read_history(store, selection)
    )
    if isinstance(decoded, int):
        return decoded
    snapshot = _snapshot(decoded, config, options, chosen)
    return chosen.write_snapshot(snapshot, options.output)


def _snapshot(
    decoded: DecodedStore,
    config: Config,
    options: argparse.Namespace,
    chosen: Edges,
) -> Snapshot:
    """Build the snapshot from the decoded history and its provenance.

    Parameters
    ----------
    decoded : DecodedStore
        The rows the command read.
    config : Config
        The parameters the gap threshold comes from.
    options : argparse.Namespace
        The parsed command line.
    chosen : Edges
        The journal and clock this run uses.

    Returns
    -------
    Snapshot
        The redacted snapshot, ready to write.
    """
    note: JournalProvenance = (
        JournalUnavailable("journal query skipped")
        if options.skip_journal
        else journal_note(chosen.journal, options.journal_span)
    )
    return build_snapshot(
        decode_history(decoded),
        now=chosen.now(),
        journal=note,
        gap_threshold_seconds=config.gap_threshold_seconds,
    )


def _settings(options: argparse.Namespace, chosen: Edges) -> tuple[Config, Path] | int:
    """Load the parameters and name the store, reporting a failure once.

    Parameters
    ----------
    options : argparse.Namespace
        The parsed command line.
    chosen : Edges
        The parameter loader and store resolver this run uses.

    Returns
    -------
    tuple[Config, Path] or int
        The parameters and store path, or the exit status after the failure
        has already been printed.
    """
    try:
        config = chosen.load_config()
    except (TypeError, ValueError, OSError) as error:
        print(f"build-limits-predict-dataset: {error}", file=sys.stderr)
        return 2
    try:
        store = chosen.resolve_store(options.store)
    except (OSError, ValueError) as error:
        print(f"build-limits-predict-dataset: {error}", file=sys.stderr)
        return 1
    return config, store


def _output_is_an_input(
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


def _read_inputs(store: Path, *, with_history: bool) -> tuple[Path, ...]:
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


def _write(snapshot: Snapshot, output: Path) -> int:
    """Write the snapshot and report an output that cannot be written.

    A read-only directory, a full disk or an output path that names a
    directory all raise ``OSError`` from the writer. The command answers with
    a message and a status, like every other failure path, rather than a
    traceback.

    Returns
    -------
    int
        0 when the snapshot is written, 1 when it cannot be.
    """
    try:
        save_snapshot(output, snapshot)
    except OSError as error:
        print(f"build-limits-predict-dataset: {error}", file=sys.stderr)
        return 1
    _report(snapshot, output)
    return 0


def _store_path(store: Path | None) -> Path:
    """Return the store path this run will read.

    Returns
    -------
    Path
        The explicit path, or this host's configured store.
    """
    return store_path(os.environ) if store is None else store


def _read(store: Path) -> DecodedStore | int:
    """Read the store, reporting the reason a refusal is fatal.

    Returns
    -------
    DecodedStore | int
        An exit status on failure, otherwise the decoded store.
    """
    try:
        decoded = outcome_store_io.read_store(store)
    except (OSError, ValueError) as error:
        print(f"build-limits-predict-dataset: {error}", file=sys.stderr)
        return 1
    if isinstance(decoded, RecordFailure):
        print(
            f"build-limits-predict-dataset: the store was rejected as {decoded.reason}",
            file=sys.stderr,
        )
        return 1
    return decoded


def _read_history(store: Path, selection: HistorySelection) -> DecodedStore | int:
    """Read retained history across every segment state.

    Parameters
    ----------
    store : Path
        The configured active store path.
    selection : HistorySelection
        The salt domain the snapshot should cover.

    Returns
    -------
    DecodedStore | int
        An exit status on failure, otherwise the decoded history.
    """
    try:
        return outcome_history.read_history_beside(
            store, selection, on_retry=report_retry
        )
    except outcome_history.HistoryFaultError as fault:
        print(
            f"build-limits-predict-dataset: the history was rejected as {fault.reason}",
            file=sys.stderr,
        )
        return 1
    except (OSError, ValueError) as error:
        print(f"build-limits-predict-dataset: {error}", file=sys.stderr)
        return 1


def _report(snapshot: Snapshot, output: Path) -> None:
    """Print what the snapshot holds, including its gaps."""
    stats = snapshot.stats()
    print(f"wrote {output}")
    print(
        f"events={stats.events} labelled={stats.labelled} "
        f"identities={stats.identities} gaps={len(snapshot.gaps)}"
    )
    if stats.span_start is None or stats.span_end is None:
        print("window: no events recorded")
    else:
        print(f"window {utc_text(stats.span_start)} to {utc_text(stats.span_end)}")
    print(
        f"admission times: recorded={stats.admission_recorded} "
        f"usable={stats.admission_usable} missing={stats.admission_missing} "
        f"after their own label={stats.admission_unusable}"
    )
    if stats.labelled == 0:
        print(
            "no attributed hit or miss was recorded, so the benchmark will "
            "report insufficient data rather than invent any",
            file=sys.stderr,
        )
    if stats.admission_usable == 0:
        print(
            "no store row carries an admission time at or before its own label, "
            "so the benchmark cannot predict any event and will report "
            "insufficient admission times rather than use the label time",
            file=sys.stderr,
        )


@dc.dataclass(frozen=True, slots=True)
class Edges:
    """The external edges the dataset command reads through.

    They travel together because every test that replaces one replaces the
    others: a fixed clock without a stub reader would still touch this host's
    store, and a stub writer without a clock would stamp the snapshot with the
    wall time the test is trying to exclude.

    Attributes
    ----------
    journal : Callable[[str], Window]
        Reads the user journal for one lookback, or raises when it cannot.
    now : Callable[[], datetime]
        The clock the snapshot records.
    read_store : Callable[[Path], DecodedStore | int]
        Reads the active outcome store, or returns an exit status on failure.
    read_history : Callable[[Path, HistorySelection], DecodedStore | int]
        Reads archived, sealed and active history for one salt domain, or
        returns an exit status on failure.
    write_snapshot : Callable[[Snapshot, Path], int]
        Writes the snapshot and reports what it holds.
    resolve_store : Callable[[Path | None], Path]
        Names the store to read, defaulting to this host's when no path is
        given. Reading the environment is a host concern, so it travels with
        the other edges rather than happening beside them.
    load_config : Callable[[], Config]
        Reads the pinned parameters the gap threshold comes from, so a test
        can refuse an unreadable file without patching the module.
    """

    journal: cabc.Callable[[str], Window] = journal_window
    now: cabc.Callable[[], dt.datetime] = utc_now
    read_store: cabc.Callable[[Path], DecodedStore | int] = _read
    read_history: cabc.Callable[[Path, HistorySelection], DecodedStore | int] = (
        _read_history
    )
    write_snapshot: cabc.Callable[[Snapshot, Path], int] = _write
    resolve_store: cabc.Callable[[Path | None], Path] = _store_path
    load_config: cabc.Callable[[], Config] = _read_config
