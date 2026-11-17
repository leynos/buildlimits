"""Render the measured latency as Markdown.

An ensemble only combines answers its components had already produced, so its
elapsed time is reported in a separate table from single-predictor prediction
latency; the two are never added together or read as the same measurement.
"""

import typing as typ

from buildlimits.predict.formatting import as_count

if typ.TYPE_CHECKING:
    import collections.abc as cabc


_TIMING_HEADER = (
    "| Predictor | Calls | Total ms | Mean us | Held-out mean us |",
    "| --- | ---: | ---: | ---: | ---: |",
)


def render_timings(timings: cabc.Mapping[str, object]) -> str:
    """Render the measured latency as Markdown.

    Single predictors predict from features; an ensemble only combines the
    answers its components had already produced. The two are reported apart so
    a combination time is never read as a prediction time.

    Returns
    -------
    str
        The latency tables, kept out of the durable report.
    """
    lines = [
        "## Prediction latency",
        "",
        "Measured on this host; not part of the deterministic JSON.",
        "",
        *_timing_rows(typ.cast("dict[str, dict[str, object]]", timings["predictors"])),
        "",
        "### Ensemble combination latency",
        "",
        "Combining only: the component answers were already computed above.",
        "",
        *_timing_rows(typ.cast("dict[str, dict[str, object]]", timings["ensembles"])),
        "",
    ]
    return "\n".join(lines).rstrip() + "\n"


def _timing_rows(entries: cabc.Mapping[str, dict[str, object]]) -> list[str]:
    """Return one Markdown table for a set of timing entries.

    Returns
    -------
    list[str]
        The header and one row per predictor, in name order.
    """
    lines = list(_TIMING_HEADER)
    for name in sorted(entries):
        value = entries[name]
        seconds = typ.cast("dict[str, float]", value["seconds"])
        each = typ.cast("dict[str, float]", value["microseconds_each"])
        lines.append(
            f"| `{name}` | {as_count(value['predictions'])} "
            f"| {1e3 * seconds['overall']:.3f} | {each['overall']:.3f} "
            f"| {each['held-out']:.3f} |"
        )
    return lines
