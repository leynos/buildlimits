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

from buildlimits import (
    outcome_history,
    outcome_segment_inventory,
)
from buildlimits.outcome_history_args import add_history_arguments, selection_from
from buildlimits.outcome_record_format import RecordFailure
from buildlimits.outcome_segments import SegmentLayout
from buildlimits.outcome_store_read import read_store
from buildlimits.outcome_store_salt import store_path
from buildlimits.predict.accounting import HistoryLoss
from buildlimits.predict.config_loader import load_config as _read_config
from buildlimits.predict.dataset import Snapshot, build_snapshot
from buildlimits.predict.dataset_inputs import output_is_an_input, read_inputs
from buildlimits.predict.dataset_journal import (
    journal_note,
    journal_span,
    journal_window,
)
from buildlimits.predict.formatting import as_counts
from buildlimits.predict.journal_provenance import (
    JournalCounts,
    JournalProvenance,
    JournalUnavailable,
)
from buildlimits.predict.records import decode_history
from buildlimits.predict.snapshot_store import save_snapshot
from buildlimits.retry_notice import report_retry
from buildlimits.utc_text import utc_now, utc_text
    JournalProvenance,
    JournalUnavailable,
)
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
        inputs = read_inputs(store, with_history=selection is not None)
    except OSError as error:
        print(f"build-limits-predict-dataset: {error}", file=sys.stderr)
        return 1
    if output_is_an_input(options.output, inputs, store, selection):
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
    snapshot = _snapshot(
        decoded,
        config,
        options,
        chosen,
        history_loss=(
            HistoryLoss.active_file_only()
            if selection is None
            else chosen.read_inventory(store)
        ),
    )
    return chosen.write_snapshot(snapshot, options.output)


def _snapshot(  # ruff: ignore[too-many-arguments] - one explicit input per provenance source.
    decoded: DecodedStore,
    config: Config,
    options: argparse.Namespace,
    chosen: Edges,
    *,
    history_loss: HistoryLoss,
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
    history_loss : HistoryLoss
        What the inventory says an extraction could not see.

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
        history_loss=history_loss,
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


def _read_inventory(store: Path) -> HistoryLoss:
    """Describe the closed history an extraction could not see.

    A scan failure is not a reason to lose the snapshot, so it is reported as
    "closed history not considered" rather than as an empty history.

    Returns
    -------
    HistoryLoss
        The inventory counts, or the active-file-only statement on failure.
    """
    try:
        inventory = outcome_segment_inventory.read_inventory(
            SegmentLayout.beside(store)
        )
    except OSError:
        return HistoryLoss.active_file_only()
    return HistoryLoss(
        active_only=False,
        archived=len(inventory.archived),
        sealed=len(inventory.sealed),
        gaps=len(inventory.gaps),
        gaps_truncated=inventory.gaps_truncated,
        retention_floor=inventory.retention_floor,
        quarantined=len(inventory.quarantined),
    )


def _read(store: Path) -> DecodedStore | int:
    """Read the store, reporting the reason a refusal is fatal.

    Returns
    -------
    DecodedStore | int
        An exit status on failure, otherwise the decoded store.
    """
    try:
        decoded = read_store(store)
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
    """Print what the snapshot holds, including its gaps and label loss."""
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
    _report_diagnostics(snapshot)
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


def _report_diagnostics(snapshot: Snapshot) -> None:
    """Print the bounded label-loss counts behind the snapshot's labels."""
    diagnostics = snapshot.diagnostics
    if diagnostics is None:
        print("label loss: no diagnostics recorded in this snapshot")
        return
    store = diagnostics.store
    print(
        f"store rows: total={store.records_total} "
        f"outcomes={store.outcome_records} summaries={store.summary_records} "
        f"labels={store.labels} labelled fraction="
        f"{store.labelled_fraction:.4f} of outcome rows; cacheable rows="
        f"{store.cacheable_records} with {store.cacheable_labels} labels"
    )
    for label, counts in (
        ("cache result", store.by_result),
        ("outcome reason", store.by_reason),
        ("attribution", store.by_attribution),
        ("origin", store.by_origin),
        ("parser class", store.by_parser_class),
        ("server version", store.by_server_version),
    ):
        print(f"by {label}: {as_counts(counts)}")
    if snapshot.journal is not None:
        _report_journal(snapshot.journal)
    history = diagnostics.history
    if history.active_only:
        print("closed history: not considered (the active file alone was read)")
        return
    print(
        f"closed history: archived={history.archived} sealed={history.sealed} "
        f"gaps={history.gaps} quarantined={history.quarantined} "
        f"retention floor={history.retention_floor}"
        + (" (gap list truncated)" if history.gaps_truncated else "")
    )


def _report_journal(note: JournalProvenance) -> None:
    """Print the recorder's own counts, which the store cannot provide."""
    if not isinstance(note, JournalCounts):
        return
    observer = note.observer
    if not observer.requests and not observer.outcomes:
        print("observer: the journal window held no admission or outcome events")
        return
    print(
        f"observer: requests={observer.requests} outcomes={observer.outcomes} "
        f"store appends={observer.store_records} "
        f"dropped={observer.store_discarded} readings={observer.readings} "
        f"skipped={observer.readings_skipped} failed={observer.readings_failed}"
    )
    print(f"observer by cache path: {as_counts(observer.by_cache_path)}")
    print(f"observer by result: {as_counts(observer.by_result)}")
    print(f"observer by reason: {as_counts(observer.by_reason)}")
    print(f"observer by gate reason: {as_counts(observer.by_gate_reason)}")


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
    read_inventory : Callable[[Path], HistoryLoss]
        Describes the closed history the extraction could not see.
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
    read_inventory: cabc.Callable[[Path], HistoryLoss] = _read_inventory
# weave: run 'weave explain buildlimits/predict/dataset_cli.py' for per-hunk detail, 'weave check' to verify your resolution
