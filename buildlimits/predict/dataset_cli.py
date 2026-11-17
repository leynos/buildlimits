"""The build-limits-predict-dataset command.

It reads this host's live cache-outcome store, redacts it into the versioned
snapshot, and says how much history the snapshot really represents.
"""

import argparse
import datetime as dt
import os
import sys
import typing as typ
from pathlib import Path

from buildlimits import outcome_store_io
from buildlimits.metrics_query import window_seconds
from buildlimits.outcome_record_format import RecordFailure
from buildlimits.outcome_store_salt import store_path
from buildlimits.predict.config import load_config
from buildlimits.predict.dataset import (
    DEFAULT_JOURNAL_SPAN,
    Snapshot,
    build_snapshot,
    count_journal,
)
from buildlimits.predict.snapshot_store import save_snapshot

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_record_format import DecodedStore

DEFAULT_OUTPUT = Path("build/predict/history.jsonl")


def main(
    argv: list[str] | None = None,
    *,
    journal: cabc.Callable[..., cabc.Mapping[str, object]] = count_journal,
) -> int:
    """Extract the live history into the benchmark snapshot.

    Parameters
    ----------
    argv : list[str] | None
        Command-line arguments, or process arguments when absent.
    journal : Callable[..., Mapping[str, object]]
        Callable used to describe the user journal; tests inject a stub.

    Returns
    -------
    int
        0 on success, 1 when the store cannot be read or the snapshot cannot
        be written, 2 on invalid usage or an unreadable parameter file.
    """
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
        config = load_config()
    except (TypeError, ValueError, OSError) as error:
        print(f"build-limits-predict-dataset: {error}", file=sys.stderr)
        return 2
    try:
        store = _store_path(options.store)
    except (OSError, ValueError) as error:
        print(f"build-limits-predict-dataset: {error}", file=sys.stderr)
        return 1
    if _same_file(store, options.output):
        print(
            "build-limits-predict-dataset: --output must not be the store being "
            f"read ({store}); choose another path",
            file=sys.stderr,
        )
        return 2
    decoded = _read(store)
    if isinstance(decoded, int):
        return decoded
    note = (
        {"available": False, "reason": "journal query skipped"}
        if options.skip_journal
        else _journal_note(journal, options.journal_span)
    )
    snapshot = build_snapshot(
        decoded,
        now=dt.datetime.now(dt.UTC),
        journal=note,
        gap_threshold_seconds=config.gap_threshold_seconds,
    )
    return _write(snapshot, options.output)


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


def _same_file(first: Path, second: Path) -> bool:
    """Return whether two paths name the same file.

    Resolving both paths means a relative spelling, a symlink or an existing
    hard link cannot disguise the store as a separate output. A path that
    does not exist yet has no inode to compare, so its resolved form is
    compared instead.

    Returns
    -------
    bool
        Whether writing ``second`` would overwrite ``first``.
    """
    try:
        return Path(first).samefile(second)
    except OSError:
        return first.resolve() == second.resolve()


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
    if stats["labelled"] == 0:
        print(
            "no attributed hit or miss was recorded, so the benchmark will "
            "report insufficient data rather than invent any",
            file=sys.stderr,
        )
