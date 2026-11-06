"""Run the pinned checker in a scratch directory and translate what it said.

This is the package's only trust boundary. Every other module is a function of
values; this one starts a process, hands it a file, and reads what it left
behind. The adapter is deliberately small, and every decision that makes it
safe is made here rather than inherited from a caller:

- the specification is written into a fresh directory under ``.model-check/``,
  never beside the model, so a run cannot leave a tracked tree dirty;
- the checker is invoked with an argument list and no shell, so a model's
  contents cannot become part of a command line;
- it is started in its own session and killed by process group, because
  ``fizz`` launches a Go binary that would otherwise survive it;
- the environment caps the Go runtime, because a mistaken model can explore
  without bound and the host should not be the thing that stops it (D5).

D5 records why this is ``subprocess`` rather than ``cuprum``: ``cuprum`` is not
a dependency of this package, and the scripting standards that require it
govern ``scripts/``. Ruff's ``S404`` and ``S603`` are ignored here only,
because a fixed argv is passed rather than a shell string.

Classifying the output belongs to
:func:`modelcheck.adapters.fizzbee_output.parse_output`, tested against
recordings of every shape the checker can print. This module only guarantees
that the text it hands over is the whole output, and that no process is left.
"""

import dataclasses as dc
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv, no shell.
import tempfile
import time
import typing as typ
from pathlib import Path

from modelcheck.adapters.fizzbee_evidence import (
    redacted,
    timeout_detail,
    unreadable_graph,
    with_evidence,
    without_scratch,
)
from modelcheck.adapters.fizzbee_output import parse_output
from modelcheck.adapters.fizzbee_process_group import kill_group, stopped
from modelcheck.domain import Errored, Stopped

if typ.TYPE_CHECKING:
    from modelcheck.domain import CheckResult, MaterializedSpec

__all__ = ["FizzbeeChecker"]

SCRATCH_ROOT = Path(".model-check")
"""Where every run's scratch directory is made.

Relative to the working directory, and git-ignored. Deleting it is always safe,
and each run's own directory is removed once its output has been read.
"""

_TIMEOUT_SECONDS = 600.0
"""How long one routine-bounds run may take before its group is killed.

A routine configuration must finish in ten minutes, so a run that hits this has
already failed and is stopped rather than left holding the machine; the result
is an ``Errored``, which a report shows as a run that proved nothing. Nightly
runs pass a larger bound.
"""

_MEMORY_LIMIT = "4GiB"
"""The Go runtime's memory ceiling for one run, per D5 and the plan's budget."""

_PROCESSORS = "2"
"""How many OS threads the Go runtime may use, so a run shares the host."""

_SPEC_NAME = "model.fizz"
"""The name the specification is written under in the scratch directory."""

_GRAPH_NAME = "error-graph.json"
"""The file the checker writes a counterexample's path into."""


class FizzbeeChecker:
    """Run one materialized specification with the pinned checker.

    Parameters
    ----------
    fizz : pathlib.Path
        The ``fizz`` launcher to run.
    timeout_seconds : float
        How long one run may take before its process group is killed. A
        nightly run passes a larger value; the caller that chose the bound set
        is the one that knows which applies.

    Examples
    --------
    The adapter satisfies :class:`modelcheck.ports.ModelChecker` structurally,
    so a caller can hold it under that protocol and never learn its type. The
    protocols are deliberately not ``runtime_checkable``: membership is by
    having the method, and a type checker is what proves it, so this example
    asserts the method rather than an ``isinstance`` the protocol would refuse.

    >>> checker = FizzbeeChecker(Path("/nonexistent/fizz"))
    >>> callable(checker.check)
    True
    """

    def __init__(self, fizz: Path, timeout_seconds: float = _TIMEOUT_SECONDS) -> None:
        """Record the launcher and the run's time limit.

        Parameters
        ----------
        fizz : pathlib.Path
            The launcher to run. A relative path is resolved against the
            caller's working directory now, not when a run starts, because the
            run's own working directory is the scratch directory — a relative
            launcher path would otherwise be looked for there and never found.
        timeout_seconds : float
            How long one run may take.
        """
        self._fizz = fizz.resolve()
        self.timeout_seconds = timeout_seconds

    def check(self, spec: MaterializedSpec) -> CheckResult:
        r"""Run one specification and classify the result.

        Parameters
        ----------
        spec : MaterializedSpec
            The text to check and the configuration it was built for.

        Returns
        -------
        CheckResult
            The classified result. A launcher that is missing, or that cannot
            start at all is an ``Errored`` rather than an exception, so the
            matrix reports a run that proved nothing; the exit status is un-met.

        Examples
        --------
        A missing launcher is reported as a run that proved nothing, not as a
        traceback escaping the matrix.

        >>> from modelcheck.domain import MaterializedSpec
        >>> FizzbeeChecker(Path("/nonexistent/fizz")).check(
        ...     MaterializedSpec("head", "action Init:\n  n = 0\n", ())
        ... ).detail.startswith("the checker could not be started:")
        True
        """
        directory = _scratch()
        started = time.monotonic()
        try:
            result = self._checked(directory, spec)
        finally:
            shutil.rmtree(directory, ignore_errors=True)
        return _observed(result, time.monotonic() - started)

    def _checked(self, directory: Path, spec: MaterializedSpec) -> CheckResult:
        """Write the specification, run it, and read what it left.

        Parameters
        ----------
        directory : pathlib.Path
            The scratch directory this run owns.
        spec : MaterializedSpec
            The specification to run.

        Returns
        -------
        CheckResult
            What the checker's output and error graph classify as.

        Notes
        -----
        Both the transcript and the error graph are redacted of this run's
        scratch directory. The graph is checker-written text that becomes
        committed evidence — a trace file on a successful parse, a bounded
        capture on a failed one — so a runner path left in it would travel into
        the artefact the same way one printed to the transcript would.
        """
        (directory / _SPEC_NAME).write_text(spec.text, encoding="utf-8")
        output = directory / "out"
        try:
            completed = self._run(directory, output)
        except OSError as error:
            return Errored(f"the checker could not be started: {error}")
        stdout = redacted(completed.stdout.decode(errors="replace"), directory)
        if completed.timed_out:
            return Errored(timeout_detail(self.timeout_seconds, stdout))
        try:
            graph = _graph(output)
        except (OSError, UnicodeDecodeError) as error:
            # The scratch directory is removed as this call returns, so the
            # checker's transcript is the only evidence left of a run whose
            # graph could not be read.
            return Errored(
                with_evidence(
                    unreadable_graph(error),
                    (("the checker printed", stdout),),
                )
            )
        graph_text = without_scratch(graph, directory)
        return parse_output(stdout, completed.status, graph_text)

    def _run(self, directory: Path, output: Path) -> _Completed:
        """Run the launcher and wait for it, killing its group on timeout.

        Parameters
        ----------
        directory : pathlib.Path
            The scratch directory the specification was written into.
        output : pathlib.Path
            Where the checker is told to write its graphs.

        Returns
        -------
        _Completed
            The exit status, the combined output and whether the run was cut
            short by the timeout.

        Notes
        -----
        The context manager reaps the launcher on every path out of this
        function, including the timeout path, where the group has already been
        signalled. The pipes are closed as part of the same protocol; a leaked
        pipe would surface as a ``ResourceWarning``, which this repository
        promotes to an error.

        On timeout the output the checker managed to print is kept rather than
        discarded. It is the only evidence of where the run had got to, and a
        report that said nothing beyond "it timed out" would hide the state
        count that says whether the model was still exploring.
        """
        with subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true] - an argument vector, never a shell.
            [str(self._fizz), "--output-dir", str(output), _SPEC_NAME],
            cwd=directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=_checker_environment(),
        ) as process:
            try:
                raw = process.communicate(timeout=self.timeout_seconds)[0]
            except subprocess.TimeoutExpired:
                # The status of a killed run is the signal that killed it, and
                # is deliberately not used: ``_checked`` reports a timeout as
                # its own kind of error, before the parser ever sees a status
                # that would read as an ordinary failure.
                return _Completed(process.returncode, stopped(process), timed_out=True)
            except BaseException:
                # The launcher runs in its own session, so a terminal interrupt
                # never reaches it or the checker it started. Without this kill
                # the group would outlive the exception, still capped only by
                # ``GOMAXPROCS`` and ``GOMEMLIMIT``, while the scratch directory
                # was removed underneath it. The exception is re-raised: the
                # caller asked for the run to stop, not to be told it passed.
                kill_group(process)
                raise
            return _Completed(process.returncode, raw, timed_out=False)


class _Completed(typ.NamedTuple):
    """What one launcher invocation left.

    Attributes
    ----------
    status : int
        The launcher's exit status. A killed process reports the negative
        signal number, as :mod:`subprocess` does.
    stdout : bytes
        The combined output, still bytes because the checker may not have
        printed valid UTF-8 and decoding is the caller's decision.
    timed_out : bool
        Whether the adapter killed the run rather than the run ending.
    """

    status: int
    stdout: bytes
    timed_out: bool


def _observed(result: CheckResult, seconds: float) -> CheckResult:
    """Attach the harness's own wall time to a run that produced no verdict.

    A pass or a counterexample carries the exploration duration the checker
    reported. A stopped or errored run reports none, so without this the one
    timing such a run has would be lost. The field is named for the harness
    rather than the checker, because it measures how long this adapter waited,
    not how long the checker explored.

    Parameters
    ----------
    result : CheckResult
        The classified result.
    seconds : float
        Wall-clock seconds the run took, measured around the whole invocation.

    Returns
    -------
    CheckResult
        The result with ``harness_seconds`` set when it is a stopped or errored
        run, and unchanged otherwise.
    """
    if isinstance(result, (Stopped, Errored)):
        return dc.replace(result, harness_seconds=seconds)
    return result


def _scratch() -> Path:
    """Make the directory one run owns.

    Returns
    -------
    pathlib.Path
        A new, empty directory under ``.model-check/``, whose name is unique to
        this run. The directory is not removed if the caller crashes before its
        ``finally`` runs; deleting ``.model-check/`` is always safe.
    """
    SCRATCH_ROOT.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="run-", dir=SCRATCH_ROOT))


def _checker_environment() -> dict[str, str]:
    """Return the environment one checker run gets.

    Returns
    -------
    dict[str, str]
        The caller's environment with the Go runtime's limits set, so a run
        explores within the host's budget rather than the host's capacity. The
        rest is inherited because the launcher needs ``HOME``, ``PATH`` and
        whatever else the caller's shell set for it.
    """
    return {**os.environ, "GOMAXPROCS": _PROCESSORS, "GOMEMLIMIT": _MEMORY_LIMIT}


def _graph(output: Path) -> str | None:
    """Return the error graph one run wrote, if it wrote one.

    Parameters
    ----------
    output : pathlib.Path
        The directory the checker was told to write into.

    Returns
    -------
    str | None
        The graph's text, or ``None`` when the run passed or wrote none.

    Raises
    ------
    OSError
        If a graph exists but cannot be read. Only its absence is ordinary:
        a permission or I/O failure means the evidence the counterexample needs
        is missing for a reason the run report must name, rather than a pass or
        a finding it cannot support.
    UnicodeDecodeError
        If a graph exists but is not UTF-8, so it cannot be read as text. The
        caller reports it the same way, because a graph nobody can decode
        proves nothing a counterexample could rest on.

    Examples
    --------
    >>> _graph(Path("/nonexistent")) is None
    True
    """  # ruff: ignore[docstring-extraneous-exception] - read_text raises it.
    try:
        return (output / _GRAPH_NAME).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
