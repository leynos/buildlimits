"""A kache process a scenario parks in a scope admission never held.

Section 4 of the issue makes the daemon guard a *report*, not a killer, so the
only way to prove it fires is to present the supervisor with exactly what it is
meant to notice: a process named ``kache`` sitting in one of the scopes the
guard reads while no admitted group accounts for it. A scenario cannot produce
that through a build, because a group's emptied scope is removed with the group
that owned it, so the fixture parks the process itself.

The process is a copy of ``sleep`` placed in a freshly created directory. It is
never copied onto a path that might already be a symlink, and the copy is what
gives it the name the guard matches: the executable link, not a command line,
is the evidence.

The scope is created through the same ``busctl`` call the supervisor uses, in
the slice the guard reads, and the scenario removes it when it closes.
"""

import contextlib
import dataclasses as dc
import os
import signal
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed fixture argv, never a shell.
from pathlib import Path
from shutil import copy

from harness_units import manager_env, wait_for

#: ``sleep`` is copied only to give the process the guard's name; this keeps it
#: alive for far longer than any scenario runs.
SLEEP_SECONDS = "300"

#: The names the supervisor's executable test recognizes. Restated here because
#: the scenario must be able to say the fixture presented one, not merely that
#: the guard stayed quiet.
STRAY_NAMES = frozenset({"kache", "kache-engine"})


@dc.dataclass(frozen=True, slots=True)
class Stray:
    """A parked kache process and the scope that owns it.

    The process handle is kept rather than its identifier alone so that the
    fixture that started it is also the one that reaps it. An abandoned
    ``Popen`` warns from its deallocator while the child is still running,
    which a test session treats as a failure.

    Attributes
    ----------
    process : subprocess.Popen[bytes]
        The parked copy of ``sleep``, still running.
    unit : str
        The scope that holds it.
    """

    process: subprocess.Popen[bytes]
    unit: str

    @property
    def pid(self) -> int:
        """The parked process's identifier.

        Returns
        -------
        int
            The process identifier.
        """
        return self.process.pid


def stray_kache_scope(unit: str, slice_unit: str, runtime: Path) -> Stray:
    """Park a process named kache in a new scope.

    Parameters
    ----------
    unit : str
        The scope unit to create. It must carry the supervisor's prefix and
        suffix, or the guard will not read the directory at all.
    slice_unit : str
        The slice the scope is created under, which must be the one the
        supervisor's guard reads.
    runtime : Path
        A private directory the copy is placed in.

    Returns
    -------
    Stray
        The parked process and its scope, for :func:`remove` to undo.

    Raises
    ------
    RuntimeError
        If the scope cannot be created or the process does not join it. A
        scenario that quietly failed here would report the guard as silent
        when it was never presented with anything.
    """
    binary = _stray_binary(runtime)
    process = subprocess.Popen(  # pylint: disable=consider-using-with  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv, no shell.
        [str(binary), SLEEP_SECONDS],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _start_scope(unit, process.pid, slice_unit)
        joined = wait_for(lambda: _scope_of(process.pid) == unit)
    except BaseException:
        process.kill()
        process.wait(timeout=30)
        raise
    if not joined:
        process.kill()
        process.wait(timeout=30)
        msg = f"the stray kache process never joined {unit}"
        raise RuntimeError(msg)
    return Stray(process, unit)


def _stray_binary(runtime: Path) -> Path:
    """Copy ``sleep`` to a fresh path named ``kache``.

    Returns
    -------
    Path
        The copy, which is the executable the guard's test reads.
    """
    directory = runtime / "stray"
    directory.mkdir()
    binary = directory / "kache"
    copy("/bin/sleep", binary)
    return binary


def _start_scope(unit: str, pid: int, slice_unit: str) -> None:
    """Create a transient scope around one process, as the supervisor does.

    Raises
    ------
    RuntimeError
        If the manager refuses the scope.
    """
    result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv.
        [
            "/usr/bin/busctl",
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
            # Matches a supervisor-created scope: one that stops is collected
            # rather than left behind as a failed unit.
            "CollectMode",
            "s",
            "inactive-or-failed",
            "0",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=manager_env(),
        timeout=30,
    )
    if result.returncode != 0:
        msg = f"cannot create {unit}: {result.stderr.strip()}"
        raise RuntimeError(msg)


def _scope_of(pid: int) -> str | None:
    """Return the scope unit a process is running in.

    Returns
    -------
    str or None
        The scope's name, or ``None`` when the process is not in one yet.
    """
    try:
        text = Path(f"/proc/{pid}/cgroup").read_text(encoding="ascii")
    except OSError:
        return None
    for line in text.splitlines():
        hierarchy, _, rest = line.partition(":")
        _controllers, _, path = rest.partition(":")
        if hierarchy != "0":
            continue
        name = Path(path.strip()).name
        if name.endswith(".scope"):
            return name
    return None


def is_observable(pid: int) -> bool:
    """Report whether the guard's executable test would match this process.

    It answers the question the supervisor's own ``/proc`` read answers, so a
    scenario can tell "the guard ran and found nothing" from "the fixture never
    presented a kache-named process at all".

    Parameters
    ----------
    pid : int
        The parked process.

    Returns
    -------
    bool
        Whether the executable link is named kache.
    """
    try:
        target = Path(f"/proc/{pid}/exe").readlink()
    except OSError:
        return False
    return target.name.removesuffix(" (deleted)") in STRAY_NAMES


def remove(stray: Stray) -> None:
    """Stop the parked scope and its process, and reap what was started.

    Parameters
    ----------
    stray : Stray
        The process and scope a scenario parked.
    """
    # Kill and reap before stopping the scope. The parked process is this
    # session's own child, so it stays a zombie inside the scope's cgroup until
    # it is reaped here, and ``systemctl stop`` waits for that cgroup to empty.
    # Waiting first would let the stop outlive its own timeout.
    with contextlib.suppress(ProcessLookupError):
        os.kill(stray.pid, signal.SIGKILL)
    stray.process.wait(timeout=30)
    # The scope is collected once its last process ends; stopping it also
    # covers a process that never joined.
    for arguments in (("stop", stray.unit), ("reset-failed", stray.unit)):
        subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argv.
            ["/usr/bin/systemctl", "--user", *arguments],
            capture_output=True,
            check=False,
            env=manager_env(),
            timeout=30,
        )
