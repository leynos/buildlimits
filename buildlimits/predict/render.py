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
from buildlimits.predict.formatting import MISSING, as_count, as_percent, as_score

if typ.TYPE_CHECKING:
    import collections.abc as cabc

UNDERPOWERED = "under-powered"
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
    lines += _comparison_table(predictors, ranking, document)
    lines += _calibration_table(predictors)
    lines += _ablation_table(document)
    lines += _origin_table(predictors, ranking)
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
        (
            f"- Admission times: {as_count(stats['admission_recorded'])} recorded, "
            f"{as_count(stats['admission_usable'])} usable for a prediction, "
            f"{as_count(stats['admission_missing'])} missing, "
            f"{as_count(stats['admission_unusable'])} recorded after their own label"
        ),
        (
            f"- Store: {as_count(source['records_total'])} rows from salt "
            f"`{source['salt_id']}` written {source['written_at']}"
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


def _comparison_table(
    predictors: cabc.Mapping[str, dict[str, object]],
    ranking: cabc.Sequence[str],
    document: cabc.Mapping[str, object],
) -> list[str]:
    """Return the held-out comparison table, best Brier first.

    Returns
    -------
    list[str]
        Markdown lines.
    """
    alpha = typ.cast("float", document["verdict_alpha"])
    interval_name = f"{as_percent(1.0 - alpha)} interval"
    lines = [
        "## Held-out comparison",
        "",
        (
            "Sorted by held-out Brier. `n` is the number of held-out rows the "
            "predictor answered; a predictor marked *under-powered* scored "
            f"fewer than the config's minimum, and its position says little. "
            f"A negative difference means fewer Brier points than the base "
            f"rate; the {interval_name} is a block bootstrap over contiguous "
            "held-out events, widened across the family of comparisons with "
            "the base rate (Bonferroni)."
        ),
        "",
        (
            "| Predictor | Brier | Log loss | Accuracy | Coverage | n "
            f"| Calibration error | Difference vs base rate | {interval_name} "
            "| Notes |"
        ),
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    lines.extend(_comparison_row(name, predictors[name]) for name in ranking)
    lines.append("")
    return lines


def _comparison_row(name: str, entry: cabc.Mapping[str, object]) -> str:
    """Return one row of the held-out comparison table.

    Returns
    -------
    str
        The Markdown row.
    """
    heldout = typ.cast("dict[str, object]", entry["held-out"])
    interval = entry["against_base_rate"]
    difference = MISSING
    bounds = MISSING
    if interval is not None:
        value = typ.cast("dict[str, object]", interval)
        difference = as_score(value["difference"])
        bounds = f"[{as_score(value['low'])}, {as_score(value['high'])}]"
    notes = MISSING if entry["meets_minimum"] else UNDERPOWERED
    return (
        f"| `{name}` | {as_score(heldout['brier'])} "
        f"| {as_score(heldout['log_loss'])} "
        f"| {as_score(heldout['accuracy'])} | {as_percent(heldout['coverage'])} "
        f"| {as_count(heldout['scored'])} "
        f"| {as_score(_calibration_error(heldout))} "
        f"| {difference} | {bounds} | {notes} |"
    )


def _calibration_error(heldout: cabc.Mapping[str, object]) -> float | None:
    """Return the count-weighted gap between predicted and observed rates.

    Returns
    -------
    float | None
        The expected calibration error, or None without answered rows.
    """
    bins = typ.cast("list[dict[str, object]]", heldout["calibration"])
    total = sum(typ.cast("int", bin_["count"]) for bin_ in bins)
    if total == 0:
        return None
    gaps = [
        typ.cast("int", bin_["count"])
        * abs(
            typ.cast("float", bin_["mean_probability"])
            - typ.cast("float", bin_["observed_rate"])
        )
        for bin_ in bins
        if bin_["count"]
    ]
    return sum(gaps) / total


def _calibration_table(
    predictors: cabc.Mapping[str, dict[str, object]],
) -> list[str]:
    """Return per-bin calibration for every predictor with a held-out score.

    Returns
    -------
    list[str]
        Markdown lines.
    """
    scored = [
        name
        for name, entry in predictors.items()
        if typ.cast("dict[str, object]", entry["held-out"])["scored"]
    ]
    if not scored:
        return []
    lines = [
        "## Calibration on the held-out period",
        "",
        "| Predictor | Bin | Count | Mean predicted | Observed |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for name in scored:
        heldout = typ.cast("dict[str, object]", predictors[name]["held-out"])
        bins = typ.cast("list[dict[str, object]]", heldout["calibration"])
        lines.extend(
            f"| `{name}` | {as_score(bin_['lower'])}-{as_score(bin_['upper'])} "
            f"| {bin_['count']} | {as_score(bin_['mean_probability'])} "
            f"| {as_score(bin_['observed_rate'])} |"
            for bin_ in bins
        )
    lines.append("")
    return lines


def _ablation_table(document: cabc.Mapping[str, object]) -> list[str]:
    """Return each ensemble's score with each component removed.

    Returns
    -------
    list[str]
        Markdown lines.
    """
    ablation = typ.cast("dict[str, dict[str, dict[str, object]]]", document["ablation"])
    lines = [
        "## Ablation",
        "",
        "Held-out Brier of each ensemble with one component removed.",
        "",
        "| Ensemble | Removed | Brier | Log loss | Coverage |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for mode in sorted(ablation):
        for component in sorted(ablation[mode]):
            value = ablation[mode][component]
            lines.append(
                f"| {mode} | `{component}` | {as_score(value['brier'])} "
                f"| {as_score(value['log_loss'])} | {as_percent(value['coverage'])} |"
            )
    lines.append("")
    return lines


def _origin_table(
    predictors: cabc.Mapping[str, dict[str, object]],
    ranking: cabc.Sequence[str],
) -> list[str]:
    """Return Brier per origin class over every scored event.

    The split here is by origin, not by phase: the numbers cover the tuning and
    held-out periods together, so they do not match the held-out table above.

    Returns
    -------
    list[str]
        Markdown lines.
    """
    origins = sorted({
        origin
        for name in ranking
        for origin in typ.cast("dict[str, object]", predictors[name]["by_origin"])
    })
    if not origins:
        return []
    header = "| Predictor | " + " | ".join(origins) + " |"
    rule = "| --- | " + " | ".join("---:" for _ in origins) + " |"
    lines = ["## Brier by origin (all scored events)", "", header, rule]
    for name in ranking:
        by_origin = typ.cast(
            "dict[str, dict[str, object]]", predictors[name]["by_origin"]
        )
        lines.append(
            f"| `{name}` | "
            + " | ".join(
                MISSING
                if origin not in by_origin
                else as_score(by_origin[origin]["brier"])
                for origin in origins
            )
            + " |"
        )
    lines.append("")
    return lines


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
        "",
    ])
    return lines
