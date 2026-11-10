"""A saved trace replaces recorded evidence only when it is whole.

:meth:`modelcheck.cli_outputs.Traces.save` writes each counterexample under the
model's ``traces/`` directory, which is tracked. Redaction withholds a decoded
string that contains a one-character credential, so a host whose environment
holds such a value produces a trace that is honest about what it withheld and
poorer as evidence than the file already committed. These tests pin both halves
of the rule: a withheld trace leaves a recorded file alone, while a whole one
replaces it as before. A withheld trace is still written where nothing would be
lost, because the marker in it says what was withheld.
"""

import json
from pathlib import Path

import pytest

from modelcheck import domain
from modelcheck.adapters.fizzbee_redaction import Redaction
from modelcheck.adapters.fizzbee_replacements import WITHHELD
from modelcheck.adapters.fizzbee_trace_redaction import redact_trace
from modelcheck.cli_outputs import Traces
from modelcheck.trace import Ending, Step, Trace

_SAVED = Path("traces") / "incident-1-L3.json"
"""Where the judgement below's counterexample is kept."""

_RECORDED = '{"recorded": "evidence"}\n'
"""Text standing in for a trace a maintainer has already committed."""


def _judgement(key: str) -> domain.Judgement:
    """Return a judgement whose counterexample state names ``key``.

    Parameters
    ----------
    key : str
        The state variable's name, as redaction left it.

    Returns
    -------
    domain.Judgement
        One finite counterexample for the ``L3`` expectation in the
        ``incident-1`` configuration, so the saved file's name is stable.
    """
    return _judgement_of(
        Trace(
            {key: False, "queue": ()},
            (Step("Go", {key: True, "queue": (1,)}),),
            Ending.FINITE,
            None,
        )
    )


def _judgement_of(trace: Trace) -> domain.Judgement:
    """Return the judgement that records ``trace`` as its counterexample.

    Parameters
    ----------
    trace : modelcheck.trace.Trace
        The path to record.

    Returns
    -------
    domain.Judgement
        One finite counterexample for the ``L3`` expectation in the
        ``incident-1`` configuration, so the saved file's name is stable.
    """
    return domain.Judgement(
        configuration="incident-1",
        expectation=domain.Holds("L3"),
        result=domain.Counterexample(
            "L3", domain.CounterexampleKind.FINITE, trace, 4, 0.1
        ),
        verdict=domain.Verdict.ERROR,
        fairness_in_force=(),
    )


def _recorded(tmp_path: Path) -> Path:
    """Write the stand-in trace and return where it was written."""
    path = tmp_path / _SAVED
    path.parent.mkdir()
    path.write_text(_RECORDED, encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "key",
    [
        pytest.param(WITHHELD, id="withheld"),
        pytest.param(f"{WITHHELD} #2", id="numbered-withheld"),
    ],
)
def test_a_withheld_trace_does_not_replace_recorded_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], key: str
) -> None:
    """A host-shortened trace is not evidence about the model.

    The environment decides which strings redaction withholds, so replacing a
    committed trace with one produced under such a host would trade evidence
    about the model for evidence about the machine. The recorded file is kept,
    and the run names the file it left alone rather than skipping it silently.
    """
    recorded = _recorded(tmp_path)

    Traces(enabled=True, model_dir=tmp_path).save((_judgement(key),))

    assert recorded.read_text(encoding="utf-8") == _RECORDED, "the file is kept"
    warning = capsys.readouterr().err
    assert _SAVED.as_posix() in warning, "the run names the file it left alone"
    assert "redaction withheld" in warning, "and says why"


def test_a_withheld_trace_is_written_where_no_evidence_exists(tmp_path: Path) -> None:
    """Nothing is lost by recording the first trace a configuration produces.

    There is no committed file to protect, and the marker tells a reader that
    part of the evidence was withheld, so the trace is written as it stands.
    """
    Traces(enabled=True, model_dir=tmp_path).save((_judgement(WITHHELD),))

    saved = (tmp_path / _SAVED).read_text(encoding="utf-8")
    assert WITHHELD in saved, "the withheld variable is named as withheld"
    assert "queue" in saved, "and the rest of the state survives"


def test_a_whole_trace_replaces_the_recorded_evidence(tmp_path: Path) -> None:
    """A trace with nothing withheld is still the run's own output.

    The guard is about evidence quality, not about refusing to write: the
    ordinary case replaces the file the way it always did.
    """
    recorded = _recorded(tmp_path)

    Traces(enabled=True, model_dir=tmp_path).save((_judgement("halted"),))

    traced = json.loads(recorded.read_text(encoding="utf-8"))
    assert traced["initial"] == {"halted": False, "queue": []}, "the new path is kept"
    assert traced["steps"][0]["action"] == "Go", "with the action it took"


def test_the_numbering_the_redaction_writes_is_the_numbering_kept(
    tmp_path: Path,
) -> None:
    """The guard reads the numbered name the redaction itself produces.

    Two state variables that both withhold to one marker are numbered so each
    survives under a name of its own. Which spelling that numbering uses is
    decided by the redaction and read by the guard, so the two run against
    each other here: a change to either must fail this test rather than
    silently let a host-shortened trace replace a committed one.
    """
    recorded = _recorded(tmp_path)
    redacted = redact_trace(
        Trace({"ax": False, "bx": True}, (), Ending.FINITE, None),
        Redaction((), ("x",)),
    )

    assert len(redacted.initial) == 2, "both variables survive under distinct names"

    Traces(enabled=True, model_dir=tmp_path).save((_judgement_of(redacted),))

    assert recorded.read_text(encoding="utf-8") == _RECORDED, "the file is kept"
