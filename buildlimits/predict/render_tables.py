"""The tables a reviewer reads in the prediction report.

Held-out comparison, calibration, ablation and origin tables are pure
formatting over the result document. They live apart from
`buildlimits.predict.render` so each table can be read next to the values it
prints, and so the prose that introduces the report stays in one place.
"""

import typing as typ

from buildlimits.predict.formatting import MISSING, as_count, as_percent, as_score

if typ.TYPE_CHECKING:
    import collections.abc as cabc

UNDERPOWERED = "under-powered"


def comparison_table(
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


def calibration_table(
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


def ablation_table(document: cabc.Mapping[str, object]) -> list[str]:
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


def origin_table(
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
