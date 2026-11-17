"""The build-limits-predict-dataset command.

It reads this host's live cache-outcome store, redacts it into the versioned
snapshot, and says how much history the snapshot really represents.
"""

import argparse
import dataclasses as dc
import datetime as dt
import os
import sys
import typing as typ
from pathlib import Path

from buildlimits import outcome_store_io
from buildlimits.metrics_query import window_seconds
from buildlimits.outcome_record_format import RecordFailure
from buildlimits.outcome_store_salt import store_path
from buildlimits.predict.config import load_config as _read_config
from buildlimits.predict.dataset import (
    DEFAULT_JOURNAL_SPAN,
    Snapshot,
    build_snapshot,
    count_journal,
)
from buildlimits.predict.paths import same_file
from buildlimits.predict.snapshot_store import save_snapshot

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_record_format import DecodedStore
    from buildlimits.predict.config import Config

DEFAULT_OUTPUT = Path("build/predict/history.jsonl")


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
        type=_journal_span,
        default=DEFAULT_JOURNAL_SPAN,
        help="journal lookback used only for the provenance note",
    )
    parser.add_argument(
        "--skip-journal",
        action="store_true",
        help="do not query the user journal at all",
    )
    try:
        options = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
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
    if same_file(store, options.output):
        print(
            "build-limits-predict-dataset: --output must not be the store being "
            f"read ({store}); choose another path",
            file=sys.stderr,
        )
        return 2
    decoded = chosen.read_store(store)
    if isinstance(decoded, int):
        return decoded
    note = (
        {"available": False, "reason": "journal query skipped"}
        if options.skip_journal
        else _journal_note(chosen.journal, options.journal_span)
    )
    snapshot = build_snapshot(
        decoded,
        now=chosen.now(),
        journal=note,
        gap_threshold_seconds=config.gap_threshold_seconds,
    )
    return chosen.write_snapshot(snapshot, options.output)


def _journal_span(text: str) -> str:
    """Return a journal lookback once it parses as a positive duration.

    The provenance note is best effort, so an unparsable span would otherwise
    be swallowed by the note's own failure handling: the command would write a
    snapshot with a generic ``journal query failed`` note and still exit 0,
    leaving the usage error invisible. Validating at parse time reports it the
    way every other bad option is reported.

    Returns
    -------
    str
        The span, unchanged.

    Raises
    ------
    argparse.ArgumentTypeError
        When the span is not a positive duration such as ``30m`` or ``1h``.
    """
    try:
        window_seconds(text)
    except ValueError as error:
        msg = "must be a positive duration such as 30m or 1h"
        raise argparse.ArgumentTypeError(msg) from error
    return text


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


def _journal_note(
    journal: cabc.Callable[..., cabc.Mapping[str, object]], span: str
) -> dict[str, object]:
    """Ask the journal what it can offer, tolerating any failure.

    The reader reports its own failure rather than returning a placeholder, so
    the conversion to an unavailable note happens here: the snapshot comes
    from the store and must not be lost to a side query.

    Returns
    -------
    dict[str, object]
        The journal summary, or an explicit unavailable note.
    """
    try:
        result = journal(since=span)
    except Exception:  # ruff: ignore[blind-except] - provenance must never fail the snapshot.
        return {"available": False, "reason": "journal query failed"}
    return dict(result)


def _report(snapshot: Snapshot, output: Path) -> None:
    """Print what the snapshot holds, including its gaps."""
    stats = snapshot.stats()
    print(f"wrote {output}")
    print(
        f"events={stats['events']} labelled={stats['labelled']} "
        f"identities={stats['identities']} gaps={len(snapshot.gaps)}"
    )
    print(f"window {stats['span_start']} to {stats['span_end']}")
    print(
        f"admission times: recorded={stats['admission_recorded']} "
        f"usable={stats['admission_usable']} missing={stats['admission_missing']} "
        f"after their own label={stats['admission_unusable']}"
    )
    if stats["labelled"] == 0:
        print(
            "no attributed hit or miss was recorded, so the benchmark will "
            "report insufficient data rather than invent any",
            file=sys.stderr,
        )
    if stats["admission_usable"] == 0:
        print(
            "no store row carries an admission time at or before its own label, "
            "so the benchmark cannot predict any event and will report "
            "insufficient admission times rather than use the label time",
            file=sys.stderr,
        )


def _utc_now() -> dt.datetime:
    """Return the current time in UTC.

    Returns
    -------
    datetime
        The host clock, in UTC.
    """
    return dt.datetime.now(dt.UTC)


@dc.dataclass(frozen=True, slots=True)
class Edges:
    """The external edges the dataset command reads through.

    They travel together because every test that replaces one replaces the
    others: a fixed clock without a stub reader would still touch this host's
    store, and a stub writer without a clock would stamp the snapshot with the
    wall time the test is trying to exclude.

    Attributes
    ----------
    journal : Callable[..., Mapping[str, object]]
        Describes the user journal, or reports why it cannot.
    now : Callable[[], datetime]
        The clock the snapshot records.
    read_store : Callable[[Path], DecodedStore | int]
        Reads the outcome store, or returns an exit status on failure.
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

    journal: cabc.Callable[..., cabc.Mapping[str, object]] = count_journal
    now: cabc.Callable[[], dt.datetime] = _utc_now
    read_store: cabc.Callable[[Path], DecodedStore | int] = _read
    write_snapshot: cabc.Callable[[Snapshot, Path], int] = _write
    resolve_store: cabc.Callable[[Path | None], Path] = _store_path
    load_config: cabc.Callable[[], Config] = _read_config
