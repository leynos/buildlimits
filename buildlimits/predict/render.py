"""Render the result document as the tables a reviewer reads.

The durable report is ASCII only and holds no measured value, so the same
document always renders the same bytes. Latency is rendered separately and
written to ``timings.json`` instead.
"""

import typing as typ

from buildlimits.predict.evaluation import STATUS_OK

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_MISSING = "-"


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
    lines += _difference_table(predictors, ranking)
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
    status = document["status"]
    lines = [
        "# Cache-hit prediction benchmark",
        "",
        f"- Status: **{status}**",
        f"- Verdict: {document['verdict']}",
        f"- Predictors compared: {count}",
        (
            f"- History: {_n(stats['events'])} events, "
            f"{_n(stats['labelled'])} labelled "
            f"({_pct(stats['labelled_fraction'])}), "
            f"{_n(stats['identities'])} identities"
        ),
        f"- Window: {stats['span_start']} to {stats['span_end']}",
        (f"- Gaps over threshold: {len(typ.cast('list[object]', stats['gaps']))}"),
        (
            f"- Split: {_n(split['train_labelled'])} tuning, "
            f"{_n(split['heldout_labelled'])} held-out"
        ),
        (
            f"- Store: {_n(source['records_total'])} rows from salt "
            f"`{source['salt_id']}` written {source['written_at']}"
        ),
        f"- Journal: {_journal_note(journal)}",
        f"- Snapshot fingerprint: `{data['fingerprint']}`",
        f"- Commit: `{document['git_sha']}`",
        "",
    ]
    if status != STATUS_OK:
        lines += [
            (
                "> **Insufficient data.** These numbers describe the history "
                "that is available; no synthetic result is substituted."
            ),
            "",
        ]
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
    return f"{_n(journal.get('records'))} records, context only"


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
            f"| `{name}` | {_num(heldout['brier'])} "
            f"| {_num(heldout['log_loss'])} "
            f"| {_num(heldout['accuracy'])} | {_pct(heldout['coverage'])} "
            f"| {_num(_calibration_error(heldout))} |"
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
        f"| {_num(bin_['lower'])}-{_num(bin_['upper'])} "
        f"| {bin_['count']} | {_num(bin_['mean_probability'])} "
        f"| {_num(bin_['observed_rate'])} |"
        for bin_ in bins
    )
    lines.append("")
    return lines


def _difference_table(
    predictors: cabc.Mapping[str, dict[str, object]],
    ranking: cabc.Sequence[str],
) -> list[str]:
    """Return block-bootstrap differences from the base rate.

    Returns
    -------
    list[str]
        Markdown lines.
    """
    lines = [
        "## Difference from the base rate",
        "",
        (
            "Negative means fewer Brier points than the base rate. The "
            "interval is a block bootstrap over contiguous held-out events."
        ),
        "",
        "| Predictor | Difference | 95% interval | Paired rows |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name in ranking:
        interval = predictors[name]["against_base_rate"]
        if interval is None:
            continue
        value = typ.cast("dict[str, object]", interval)
        lines.append(
            f"| `{name}` | {_num(value['difference'])} "
            f"| [{_num(value['low'])}, {_num(value['high'])}] "
            f"| {_n(value['rows'])} |"
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
                f"| {mode} | `{component}` | {_num(value['brier'])} "
                f"| {_num(value['log_loss'])} | {_pct(value['coverage'])} |"
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
                _MISSING
                if origin not in by_origin
                else _num(by_origin[origin]["brier"])
                for origin in origins
            )
            + " |"
        )
    lines.append("")
    return lines


def render_timings(timings: cabc.Mapping[str, object]) -> str:
    """Render the measured prediction latency as Markdown.

    Returns
    -------
    str
        The latency table, kept out of the durable report.
    """
    entries = typ.cast("dict[str, dict[str, object]]", timings["predictors"])
    lines = [
        "## Prediction latency",
        "",
        "Measured on this host; not part of the deterministic JSON.",
        "",
        "| Predictor | Calls | Total ms | Mean us | Held-out mean us |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for name in sorted(entries):
        value = entries[name]
        seconds = typ.cast("dict[str, float]", value["seconds"])
        each = typ.cast("dict[str, float]", value["microseconds_each"])
        lines.append(
            f"| `{name}` | {_n(value['predictions'])} "
            f"| {1e3 * seconds['overall']:.3f} | {each['overall']:.3f} "
            f"| {each['held-out']:.3f} |"
        )
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _num(value: object) -> str:
    """Format a score or rate as four decimals.

    Returns
    -------
    str
        Four decimals, or a dash when absent.
    """
    return _MISSING if value is None else f"{typ.cast('float', value):.4f}"


def _n(value: object) -> str:
    """Format a count with thousands separators.

    Returns
    -------
    str
        The count, or a dash when absent.
    """
    return _MISSING if value is None else f"{typ.cast('int', value):,}"


def _pct(value: object) -> str:
    """Format a fraction as a percentage.

    Returns
    -------
    str
        One decimal place, or a dash when absent.
    """
    return _MISSING if value is None else f"{100 * typ.cast('float', value):.1f}%"
