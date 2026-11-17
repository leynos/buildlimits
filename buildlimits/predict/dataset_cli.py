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
from buildlimits.outcome_record_format import RecordFailure
from buildlimits.outcome_store_salt import store_path
from buildlimits.predict.config import load_config
from buildlimits.predict.dataset import (
    DEFAULT_JOURNAL_SPAN,
    Snapshot,
    build_snapshot,
    count_journal,
    save_snapshot,
)

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
        0 on success, 1 when the store cannot be read, 2 on invalid usage.
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
    decoded = _read(options.store)
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
        gap_threshold_seconds=load_config().gap_threshold_seconds,
    )
    save_snapshot(options.output, snapshot)
    _report(snapshot, options.output)
    return 0


def _read(store: Path | None) -> DecodedStore | int:
    """Read the store, reporting the reason a refusal is fatal.

    Returns
    -------
    DecodedStore | int
        An exit status on failure, otherwise the decoded store.
    """
    path = store_path(os.environ) if store is None else store
    try:
        decoded = outcome_store_io.read_store(path)
    except OSError as error:
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
