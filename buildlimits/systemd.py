"""The systemd user manager port: transient scopes and unit properties.

The supervisor talks to the user manager through ``busctl`` and ``systemctl``
rather than a D-Bus library, so the package keeps its standard-library-only
runtime. The command runner is injectable for tests.
"""

import collections.abc as cabc
import dataclasses as dc
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argument vectors, never a shell.

from buildlimits.bus_properties import BusQueryError, service_properties

type Runner = cabc.Callable[[cabc.Sequence[str]], subprocess.CompletedProcess[str]]

BUSCTL = "busctl"
SYSTEMCTL = "systemctl"


class SystemdError(Exception):
    """The user manager rejected a request or could not be reached."""


def run(argv: cabc.Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a fixed command and capture its output.

    Parameters
    ----------
    argv : Sequence[str]
        The command's argument vector.

    Returns
    -------
    subprocess.CompletedProcess[str]
        The finished process.

    Raises
    ------
    SystemdError
        If the command cannot be started or does not finish in time, so that
        callers handle every failure to reach the manager in one place.
    """
    try:
        return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - argv is built from constants and unit names.
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=_transport_bound(argv),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        msg = f"cannot run {' '.join(argv)}: {error}"
        raise SystemdError(msg) from error


def _transport_bound(argv: cabc.Sequence[str]) -> float:
    """Choose the bound for one command from what the command asks the manager.

    ``--no-block`` and ``--timeout=3s`` mark requests the manager answers as
    soon as it has accepted them, so a manager that stalls mid-call must not
    hold the supervisor open for the full synchronous bound. Creating a scope
    is a synchronous bus call the manager must complete, so it keeps that
    bound.

    Returns
    -------
    float
        Seconds the command may take before it is a transport fault.
    """
    async_request = "--no-block" in argv or "--timeout=3s" in argv or "show" in argv
    return 3 if async_request else 30


def _check_query(result: subprocess.CompletedProcess[str]) -> None:
    """Reject a failed private query with bounded escaped fault context.

    Raises
    ------
    SystemdError
        If the manager command exits unsuccessfully.
    """
    if result.returncode != 0:
        fault = ascii(result.stderr.strip()[:240])[1:-1]
        msg = f"private manager query failed: {fault}"
        raise SystemdError(msg)


def unit_verb_argv(verb: str, unit: str) -> list[str]:
    """Build a non-blocking private control command for one unit.

    The job is queued rather than awaited: a supervisor that blocked on
    ``systemctl start`` would stall its own event loop, and an accepted job is
    never evidence that the unit reached the requested state. Callers verify
    the outcome from the unit's own properties.

    Parameters
    ----------
    verb : str
        The control verb, ``stop`` or ``start``.
    unit : str
        The unit to control.

    Returns
    -------
    list[str]
        The argument vector.

    Examples
    --------
    >>> " ".join(unit_verb_argv("stop", "sccache.service"))
    'systemctl --user --no-block stop sccache.service'
    """
    return [SYSTEMCTL, "--user", "--no-block", verb, unit]


def start_scope_argv(unit: str, pid: int, slice_unit: str) -> list[str]:
    """Build the ``busctl`` call that moves ``pid`` into a new transient scope.

    Parameters
    ----------
    unit : str
        The scope unit name, ending in ``.scope``.
    pid : int
        The process to place in the scope.
    slice_unit : str
        The slice to create the scope under.

    Returns
    -------
    list[str]
        The argument vector.

    Examples
    --------
    >>> argv = start_scope_argv("x.scope", 42, "s.slice")
    >>> " ".join(argv[argv.index("PIDs") :])
    'PIDs au 1 42 Slice s s.slice CollectMode s inactive-or-failed 0'
    """
    return [
        BUSCTL,
        "--user",
        "call",
        "org.freedesktop.systemd1",
        "/org/freedesktop/systemd1",
        "org.freedesktop.systemd1.Manager",
        "StartTransientUnit",
        "ssa(sv)a(sa(sv))",
        unit,
        "fail",
        "3",
        "PIDs",
        "au",
        "1",
        str(pid),
        "Slice",
        "s",
        slice_unit,
        # A group killed by a signal would otherwise leave a failed scope
        # behind; collect it once it stops, whatever the result.
        "CollectMode",
        "s",
        "inactive-or-failed",
        "0",
    ]


@dc.dataclass(slots=True)
class UserManager:
    """Operations on the calling user's systemd manager.

    Parameters
    ----------
    runner : Runner
        Runs a command; replaced in tests.
    """

    runner: Runner = run

    def start_scope(self, unit: str, pid: int, slice_unit: str) -> None:
        """Place a process in a new transient scope under a slice.

        Parameters
        ----------
        unit : str
            The scope unit name.
        pid : int
            The process to place.
        slice_unit : str
            The slice to create the scope under.

        Raises
        ------
        SystemdError
            If the manager refuses the scope.
        """
        result = self.runner(start_scope_argv(unit, pid, slice_unit))
        if result.returncode != 0:
            msg = f"cannot create {unit}: {result.stderr.strip()}"
            raise SystemdError(msg)

    def show(self, unit: str, *properties: str) -> dict[str, str]:
        """Read unit properties.

        Parameters
        ----------
        unit : str
            The unit to query.
        *properties : str
            Property names to read.

        Returns
        -------
        dict[str, str]
            The requested properties.

        Raises
        ------
        SystemdError
            If the manager cannot be queried.
        """
        argv = [SYSTEMCTL, "--user", "show", unit]
        argv.extend(f"--property={name}" for name in properties)
        try:
            result = self.runner(argv)
            _check_query(result)
        except SystemdError as private_error:
            try:
                return service_properties(self.runner, unit, properties)
            except (BusQueryError, SystemdError) as public_error:
                msg = (
                    f"cannot query {unit}: "
                    f"{ascii(str(private_error)[:300])[1:-1]}; "
                    f"{ascii(str(public_error)[:300])[1:-1]}"
                )
                raise SystemdError(msg) from public_error
        values: dict[str, str] = {}
        for line in result.stdout.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                values[key] = value
        return values

    def control_unit(self, verb: str, unit: str) -> None:
        """Queue a start or stop for a unit without waiting for it to finish.

        One method rather than a verb per name, because the two differ only in
        the word they pass to ``systemctl``: splitting them duplicated the same
        queued-job contract and forced callers to select a method by name.

        Parameters
        ----------
        verb : str
            The control verb, ``stop`` or ``start``.
        unit : str
            The unit to control.

        Raises
        ------
        SystemdError
            If the manager refuses the job or cannot be reached. An accepted
            job is not proof that the unit reached the requested state, so
            callers verify the outcome from the unit's own properties.
        """
        result = self.runner(unit_verb_argv(verb, unit))
        if result.returncode != 0:
            fault = ascii(result.stderr.strip()[:240])[1:-1]
            msg = f"cannot {verb} {unit}: {fault}"
            raise SystemdError(msg)
