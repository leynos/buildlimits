"""Render the result document as the tables a reviewer reads.

The durable report is ASCII only and holds no measured value, so the same
document always renders the same bytes. Latency is rendered separately and
written to ``timings.json`` instead, which the header points at.

Everything the issue asks a reader to compare sits in one place: the held-out
comparison table carries each predictor's scores, its difference from the base
rate, that difference's block-bootstrap interval and whether the predictor
scored enough held-out rows for its position to mean anything. Calibration is
reported for every predictor that scored a held-out row, not only the leader.
"""

import typing as typ

from buildlimits.predict.evaluation import (
    STATUS_INSUFFICIENT,
    STATUS_INSUFFICIENT_ADMISSION,
    STATUS_INSUFFICIENT_HELDOUT,
)
from buildlimits.predict.formatting import (
    MISSING,
    admission_line,
    as_count,
    as_percent,
)
from buildlimits.predict.render_tables import (
    UNDERPOWERED,
    ablation_table,
    calibration_table,
    comparison_table,
    data_sufficiency,
    origin_table,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# ``UNDERPOWERED`` stays here because it marks a position a reader must read as
# provisional, so it belongs beside the report's other reader-facing wording
# even though the table that prints it lives in ``render_tables``.
__all__ = ["UNDERPOWERED", "render_markdown"]

# A snapshot spanning one domain needs no note; a longer list is named up to a
# bounded prefix so a damaged store cannot make the report unbounded.
_MULTI_DOMAIN = 2
_DOMAINS_LISTED = 3
# A status that needs explaining is looked up rather than compared in a chain:
# a new status may be added without editing this module.
_STATUS_NOTES = {
    STATUS_INSUFFICIENT: (
        "> **Insufficient data.** These numbers describe the history "
        "that is available; no synthetic result is substituted."
    ),
    STATUS_INSUFFICIENT_ADMISSION: (
        "> **Insufficient admission times.** The history is large enough "
        "to run, but its records carry no admission time, so no event "
        "could have been predicted before its label was known. No "
        "prediction is reported; the counts describe what is missing."
    ),
    STATUS_INSUFFICIENT_HELDOUT: (
        "> **Insufficient held-out evidence.** The history is large "
        "enough to run, but too little of it lies in the held-out "
        "period to compare predictors; this is not a negative result."
    ),
}


def render_markdown(document: cabc.Mapping[str, object]) -> str:
    """Render the comparison, calibration, ablation and origin tables.

    Returns
    -------
    str
        The complete Markdown report.
    """
    predictors = _predictors(document)
    ranking = typ.cast("list[str]", document["ranking"])
    lines = _header(document, len(predictors))
    lines += data_sufficiency(document)
    lines += comparison_table(predictors, ranking, document)
    lines += calibration_table(predictors)
    lines += ablation_table(document)
    lines += origin_table(predictors, ranking)
    lines += _limitations(predictors)
    return "\n".join(lines).rstrip() + "\n"


def _predictors(
    document: cabc.Mapping[str, object],
) -> dict[str, dict[str, object]]:
    """Return the predictor entries with their nested types restored.

    Returns
    -------
    dict[str, dict[str, object]]
        One entry per predictor.
    """
    return typ.cast("dict[str, dict[str, object]]", document["predictors"])


def _header(document: cabc.Mapping[str, object], count: int) -> list[str]:
    """Return the title and provenance block.

    Returns
    -------
    list[str]
        Markdown lines.
    """
    data = typ.cast("dict[str, object]", document["data"])
    stats = typ.cast("dict[str, object]", data["stats"])
    source = typ.cast("dict[str, object]", data["source"])
    journal = typ.cast("dict[str, object]", data["journal"])
    split = typ.cast("dict[str, object]", document["split"])
    replay = typ.cast("dict[str, object]", document["replay"])
    latency = typ.cast("dict[str, object]", document["latency"])
    status = document["status"]
    lines = [
        "# Cache-hit prediction benchmark",
        "",
        f"- Status: **{status}**",
        f"- Verdict: {document['verdict']}",
        f"- Predictors compared: {count}",
        (
            f"- History: {as_count(stats['events'])} events, "
            f"{as_count(stats['labelled'])} labelled "
            f"({as_percent(stats['labelled_fraction'])}), "
            f"{as_count(stats['identities'])} identities"
        ),
        f"- Window: {stats['span_start'] or MISSING} to {stats['span_end'] or MISSING}",
        (f"- Gaps over threshold: {len(typ.cast('list[object]', stats['gaps']))}"),
        (
            f"- Split: {as_count(split['train_labelled'])} tuning, "
            f"{as_count(split['heldout_labelled'])} held-out"
        ),
        (
            f"- Replay: {as_count(replay['moments'])} admission moments, "
            f"{as_count(replay['unavailable_admission'])} events without a usable "
            f"admission time, {as_count(replay['applied_labels'])} labels "
            f"delivered before a prediction, {as_count(replay['unapplied_labels'])} "
            "labels too late to inform one"
        ),
        admission_line(stats),
        (
            f"- Store: {as_count(source['records_total'])} rows from salt "
            f"`{source['salt_id']}`{_domain_note(source.get('salt_ids'))} written "
            f"{source['written_at']}"
        ),
        f"- Journal: {_journal_note(journal)}",
        f"- Latency: measured separately in `{latency['file']}`",
        f"- Snapshot fingerprint: `{data['fingerprint']}`",
        f"- Commit: `{document['git_sha']}`",
        "",
    ]
    note = _STATUS_NOTES.get(typ.cast("str", status))
    if note is not None:
        lines += [note, ""]
    return lines


def _domain_note(value: object) -> str:
    """Describe a snapshot that spans several salt domains.

    Returns
    -------
    str
        An empty string for one domain, else a bounded phrase naming the
        domains the record counts cover.
    """
    if not isinstance(value, list) or len(value) < _MULTI_DOMAIN:
        return ""
    shown = ", ".join(
        f"`{item}`" for item in value[:_DOMAINS_LISTED] if isinstance(item, str)
    )
    suffix = ", and more" if len(value) > _DOMAINS_LISTED else ""
    return f" across {len(value)} salt domains ({shown}{suffix})"


def _journal_note(journal: cabc.Mapping[str, object]) -> str:
    """Describe the journal contribution in one phrase.

    Returns
    -------
    str
        The record count, its span, the supervisor starts it witnessed, and
        what it could not correlate.
    """
    if not journal.get("available"):
        return f"unavailable ({journal.get('reason', 'no records')})"
    restarts = typ.cast("dict[str, object]", journal.get("restarts") or {})
    note = (
        f"{as_count(journal.get('records'))} records from "
        f"{journal.get('span_start') or MISSING} to "
        f"{journal.get('span_end') or MISSING}"
    )
    count = restarts.get("count")
    if count is not None:
        note += (
            f", {as_count(count)} supervisor starts"
            if restarts.get("first") is None
            else f", {as_count(count)} supervisor starts between "
            f"{restarts['first']} and {restarts['last']}"
        )
    return f"{note}; {journal.get('correlation', 'no correlation available')}"


def _limitations(predictors: cabc.Mapping[str, dict[str, object]]) -> list[str]:
    """State what the scores do not claim.

    Returns
    -------
    list[str]
        Markdown lines.
    """
    retrospective = sorted(
        name for name, entry in predictors.items() if entry["train"] is None
    )
    lines = ["## Limitations", ""]
    if retrospective:
        lines.append(
            "- The fixed ensemble's weights were fitted on the tuning labels, so "
            "its tuning rows are excluded rather than reported as retrospective "
            "predictions: " + ", ".join(f"`{name}`" for name in retrospective) + "."
        )
    lines.extend([
        (
            "- An event with no admission time at or before its own label is "
            "not predicted or scored; it is counted above, not repaired by "
            "assumption."
        ),
        (
            "- The journal supplies counts and a retention span, not a "
            "joinable per-compilation admission record, so admission times "
            "come only from the outcome store."
        ),
        (
            "- A row whose window could not be attributed stays `unknown`; a "
            "missing hit is never read as a miss, and an ambiguous counter "
            "change is never split between concurrent requests."
        ),
        "",
    ])
    return lines
