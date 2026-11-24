"""Print the dataset snapshot's label-loss and retention evidence.

The command owns the files; this adapter owns only the report a user reads. It
receives the fully built snapshot, so every count it prints has already been
validated by the snapshot loader.
"""

import sys
import typing as typ

from buildlimits.predict.accounting import UNRECORDED
from buildlimits.predict.formatting import (
    TRUNCATED_OBSERVER_NOTE,
    as_counts,
    generation_unavailable_line,
)
from buildlimits.predict.journal_provenance import JournalCounts
from buildlimits.utc_text import utc_text

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from buildlimits.predict.accounting import HistoryLoss, StoreAccounting
    from buildlimits.predict.dataset import Snapshot
    from buildlimits.predict.journal_provenance import (
        JournalProvenance,
        ObserverAccounting,
    )


def _observer_measured_anything(observer: ObserverAccounting) -> bool:
    """Report whether any observer counter in the window is non-zero.

    The scalar counters are the denominators every map is drawn from. A window
    that recorded only a failed reading, for example, has zero requests and
    outcomes but a non-zero failure count, and that failure must not be hidden.
    A window that recorded only a gate transition is the same kind of case:
    every scalar is zero, but the gate reason map holds the transition, and
    the maps are printed beside the scalars.

    Returns
    -------
    bool
        True when at least one observer count or closed-vocabulary map was
        recorded.
    """
    scalars = (
        observer.requests,
        observer.outcomes,
        observer.record_omitted,
        observer.record_omitted_unrecorded,
        observer.store_records,
        observer.store_discarded,
        observer.readings,
        observer.readings_skipped,
        observer.readings_failed,
    )
    maps = (
        observer.by_cache_path,
        observer.by_backend,
        observer.by_result,
        observer.by_reason,
        observer.by_attribution,
        observer.by_origin,
        observer.by_omission_reason,
        observer.by_store_operation,
        observer.by_store_reason,
        observer.by_gate_reason,
        observer.by_reading_reason,
    )
    return any(scalars) or any(maps)


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
    print(f"unverified label times: {stats.unverified_label_time}")
    print(
        f"timing evidence: scored={stats.scored} "
        f"excluded={as_counts(stats.excluded)} "
        f"duplicate attempts={stats.duplicates} "
        f"conflicting attempts={stats.conflicts}"
    )
    _report_diagnostics(snapshot)
    if stats.labelled == 0:
        print(
            "no attributed hit or miss was recorded, so the benchmark will "
            "report insufficient data rather than invent any",
            file=sys.stderr,
        )
    if stats.unverified_label_time:
        print(
            f"{stats.unverified_label_time} labelled rows carry no proof of when "
            "their label became known, so the benchmark withholds them instead "
            "of releasing them early",
            file=sys.stderr,
        )
    if stats.admission_usable == 0:
        print(
            "no store row carries an admission time at or before its own label, "
            "so the benchmark cannot predict any event and will report "
            "insufficient admission times rather than use the label time",
            file=sys.stderr,
        )
    elif stats.labelled and stats.unverified_label_time == stats.labelled:
        print(
            "no store row proves when its label became known, so the benchmark "
            "cannot release any label and will report insufficient "
            "label-availability times rather than use a completion time",
            file=sys.stderr,
        )


def _report_diagnostics(snapshot: Snapshot) -> None:
    """Print the retained-store counts, then the journal's own evidence.

    The journal block is reported whichever way the store block goes: it is
    evidence about the recorder, not about the retained rows, so a snapshot
    that predates the store accounting still carries counts worth reading.
    """
    if snapshot.diagnostics is None:
        print("label loss: no diagnostics recorded in this snapshot")
    else:
        _report_store(snapshot.diagnostics.store)
    if snapshot.journal is not None:
        _report_journal(snapshot.journal)
    if snapshot.diagnostics is not None:
        _report_history(snapshot.diagnostics.history)


def _report_store(store: StoreAccounting) -> None:
    """Print the retained rows by their closed vocabularies."""
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
    print(_generation_line(store.by_generation))


def _generation_line(by_generation: cabc.Mapping[str, int]) -> str:
    """Return the per-generation line for one store's retained rows.

    A history written before the field existed has no generation evidence, and
    its rows count under ``unrecorded`` alone. Printing that as a measurement
    would report a dimension as zero rather than as absent, so the unavailable
    sentence is kept for exactly that case.

    Returns
    -------
    str
        The measured breakdown, or the statement that none is available.
    """
    if not by_generation or set(by_generation) <= {UNRECORDED}:
        return generation_unavailable_line()
    return f"by generation: {as_counts(by_generation)}"


def _report_history(history: HistoryLoss) -> None:
    """Print the closed-history inventory, or why it was not measured."""
    if history.active_only:
        print("closed history: not considered (the active file alone was read)")
        return
    if history.inventory_unavailable:
        print("closed history: inventory unavailable; rows already read are included")
        return
    print(
        f"closed history: archived={history.archived} sealed={history.sealed} "
        f"gaps={history.gaps} quarantined={history.quarantined} "
        f"retention floor={history.retention_floor}"
        + (" (gap list truncated)" if history.gaps_truncated else "")
    )


def _report_journal(note: JournalProvenance) -> None:
    """Print the recorder's own counts, which the store cannot provide.

    A truncated query returns only the newest records within the cap, so its
    counts are a lower bound on the window. The same sentence the rendered
    report uses marks them partial here, and a complete window prints nothing
    extra.
    """
    if not isinstance(note, JournalCounts):
        return
    observer = note.observer
    if observer is None:
        print("observer: the snapshot carries no observer-count block")
        return
    if not _observer_measured_anything(observer):
        print("observer: the journal window held no observer counts")
        return
    print(
        f"observer: requests={observer.requests} outcomes={observer.outcomes} "
        f"record omitted={observer.record_omitted} "
        f"omission not recorded={observer.record_omitted_unrecorded} "
        f"store appends={observer.store_records} "
        f"dropped={observer.store_discarded} readings={observer.readings} "
        f"skipped={observer.readings_skipped} failed={observer.readings_failed}"
    )
    print(f"observer by cache path: {as_counts(observer.by_cache_path)}")
    print(f"observer by result: {as_counts(observer.by_result)}")
    print(f"observer by reason: {as_counts(observer.by_reason)}")
    print(f"observer by omission reason: {as_counts(observer.by_omission_reason)}")
    print(f"observer by store operation: {as_counts(observer.by_store_operation)}")
    print(f"observer by store reason: {as_counts(observer.by_store_reason)}")
    print(f"observer by gate reason: {as_counts(observer.by_gate_reason)}")
    if note.truncated:
        print(f"observer: {TRUNCATED_OBSERVER_NOTE}")
