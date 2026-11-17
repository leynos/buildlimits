"""Render the result document as the tables a reviewer reads.

The durable report is ASCII only and holds no measured value, so the same
document always renders the same bytes. Latency is rendered separately and
written to ``timings.json`` instead.
"""

import typing as typ

from buildlimits.predict.evaluation import (
    STATUS_INSUFFICIENT,
    STATUS_INSUFFICIENT_HELDOUT,
)
from buildlimits.predict.formatting import MISSING, as_count, as_percent, as_score

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# A status that needs explaining is looked up rather than compared in a chain:
# a new status may be added without editing this module.
_STATUS_NOTES = {
    STATUS_INSUFFICIENT: (
        "> **Insufficient data.** These numbers describe the history "
        "that is available; no synthetic result is substituted."
    ),
    STATUS_INSUFFICIENT_HELDOUT: (
        "> **Insufficient held-out evidence.** The history is large "
        "enough to run, but too little of it lies in the held-out "
        "period to compare predictors; this is not a negative result."
    ),
}


def render_markdown(document: cabc.Mapping[str, object]) -> str:
    """Render the comparison, calibration, ablation and timing tables.

    Returns
    -------
    str
        The complete Markdown report.
    """
    predictors = _predictors(document)
    ranking = typ.cast("list[str]", document["ranking"])
    lines = _header(document, len(predictors))
    lines += _comparison_table(predictors, ranking)
    lines += _calibration_table(predictors, ranking)
    lines += _difference_table(predictors, ranking, document)
    lines += _ablation_table(document)
    lines += _origin_table(predictors, ranking)
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
            f"- Replay: {as_count(replay['batches'])} batches, "
            f"{as_count(replay['embargoed_rows'])} labels withheld one batch, "
            f"{as_count(replay['unapplied_rows'])} labels after the last prediction"
        ),
        (
            f"- Store: {as_count(source['records_total'])} rows from salt "
            f"`{source['salt_id']}` written {source['written_at']}"
        ),
        f"- Journal: {_journal_note(journal)}",
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
        Either the record count or the reason no records were usable.
    """
    if not journal.get("available"):
        return f"unavailable ({journal.get('reason', 'no records')})"
    return f"{as_count(journal.get('records'))} records, context only"


def _comparison_table(
    predictors: cabc.Mapping[str, dict[str, object]],
    ranking: cabc.Sequence[str],
) -> list[str]:
    """Return the held-out comparison table, best Brier first.

    Returns
    -------
    list[str]
        Markdown lines.
    """
    lines = [
        "## Held-out comparison",
        "",
        ("| Predictor | Brier | Log loss | Accuracy | Coverage | Calibration error |"),
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name in ranking:
        heldout = typ.cast("dict[str, object]", predictors[name]["held-out"])
        lines.append(
            f"| `{name}` | {as_score(heldout['brier'])} "
            f"| {as_score(heldout['log_loss'])} "
            f"| {as_score(heldout['accuracy'])} | {as_percent(heldout['coverage'])} "
            f"| {as_score(_calibration_error(heldout))} |"
        )
    lines.append("")
    return lines


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
    ranking: cabc.Sequence[str],
) -> list[str]:
    """Return per-bin calibration for the best-ranked predictor.

    Returns
    -------
    list[str]
        Markdown lines.
    """
    if not ranking:
        return []
    name = ranking[0]
    heldout = typ.cast("dict[str, object]", predictors[name]["held-out"])
    bins = typ.cast("list[dict[str, object]]", heldout["calibration"])
    lines = [
        f"## Calibration on the held-out period: `{name}`",
        "",
        "| Bin | Count | Mean predicted | Observed |",
        "| --- | ---: | ---: | ---: |",
    ]
    lines.extend(
        f"| {as_score(bin_['lower'])}-{as_score(bin_['upper'])} "
        f"| {bin_['count']} | {as_score(bin_['mean_probability'])} "
        f"| {as_score(bin_['observed_rate'])} |"
        for bin_ in bins
    )
    lines.append("")
    return lines


def _difference_table(
    predictors: cabc.Mapping[str, dict[str, object]],
    ranking: cabc.Sequence[str],
    document: cabc.Mapping[str, object],
) -> list[str]:
    """Return block-bootstrap differences from the base rate.

    Returns
    -------
    list[str]
        Markdown lines.
    """
    alpha = typ.cast("float", document["verdict_alpha"])
    interval_name = f"{as_percent(1.0 - alpha)} interval"
    lines = [
        "## Difference from the base rate",
        "",
        (
            "Negative means fewer Brier points than the base rate. The "
            "interval is a block bootstrap over contiguous held-out events, "
            "widened across the family of comparisons with the base rate "
            "(Bonferroni), so the verdict as a whole holds at the level the "
            "config pins."
        ),
        "",
        f"| Predictor | Difference | {interval_name} | Paired rows |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name in ranking:
        interval = predictors[name]["against_base_rate"]
        if interval is None:
            continue
        value = typ.cast("dict[str, object]", interval)
        lines.append(
            f"| `{name}` | {as_score(value['difference'])} "
            f"| [{as_score(value['low'])}, {as_score(value['high'])}] "
            f"| {as_count(value['rows'])} |"
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
    """Return Brier per origin class over every labelled event.

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
    lines = ["## Brier by origin (all labelled events)", "", header, rule]
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
