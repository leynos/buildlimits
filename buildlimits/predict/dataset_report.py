"""Print the dataset snapshot's label-loss and retention evidence.

The command owns the files; this adapter owns only the report a user reads. It
receives the fully built snapshot, so every count it prints has already been
validated by the snapshot loader.
"""

import sys
import typing as typ

from buildlimits.predict.formatting import as_counts
from buildlimits.predict.journal_provenance import JournalCounts
from buildlimits.utc_text import utc_text

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.predict.dataset import Snapshot
    from buildlimits.predict.journal_provenance import JournalProvenance


def report_snapshot(snapshot: Snapshot, output: Path) -> None:
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
