"""Run the pinned checker in a scratch directory and translate what it said.

This is the package's only trust boundary. Every other module is a function of
values; this one starts a process, hands it a file, and reads what it left
behind. The adapter is deliberately small, and every decision that makes it
safe is made here rather than inherited from a caller:

- the specification is written into a fresh directory under ``.model-check/``,
  never beside the model, so a run cannot leave a tracked tree dirty;
- the checker is invoked with an argument list and no shell, so a model's
  contents cannot become part of a command line;
- it is started in its own session and killed by process group, because the
  ``fizz`` launcher starts a Go binary of its own and killing only the launcher
  would leave that binary running;
- the environment caps the Go runtime, because a mistaken model can explore
  without bound and the host should not be the thing that stops it (D5).

D5 records why this is ``subprocess`` rather than ``cuprum``: ``cuprum`` is not
a dependency of this package, and the scripting standards that require it
govern ``scripts/``. Ruff's ``S404`` and ``S603`` are ignored for this module
only, because a fixed argv is passed rather than a shell string.

Classifying what the checker printed is not here. That is
:func:`modelcheck.adapters.fizzbee_output.parse_output`, which is pure and is
tested against recordings of every shape the checker can print. This module
only guarantees that the text it hands over is the checker's whole output, and
that the run left no process behind.
"""

import contextlib
import os
import shutil
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argv, no shell.
import tempfile
import typing as typ
from pathlib import Path

from modelcheck.adapters.fizzbee_output import parse_output
from modelcheck.domain import Errored

if typ.TYPE_CHECKING:
    from modelcheck.domain import CheckResult, MaterializedSpec

__all__ = ["FizzbeeChecker"]

SCRATCH_ROOT = Path(".model-check")
"""Where every run's scratch directory is made.

The path is relative to the working directory, which for a repository run is
the checkout root, and the directory is git-ignored. It is scratch: deleting it
is always safe, and this module deletes each run's own directory as soon as the
run's output has been read.
"""

_TIMEOUT_SECONDS = 600.0
"""How long one routine-bounds run may take before its group is killed.

A routine configuration is required to finish in ten minutes. A run that hits
this has already failed the requirement, so the adapter stops it rather than
letting it hold the machine; the result is an ``Errored``, which a report shows
as a run that proved nothing. Nightly runs pass a larger bound.
"""

_KILL_GRACE_SECONDS = 5.0
"""How long a killed group has to exit after ``SIGTERM`` before ``SIGKILL``."""

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
            be started at all, is an ``Errored`` rather than an exception: the
            matrix reports it as a run that proved nothing, which is what it
            is, and the exit status is still the un-met one.

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
        try:
            return self._checked(directory, spec)
        finally:
            shutil.rmtree(directory, ignore_errors=True)

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
        """
        (directory / _SPEC_NAME).write_text(spec.text, encoding="utf-8")
        output = directory / "out"
        try:
            completed = self._run(directory, output)
        except OSError as error:
            return Errored(f"the checker could not be started: {error}")
        stdout = completed.stdout.decode(errors="replace")
        if completed.timed_out:
            return Errored(_timed_out(self.timeout_seconds, stdout))
        return parse_output(stdout, completed.status, _graph(output))

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
                return _Completed(process.returncode, _stopped(process), timed_out=True)
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


def _timed_out(timeout_seconds: float, stdout: str) -> str:
    r"""Build the detail for a run the adapter stopped.

    Parameters
    ----------
    timeout_seconds : float
        The limit the run exceeded.
    stdout : str
        Everything the checker printed before it was killed.

    Returns
    -------
    str
        The reason, followed by the checker's own output when there was any.
        Keeping it is the difference between a report naming the state count
        the model had reached and a report saying only that time ran out.

    Examples
    --------
    >>> _timed_out(600.0, "Nodes: 41\n").splitlines()[0]
    'the checker did not finish within 600s'
    >>> _timed_out(600.0, "").splitlines()
    ['the checker did not finish within 600s']
    """
    reason = f"the checker did not finish within {timeout_seconds:.0f}s"
    printed = stdout.strip()
    return f"{reason}\n{printed}" if printed else reason


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


def _stopped(process: subprocess.Popen[bytes]) -> bytes:
    """Kill a run's group and collect what it printed before it went.

    Parameters
    ----------
    process : subprocess.Popen
        The launcher, whose output has not yet been read.

    Returns
    -------
    bytes
        The output the checker managed to print. It is the only evidence of
        how far the run had got, so it is kept rather than discarded: a report
        that said nothing beyond "it timed out" would hide the state count that
        says whether the model was still exploring, or stuck.

    Notes
    -----
    The second ``communicate`` blocks until every writer has closed its end of
    the pipe. Sending ``SIGKILL`` to the group is what guarantees that happens
    even when a grandchild inherited the pipe, so this call cannot hang on a
    daemon the launcher left behind.
    """
    _kill_group(process)
    return process.communicate()[0]


def _kill_group(process: subprocess.Popen[bytes]) -> None:
    """Ask a launcher's group to stop, then insist.

    Parameters
    ----------
    process : subprocess.Popen
        The launcher, started in its own session, so its process group is
        exactly the launcher and everything it started.

    Notes
    -----
    ``SIGTERM`` first gives the checker a chance to print why it was stopped,
    which is the difference between a report naming its cause and a report
    saying the run vanished. ``SIGKILL`` follows after the grace period, which
    is what handles a checker wedged inside the Go runtime.

    The escalation is unconditional and runs even when the launcher has already
    exited. ``process.wait()`` observes the launcher alone, so a child that
    ignores ``SIGTERM`` while the launcher obeys it would otherwise never
    receive ``SIGKILL``, and the following ``communicate`` would block on the
    pipe that child still holds. A group that has already gone is not an
    error: ``ProcessLookupError`` means the work is done, and the subsequent
    ``communicate`` collects whatever it printed.
    """
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)


def _graph(output: Path) -> str | None:
    """Return the error graph one run wrote, if it wrote one.

    Parameters
    ----------
    output : pathlib.Path
        The directory the checker was told to write into.

    Returns
    -------
    str | None
        The graph's text, or ``None`` when the run passed or wrote none. A
        graph that cannot be read is reported as absent rather than raised: the
        violation the output announced is real either way, and the path is
        evidence about it rather than the finding itself.

    Examples
    --------
    >>> _graph(Path("/nonexistent")) is None
    True
    """
    try:
        return (output / _GRAPH_NAME).read_text(encoding="utf-8")
    except OSError:
        return None
