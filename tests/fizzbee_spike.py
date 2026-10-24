"""Drive the pinned FizzBee checker from a scratch directory.

The launcher writes ``<spec>.json`` beside the spec it is given and its graphs
under ``out/``. Every call therefore copies the model into the test's own
directory first, so a run never leaves a tracked tree dirty.

The environment sets ``GOMAXPROCS=2`` and ``GOMEMLIMIT=4GiB``: a mistaken
model can explore without bound, and the Go runtime's own limits stop it
before the host does. A run that outlives its timeout has its whole process
group killed, because the launcher starts a checker process of its own.
"""

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv, never a shell.
import typing as typ
from pathlib import Path

import pytest

if typ.TYPE_CHECKING:
    import collections.abc as cabc

REPOSITORY = Path(__file__).resolve().parents[1]
MODELS = REPOSITORY / "models" / "spike"
FIZZ = REPOSITORY / ".tools" / "fizzbee" / "fizz"
OPT_IN = "BUILD_LIMITS_MODEL_CHECK"

# The launcher prints wall-clock durations, a timestamped run directory and
# the frontier size it happened to hold when exploration ended. Masking them
# lets an assertion quote a line without pinning a machine's timing, and lets
# two runs of one model be compared byte for byte.
_VOLATILE = (
    (re.compile(r"out/run_[0-9_-]+"), "out/run_<timestamp>"),
    # The unit is a non-ASCII micro sign, so match "not a space or comma"
    # rather than spelling either of the two characters a reader might type.
    (re.compile(r"elapsed: [0-9.]+[^\s,]+"), "elapsed: <duration>"),
    (re.compile(r"queued: \d+"), "queued: <count>"),
    (
        re.compile(r"Time taken for model checking: .*"),
        "Time taken for model checking: <duration>",
    ),
    (
        re.compile(r"Time taken to check liveness: .*"),
        "Time taken to check liveness: <duration>",
    ),
    (re.compile(r"^Logs at /tmp/.*$", re.MULTILINE), "Logs at <logs-dir>"),
)
_NODES = re.compile(r"^Nodes: (\d+)", re.MULTILINE)
_VALID = re.compile(r"^Valid Nodes: (\d+) Unique states: (\d+)", re.MULTILINE)
_GRAPH_STATE = re.compile(r"State: (\{.*\})", re.MULTILINE)


class CheckerRun(typ.NamedTuple):
    """One checker invocation and what it left behind.

    Attributes
    ----------
    returncode : int
        The launcher's exit status.
    stdout : str
        Standard output and standard error, with volatile figures masked.
    output : Path
        The run directory under ``out/``, or the scratch root when the run
        produced none.
    """

    returncode: int
    stdout: str
    output: Path


def missing_opt_in_reason() -> str | None:
    """Return why the checker tests must skip, or ``None`` when opted in.

    Returns
    -------
    str | None
        The skip reason, naming the variable to set, or ``None``.

    Examples
    --------
    The example opts out in a copy of the environment rather than in this
    process, so it gives the same answer however the suite was invoked and
    leaves ``os.environ`` as it found it.

    >>> import os
    >>> from unittest import mock
    >>> with mock.patch.dict(os.environ, {}, clear=True):
    ...     reason = missing_opt_in_reason()
    >>> "BUILD_LIMITS_MODEL_CHECK" in reason
    True
    """
    if os.environ.get(OPT_IN) == "1":
        return None
    return f"needs the pinned FizzBee checker; set {OPT_IN}=1 (after `make fizzbee`)"


def graph_states(text: str) -> list[str]:
    r"""Return each node's state from a rendered ``graph.dot``.

    The node ids in the file are Go pointers, so they differ on every run.
    Reading the states in document order gives a stable sequence to pin,
    which is what the state-rendering question is about.

    Parameters
    ----------
    text : str
        The contents of ``graph.dot``.

    Returns
    -------
    list[str]
        One JSON state object per node, in file order, unescaped.

    Examples
    --------
    >>> escaped = chr(92) + '"a' + chr(92) + '"'
    >>> graph_states('State: {' + escaped + ':0}')
    ['{"a":0}']
    """
    return [state.replace('\\"', '"') for state in _GRAPH_STATE.findall(text)]


def normalize(text: str) -> str:
    """Mask the figures that differ between two runs of the same model.

    Parameters
    ----------
    text : str
        The checker's combined output.

    Returns
    -------
    str
        The same text with durations, run names and frontier sizes replaced.

    Examples
    --------
    The launcher's own lines are quoted by their stable part alone, because
    upstream misspells "Written" and no matcher may depend on that spelling.

    >>> normalize("dot -Tsvg out/run_2026-10-24_13-59-07/graph.dot -o graph.svg")
    'dot -Tsvg out/run_<timestamp>/graph.dot -o graph.svg'
    >>> normalize("Nodes: 4, queued: 1, elapsed: 812.5ms")
    'Nodes: 4, queued: <count>, elapsed: <duration>'
    """
    for pattern, replacement in _VOLATILE:
        text = pattern.sub(replacement, text)
    return text


def state_counts(stdout: str) -> tuple[int, int, int]:
    r"""Return the explored, valid and distinct-state counts from a run.

    Parameters
    ----------
    stdout : str
        Masked checker output.

    Returns
    -------
    tuple[int, int, int]
        The node count, the valid node count and the unique state count.

    Raises
    ------
    ValueError
        If the output carries neither a node count nor a valid-node count.

    Examples
    --------
    >>> text = "Nodes: 9, queued: 0" + chr(10) + "Valid Nodes: 7 Unique states: 3"
    >>> state_counts(text)
    (9, 7, 3)
    """
    explored = _NODES.search(stdout)
    valid = _VALID.search(stdout)
    if explored is None or valid is None:
        message = f"the run printed no node count:\n{stdout}"
        raise ValueError(message)
    return int(explored.group(1)), int(valid.group(1)), int(valid.group(2))


def checker_environment() -> dict[str, str]:
    """Return the parent environment with the checker's Go runtime bounded.

    Returns
    -------
    dict[str, str]
        The environment a checker run is started with.
    """
    environment = dict(os.environ)
    environment["GOMAXPROCS"] = "2"
    environment["GOMEMLIMIT"] = "4GiB"
    return environment


def run_model(
    directory: Path,
    model: str,
    *options: str,
    timeout: float = 120.0,
) -> CheckerRun:
    """Run one spike model under the pinned checker.

    Parameters
    ----------
    directory : Path
        A directory owned by this test; the model is copied into it.
    model : str
        File name under ``models/spike``.
    *options : str
        Extra launcher flags, such as ``--no-symmetry-reduction``.
    timeout : float
        Seconds before the process group is killed.

    Returns
    -------
    CheckerRun
        The exit status, masked output and run directory.

    Notes
    -----
    A missing binary or a run that outlives ``timeout`` ends the test through
    ``pytest.fail``, so the failure names the remedy rather than surfacing as
    an assertion or a timeout of the test itself.
    """
    if not FIZZ.is_file():
        pytest.fail(f"missing {FIZZ}; run `make fizzbee` before setting {OPT_IN}=1")
    directory.mkdir(parents=True, exist_ok=True)
    shutil.copy2(MODELS / model, directory / model)
    # The context manager guarantees the child is reaped on every exit path,
    # including the ``pytest.fail`` below, which raises past the caller.
    with subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv, no shell.
        [str(FIZZ), *options, model],
        cwd=directory,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        env=checker_environment(),
    ) as process:
        try:
            raw = process.communicate(timeout=timeout)[0]
        except subprocess.TimeoutExpired:
            _kill_group(process)
            process.communicate()
            pytest.fail(f"{model} did not finish within {timeout:.0f}s")
        return CheckerRun(
            process.returncode, normalize(raw.decode()), _run_directory(directory)
        )


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    """Kill the launcher and the checker it started.

    Parameters
    ----------
    process : subprocess.Popen
        The launcher, started in its own session.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)


def _run_directory(directory: Path) -> Path:
    """Return the newest run directory, or the scratch root when there is none.

    Parameters
    ----------
    directory : Path
        The directory the model ran in.

    Returns
    -------
    Path
        Where the checker wrote its graphs.
    """
    runs = sorted((directory / "out").glob("run_*"))
    return runs[-1] if runs else directory


def error_graph(run: CheckerRun) -> list[dict[str, typ.Any]]:
    """Return the parsed ``error-graph.json`` of a failing run.

    Parameters
    ----------
    run : CheckerRun
        A run that was expected to fail.

    Returns
    -------
    list[dict[str, Any]]
        One link object per step of the counterexample.
    """
    payload = (run.output / "error-graph.json").read_text(encoding="utf-8")
    return json.loads(payload)


def link_names(graph: cabc.Sequence[dict[str, typ.Any]]) -> list[str]:
    """Return each link's action name, in counterexample order.

    Parameters
    ----------
    graph : Sequence[dict[str, Any]]
        The parsed error graph.

    Returns
    -------
    list[str]
        The ``Name`` of every link, including ``"stutter"`` where present.

    Examples
    --------
    >>> link_names([{"Name": "Init"}, {"Name": "stutter"}])
    ['Init', 'stutter']
    """
    return [link["Name"] for link in graph]


def link_states(graph: cabc.Sequence[dict[str, typ.Any]]) -> list[dict[str, typ.Any]]:
    """Return the model state at every node of a counterexample.

    Parameters
    ----------
    graph : Sequence[dict[str, Any]]
        The parsed error graph.

    Returns
    -------
    list[dict[str, Any]]
        Each link's node state, in the same order as :func:`link_names`.
    """
    return [link["Node"]["state"] for link in graph]
