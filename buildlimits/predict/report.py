"""Turn one evaluation into the deterministic JSON and Markdown reports.

Latency is measured, so it cannot agree byte for byte between runs. It lives in
``timings.json`` and in the table the command prints, while ``results.json`` and
``results.md`` hold only reproducible scores: the same snapshot, config and
commit produce the same files.
"""

import dataclasses as dc
import json
import typing as typ

from buildlimits.predict.paths import same_file
from buildlimits.predict.render import render_markdown
from buildlimits.predict.report_codec import (
    interval_to_dict,
    metrics_to_dict,
    split_to_dict,
)
from buildlimits.predict.snapshot_codec import source_to_dict

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from buildlimits.predict.config import Config
    from buildlimits.predict.dataset import Snapshot
    from buildlimits.predict.evaluation import Evaluation
    from buildlimits.predict.metrics import Metrics

BENCHMARK_VERSION = 1
# The files one report writes. Kept together so a command can check every
# destination before the first write replaces anything.
RESULTS = "results.json"
MARKDOWN = "results.md"
TIMINGS = "timings.json"
RESULT_FILES = (RESULTS, MARKDOWN, TIMINGS)


@dc.dataclass(frozen=True, slots=True)
class Provenance:
    """What identifies the history and code behind one run.

    Attributes
    ----------
    snapshot : Snapshot
        The history the benchmark read.
    config : Config
        The pinned parameters.
    git_sha : str
        The commit the run was produced from.
    fingerprint : str
        The SHA-256 digest of the event stream.
    """

    snapshot: Snapshot
    config: Config
    git_sha: str
    fingerprint: str


@dc.dataclass(frozen=True, slots=True)
class Report:
    """Everything one run writes.

    Attributes
    ----------
    document : dict[str, object]
        The deterministic ``results.json`` payload.
    timings : dict[str, object]
        The measured prediction latency, kept out of ``document``.
    markdown : str
        The rendered ``results.md``.
    """

    document: dict[str, object]
    timings: dict[str, object]
    markdown: str

    def write(self, directory: Path) -> None:
        """Write both result files and the timings beside them.

        The three destinations are checked for a symbolic or hard link between
        them first, so a directory that would make two artefacts one file is
        refused before anything is written.
        """
        directory.mkdir(parents=True, exist_ok=True)
        _require_distinct_destinations(directory)
        _write_json(directory / RESULTS, self.document)
        _write_json(directory / TIMINGS, self.timings)
        (directory / MARKDOWN).write_text(self.markdown, encoding="utf-8")


def _require_distinct_destinations(directory: Path) -> None:
    """Refuse a report directory whose files alias each other.

    The three names are fixed, but a symbolic link or a hard link placed in
    the directory can still make two of them one file. The check runs before
    the first write, so a refused run leaves every artefact as it was.

    Raises
    ------
    ValueError
        If two destinations resolve to, or share an inode with, one another.
    """
    destinations = [directory / name for name in RESULT_FILES]
    for index, first in enumerate(destinations):
        for second in destinations[index + 1 :]:
            if same_file(first, second):
                msg = (
                    f"report destinations {first.name} and {second.name} are "
                    "the same file; choose a directory without a link "
                    "between them"
                )
                raise ValueError(msg)


def build_report(evaluation: Evaluation, provenance: Provenance) -> Report:
    """Assemble the document, timings and rendered Markdown.

    Returns
    -------
    Report
        The three artefacts of one benchmark run.
    """
    document = build_document(evaluation, provenance)
    timings = build_timings(evaluation)
    return Report(document, timings, render_markdown(document))


def build_document(evaluation: Evaluation, provenance: Provenance) -> dict[str, object]:
    """Return the deterministic result document.

    Returns
    -------
    dict[str, object]
        Scores, intervals, ablation and provenance.
    """
    weights = {name: list(run.weights) for name, run in evaluation.ensembles.items()}
    return {
        "benchmark_version": BENCHMARK_VERSION,
        "git_sha": provenance.git_sha,
        "status": evaluation.status,
        "verdict": evaluation.verdict,
        "config": provenance.config.as_dict(),
        "verdict_alpha": evaluation.verdict_alpha,
        "data": {
            "fingerprint": provenance.fingerprint,
            "stats": provenance.snapshot.stats(),
            "source": source_to_dict(provenance.snapshot.source),
            "journal": provenance.snapshot.journal,
        },
        "split": split_to_dict(evaluation.walk.split),
        "replay": {
            "moments": len(evaluation.walk.moments),
            "unavailable_admission": evaluation.walk.unavailable_admission,
            "applied_labels": evaluation.walk.applied_labels,
            "unapplied_labels": evaluation.walk.unapplied_labels,
        },
        "latency": {
            "file": TIMINGS,
            "note": "measured latency; not reproducible byte for byte",
        },
        "predictors": {
            name: _predictor_entry(name, block, evaluation, weights)
            for name, block in evaluation.metrics.items()
        },
        "best_single_on_training": evaluation.best_single,
        "ensembles_against_best_single": {
            name: None if interval is None else interval_to_dict(interval)
            for name, interval in evaluation.against_best_single.items()
        },
        "ablation": {
            mode: {
                component: metrics_to_dict(metrics)
                for component, metrics in removed.items()
            }
            for mode, removed in evaluation.ablation_metrics.items()
        },
        "ranking": list(evaluation.ranking()),
    }


def _predictor_entry(
    name: str,
    block: cabc.Mapping[str, Metrics],
    evaluation: Evaluation,
    weights: cabc.Mapping[str, list[float]],
) -> dict[str, object]:
    """Return one predictor's report entry.

    A fixed ensemble does not report a tuning score, because its weights were
    fitted on those labels; ``train`` is null for it rather than a
    retrospective fit presented as a prediction.

    Returns
    -------
    dict[str, object]
        Metrics per row set, the base-rate interval and any weights.
    """
    interval = evaluation.against_base_rate.get(name)
    origins = {
        key.removeprefix("origin:"): metrics_to_dict(value)
        for key, value in block.items()
        if key.startswith("origin:")
    }
    train = block.get("train")
    return {
        "overall": metrics_to_dict(block["overall"]),
        "train": None if train is None else metrics_to_dict(train),
        "held-out": metrics_to_dict(block["held-out"]),
        "by_origin": origins,
        "against_base_rate": None if interval is None else interval_to_dict(interval),
        "weights": weights.get(name),
        "meets_minimum": evaluation.meets_minimum(name),
    }


def build_timings(evaluation: Evaluation) -> dict[str, object]:
    """Aggregate measured latency per predictor and per ensemble.

    An ensemble's elapsed time covers only combining the answers its
    components had already produced, so it is reported apart from the single
    predictors' prediction latency rather than in the same table.

    Returns
    -------
    dict[str, object]
        Total and per-event seconds overall, per phase and per origin, split
        into ``predictors`` and ``ensembles``.
    """
    predicted = [
        index for index, row in enumerate(evaluation.walk.rows) if row is not None
    ]
    phases = [evaluation.walk.phases[index] for index in predicted]
    origins = [evaluation.walk.events[index].origin for index in predicted]
    return {
        "predictors": {
            name: _timing_entry(
                [series.elapsed[index] for index in predicted], phases, origins
            )
            for name, series in evaluation.walk.series.items()
        },
        "ensembles": {
            name: _timing_entry(
                [run.elapsed[index] for index in predicted], phases, origins
            )
            for name, run in evaluation.ensembles.items()
        },
    }


def _matching(
    values: cabc.Sequence[float], keys: cabc.Sequence[object], wanted: object
) -> list[float]:
    """Return the values whose paired key equals ``wanted``.

    Returns
    -------
    list[float]
        The matching values, in their original order.
    """
    return [value for value, key in zip(values, keys, strict=True) if key == wanted]


def _timing_entry(
    elapsed: cabc.Sequence[float],
    phases: cabc.Sequence[object],
    origins: cabc.Sequence[str],
) -> dict[str, object]:
    """Summarize one predictor's measured latency.

    Returns
    -------
    dict[str, object]
        Seconds per scope, with the call count that produced them. The
        per-call mean of a scope with no calls is None rather than a measured
        zero.
    """
    scopes: dict[str, list[float]] = {"overall": list(elapsed)}
    for phase in ("train", "held-out"):
        scopes[phase] = _matching(elapsed, phases, phase)
    by_origin = {
        origin: sum(_matching(elapsed, origins, origin))
        for origin in sorted(set(origins))
    }
    return {
        "predictions": len(elapsed),
        "seconds": {scope: sum(values) for scope, values in scopes.items()},
        "microseconds_each": {
            scope: (None if not values else 1e6 * sum(values) / len(values))
            for scope, values in scopes.items()
        },
        "seconds_by_origin": by_origin,
    }


def _write_json(path: Path, document: cabc.Mapping[str, object]) -> None:
    """Write one deterministic JSON document."""
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
