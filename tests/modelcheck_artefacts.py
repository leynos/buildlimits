"""Read the matrix artefacts ``make model-check`` writes.

The checker run is expensive and its bounds are the plan's, so the Makefile
target owns it; judging the result is cheap and belongs in the ordinary test
run. That split leaves one question the judgement cannot answer by itself:
whether the file it just read describes the model it just loaded. Reading is
therefore part of the judgement, and it lives here so both the end-to-end
module and any future reader share one definition of where the artefacts are
and when one is too old to trust.
"""

import json
import typing as typ
from pathlib import Path

import pytest

type Record = dict[str, object]
"""One matrix row: the property or probe a run judged, and its outcome."""

ROOT = Path(__file__).resolve().parents[1]
"""The checkout root, which is where ``make model-check`` writes its results."""

MODELS = {
    "coupling": ROOT / "models" / "admission" / "coupling",
    "transaction": ROOT / "models" / "admission" / "transaction",
}
"""The two W1 model directories, each the source its artefact must postdate."""

_TRACES = "traces"
"""The directory ``--save-traces`` writes under a model, which is output."""


def read_records(model: str) -> list[Record]:
    """Return one model's matrix artefact records.

    Parameters
    ----------
    model : str
        The model directory's name, a key of :data:`MODELS`.

    Returns
    -------
    list[dict[str, object]]
        The decoded array ``modelcheck check --json`` wrote for that model.

    Notes
    -----
    An absent or stale artefact fails rather than skips: a skip would report a
    matrix that was not run against the source under test as a pass.
    """
    directory = MODELS[model]
    results = ROOT / ".model-check" / f"{model}.json"
    if not results.exists():
        pytest.fail(
            f"{results} is missing; run `make model-check` before this suite",
            pytrace=False,
        )
    newest_source = max(
        path.stat().st_mtime
        for path in directory.rglob("*")
        # ``traces/`` is this run's own output, written after the records;
        # only the model's sources can make the artefact stale.
        if _TRACES not in path.relative_to(directory).parts
    )
    if results.stat().st_mtime < newest_source:
        pytest.fail(
            f"{results} is older than the model; run `make model-check` again",
            pytrace=False,
        )
    return typ.cast("list[Record]", json.loads(results.read_text(encoding="utf-8")))
