"""The report's data-sufficiency block: what the labels rest on.

The block is deliberately about the input, not the scores. It names the
observer's denominators, the retained rows, the labelled share, the
admission-time coverage, the labels withheld for an unverified time, and the
closed-history loss, so a reader can tell a negative result from a history that
never carried enough evidence. It lives apart from the comparison tables in
:mod:`buildlimits.predict.render_tables` because it reads the snapshot's data
block rather than the result document's scores.
"""

import typing as typ

from buildlimits.predict.formatting import (
    TRUNCATED_OBSERVER_NOTE,
    admission_line,
    as_count,
    as_counts,
    as_percent,
    generation_unavailable_line,
)
from buildlimits.predict.report_blocks import report_data

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_DIGEST_LENGTH = 64
"""The length of a salted digest, as every other digest in the store states."""

_SHORT_DIGEST_LENGTH = 12
"""How much of a digest a report prints; enough to tell generations apart."""


def data_sufficiency(document: cabc.Mapping[str, object]) -> list[str]:
    """Return the counts that explain what the labels rest on.

    Returns
    -------
    list[str]
        Markdown lines.
    """
    blocks = report_data(document)
    stats = blocks.stats
    source = blocks.source
    journal = blocks.journal
    lines = [
        "## Data sufficiency",
        "",
        _rows_read_line(stats, source),
        (
            f"- Labels: {as_count(stats['labelled'])} of "
            f"{as_count(stats['events'])} events "
            f"({as_percent(stats['labelled_fraction'])}), "
            f"{as_count(stats['identities'])} distinct identities"
        ),
        _unverified_line(stats),
        admission_line(stats),
    ]
    diagnostics = blocks.diagnostics
    if diagnostics is None:
        lines.append(
            "- Label-loss diagnostics: unavailable; this snapshot predates "
            "the retained-store accounting."
        )
    else:
        lines += _diagnostics_lines(typ.cast("dict[str, object]", diagnostics))
    # The journal's observer counts are independent of the retained-store
    # block, so they are still worth printing when the store counts are not.
    lines += _observer_lines(journal)
    lines += [
        "",
        (
            "An ambiguous or unavailable window remains `unknown`; the report "
            "never turns a missing hit into a miss."
        ),
        "",
    ]
    return lines


def _diagnostics_lines(diagnostics: cabc.Mapping[str, object]) -> list[str]:
    """Return the retained-store and closed-history bullets.

    Returns
    -------
    list[str]
        Markdown lines.
    """
    store = typ.cast("dict[str, object]", diagnostics["store"])
    history = typ.cast("dict[str, object]", diagnostics["history"])
    return [
        (
            f"- Label coverage among outcome rows: {as_count(store['labels'])} of "
            f"{as_count(store['outcome_records'])}"
        ),
        (
            f"- Label coverage among cacheable compilations: "
            f"{as_count(store.get('cacheable_labels'))} of "
            f"{as_count(store.get('cacheable_records'))}"
        ),
        f"- By cache result: {as_counts(store['by_result'])}",
        f"- By outcome reason: {as_counts(store['by_reason'])}",
        f"- By attribution: {as_counts(store['by_attribution'])}",
        f"- By origin: {as_counts(store['by_origin'])}",
        f"- By parser class: {as_counts(store['by_parser_class'])}",
        _generation_line(store),
        _closed_history_line(history),
    ]


def _generation_line(store: cabc.Mapping[str, object]) -> str:
    """Return the per-generation label-loss bullet for one store block.

    A snapshot written before the dimension existed carries no map, and a
    document whose store holds no rows carries an empty one. Both are reported
    as unavailable rather than as a measured zero, because neither shows which
    generation produced any label. A present map is printed as measured counts,
    with the digest shortened so a report names a generation without repeating
    64 characters per row; the store still holds the full key.

    Returns
    -------
    str
        A measured breakdown, or the statement of why none is available.
    """
    counts = store.get("by_generation")
    if not isinstance(counts, dict) or not counts:
        return f"- Label loss {generation_unavailable_line()}"
    labelled = store.get("labels_by_generation")
    labelled = labelled if isinstance(labelled, dict) else {}
    pairs = ", ".join(
        f"{_short_digest(key)}={_labelled_of(value, labelled.get(key))}"
        for key, value in sorted(typ.cast("dict[str, object]", counts).items())
    )
    return f"- By generation (rows, of which labelled): {pairs}"


def _labelled_of(rows: object, labelled: object) -> str:
    """Return one generation's row count with its labelled share.

    A generation that produced many refusals and a generation that produced
    none read identically when only the row count is printed. Showing the
    labelled share beside it is what turns the bucket into evidence about
    label loss rather than about traffic.

    Returns
    -------
    str
        ``rows/labels`` for one bucket, so ``12/3`` is three labels in twelve
        rows.
    """
    labelled_count = labelled if isinstance(labelled, int) else 0
    return f"{as_count(rows)}/{labelled_count}"


def _short_digest(key: str) -> str:
    """Return the short form of one generation bucket name.

    Returns
    -------
    str
        The first twelve characters of a digest, or the bucket name unchanged
        when it is not a digest. ``unrecorded`` must stay recognizable: it is
        the count of rows that name no generation, not a generation of its own.
    """
    return key[:_SHORT_DIGEST_LENGTH] if len(key) == _DIGEST_LENGTH else key


def _closed_history_line(history: cabc.Mapping[str, object]) -> str:
    """Return one honest statement of what closed history the snapshot saw.

    A count that was never measured must not read as a measured zero, so the
    two uninspected cases name themselves instead of printing counts. The
    measured branch names the quarantined files as well, because the plain
    command report already prints that count and a reader comparing the two
    reports must not see one of them omit evidence the snapshot holds.

    Returns
    -------
    str
        A measured counts line, or the reason no count is available.
    """
    uninspected = _UNINSPECTED_HISTORY.get(_history_state(history))
    if uninspected is not None:
        return uninspected
    return (
        f"- Closed history: archived {as_count(history['archived'])}, "
        f"sealed {as_count(history['sealed'])}, "
        f"gaps {as_count(history['gaps'])}, "
        f"quarantined {as_count(history['quarantined'])}"
        + (" (gap list truncated)" if history.get("gaps_truncated") else "")
    )


# The two states in which closed history was not measured, so no count may be
# printed as one. A state outside this table was measured.
_UNINSPECTED_HISTORY = {
    "active_only": "- Closed history: not considered (the active file alone was read)",
    "inventory_unavailable": (
        "- Closed history: inventory unavailable; rows already read are included"
    ),
}


def _history_state(history: cabc.Mapping[str, object]) -> str:
    """Return which unmeasured state the history block reports, if any.

    ``active_only`` takes precedence, so a block that somehow claims both is
    described as the narrower read rather than the failed scan.

    Returns
    -------
    str
        ``active_only``, ``inventory_unavailable``, or ``measured``.
    """
    states = ("active_only", "inventory_unavailable")
    return next((state for state in states if history.get(state)), "measured")


def _rows_read_line(
    stats: cabc.Mapping[str, object], source: cabc.Mapping[str, object]
) -> str:
    """Return the row-count line, tolerating a document without source counts.

    Returns
    -------
    str
        The Markdown bullet naming outcome and summary rows.
    """
    events = stats.get("events")
    return (
        f"- Rows read: {as_count(source.get('records_total', events))} "
        f"({as_count(source.get('outcome_records', events))} outcome rows, "
        f"{as_count(source.get('summary_records', 0))} window summaries)"
    )


def _unverified_line(stats: cabc.Mapping[str, object]) -> str:
    """Return the bullet naming labels withheld for an unverified label time.

    A snapshot written before the provenance marker existed carries no count,
    so an absent value reads as the dash rather than a measured zero. The
    sentence states the consequence as well: those labels are neither released
    nor scored, which is why the label gate can be met by fewer usable rows.

    Returns
    -------
    str
        The Markdown bullet.
    """
    return (
        f"- Labels withheld for an unverified label time: "
        f"{as_count(stats.get('unverified_label_time'))}; they are neither "
        "released nor scored."
    )


def _omission_line(block: cabc.Mapping[str, object]) -> str:
    """Return the bullet naming the categories a published outcome can take.

    A published outcome can become an appended outcome row, a pre-store
    omission, a discarded write or an outstanding queued write. The bullet
    names those categories without claiming they reconcile, because the counts
    cannot be correlated: an append also counts a reading's window summary, a
    discard also counts an eviction, and a queued write still outstanding is
    not measured at all.

    Returns
    -------
    str
        The Markdown bullet.
    """
    unrecorded = block.get("record_omitted_unrecorded")
    omitted = (
        f"{as_count(block.get('record_omitted'))}, by reason "
        f"{as_counts(block.get('by_omission_reason'))}"
    )
    if unrecorded:
        omitted = (
            f"{omitted}; {as_count(unrecorded)} published outcomes carried no "
            "omission field, so their omission is unknown rather than measured"
        )
    return (
        f"- Observer outcomes without a store row: {omitted}. A published "
        "outcome falls into one of four categories: an appended outcome row, a "
        "pre-store omission, a discarded write or an outstanding queued write. "
        "The report does not prove that identity, because it cannot correlate "
        "the terms: the appended total also counts a reading's window summary, "
        "the discarded total also counts evictions, and a queued write still "
        "outstanding is not measured"
    )


def _observer_lines(journal: cabc.Mapping[str, object]) -> list[str]:
    """Return the recorder's own counts from the journal note.

    Returns
    -------
    list[str]
        Markdown lines, or an explicit absence note.
    """
    observer = journal.get("observer")
    if observer is None or not isinstance(observer, dict):
        return [
            (
                "- Observer counts: unavailable; the snapshot carries no "
                "observer-count block."
            )
        ]
    block = typ.cast("dict[str, object]", observer)
    lines = [
        (
            f"- Observer: {as_count(block.get('requests'))} admitted requests, "
            f"{as_count(block.get('outcomes'))} published outcomes, "
            f"{as_count(block.get('store_records'))} rows appended, "
            f"{as_count(block.get('store_discarded'))} dropped or evicted"
        ),
        _omission_line(block),
        (
            f"- Observer readings: {as_count(block.get('readings'))} completed, "
            f"{as_count(block.get('readings_skipped'))} skipped, "
            f"{as_count(block.get('readings_failed'))} failed"
        ),
        f"- Observer by cache path: {as_counts(block.get('by_cache_path'))}",
        f"- Observer by result: {as_counts(block.get('by_result'))}",
        f"- Observer by reason: {as_counts(block.get('by_reason'))}",
        (
            f"- Observer by store operation: "
            f"{as_counts(block.get('by_store_operation'))}; only an append adds "
            "a row, a compaction reports the rows it retained and a seal the "
            "rows it released"
        ),
        (f"- Observer by store reason: {as_counts(block.get('by_store_reason'))}"),
        (f"- Observer by gate reason: {as_counts(block.get('by_gate_reason'))}"),
    ]
    if journal.get("truncated"):
        lines.append(f"- {TRUNCATED_OBSERVER_NOTE}")
    return lines
