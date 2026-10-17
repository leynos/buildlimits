"""The scripted managed-cache double shared by the recovery test modules.

The recovery transaction drives a systemd user manager it does not own, so its
tests script that manager rather than the machine. :class:`CacheState` records
every control call and answers unit queries from mutable fields;
:func:`runner_for` turns that state into the ``systemctl`` argv the private
transport builds, and :func:`manager_for` wraps it as the user-manager port.
The case records below give the parametrized tests one scripted criterion each.
Nothing here touches a real unit, and no assertion may turn elapsed time, idle
CPU or a missing compiler process into accounting proof. The restart barrier's
own fates and I/O failures live in :mod:`cache_recovery_barrier_helpers`.

Examples
--------
>>> state = CacheState(active=False)
>>> state.verbs
[]
>>> state.last_attempted("stop")
False
>>> state.refuse = "start"
>>> state.refuse
'start'

"""

import dataclasses as dc
import subprocess  # ruff: ignore[suspicious-subprocess-import] - tests script the manager's argv, never a shell.
import typing as typ
from pathlib import Path, PurePosixPath

from buildlimits import cgroups
from buildlimits.admission import Counts
from buildlimits.systemd import UserManager

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest

    from buildlimits.cache_recovery import CacheRecovery

OWN_CGROUP = PurePosixPath(
    "/user.slice/user-1000.slice/user@1000.service/app.slice/s.service"
)
CACHE_UNIT = "cache.service"
LIVE_PID = 4242

type ManagerRunner = cabc.Callable[..., subprocess.CompletedProcess[str]]


@dc.dataclass(frozen=True, slots=True)
class DrainReading:
    """One drain probe's answer: the fault kind and the roots it counted.

    The two fields are one reading rather than two independent variables. The
    live watch records a root count only for a live-work fault — it takes the
    count from ``LiveCacheWorkError.roots`` and clears it for every other kind
    — and only the zero-root proof reads it back. Pairing them here keeps the
    impossible combination, a manager fault carrying a visible compiler count,
    out of the fixtures.

    Attributes
    ----------
    kind : str or None
        The fault the server watch reports, or None for a clean probe. It
        defaults to the live-work fault the trigger acts on, so a fixture
        wanting a clean probe or a transport fault asks for it explicitly.
    roots : int or None
        Visible compiler roots the diagnostic established, or None for a count
        it could not read. Ignored unless ``kind`` is ``"live_work"``.
    """

    kind: str | None = "live_work"
    roots: int | None = 0

    @property
    def proves_zero_roots(self) -> bool:
        """Whether this reading is the zero-root proof the trigger requires.

        Returns
        -------
        bool
            True only for a live-work fault whose count was established as
            zero, matching the live watch's own zero-root proof.
        """
        return self.kind == "live_work" and self.roots == 0


# The reading a fixture takes when it does not script one: the live-work fault
# with a proven zero root count, which is the one the automatic trigger acts
# on. Immutable, so fixtures may share it.
LIVE_WORK_ZERO_ROOTS = DrainReading()


@dc.dataclass(frozen=True, slots=True)
class TriggerCase:
    """One scripted policy, fault and gauge combination for the trigger.

    Attributes
    ----------
    mode : str
        The configured recovery policy.
    fault : str or None
        The drain fault the server watch reports, or None for a clean probe.
    counts : Counts
        The admission gauge snapshot the trigger reads.
    starts : bool
        Whether this combination must open a recovery transaction.
    roots : int or None
        Visible compiler roots the zero-root proof reports; None for a root
        count the diagnostic could not establish.
    """

    mode: str
    fault: str | None
    counts: Counts
    starts: bool
    roots: int | None = 0

    @property
    def reading(self) -> DrainReading:
        """Pair the case's fault kind with the root count it reports.

        Returns
        -------
        DrainReading
            The single reading the trigger takes both values from.
        """
        return DrainReading(self.fault, self.roots)


@dc.dataclass(slots=True)
class CacheState:
    """Scripted managed-cache state, shared with the manager runner."""

    active: bool = True
    manager_down: bool = False
    group: Path | None = None
    marker: Path = Path("unset")
    refuse: str | None = None
    marker_at_stop: bool | None = None
    marker_at_start: bool | None = None
    calls: list[list[str]] = dc.field(default_factory=list)

    @property
    def verbs(self) -> list[str]:
        """List the control verbs the manager was asked to perform.

        Returns
        -------
        list[str]
            ``stop`` and ``start`` in call order, ignoring state queries.
        """
        return [
            verb
            for call in self.calls
            for verb in ("stop", "start")
            if _requested(call, verb)
        ]

    def last_attempted(self, verb: str) -> bool:
        """Report whether the manager's most recent call asked for ``verb``.

        Returns
        -------
        bool
            Whether the newest recorded call names the requested verb.
        """
        return bool(self.calls) and _requested(self.calls[-1], verb)

    def record(self, group: Path) -> None:
        """Point the scripted service at its control group and settle its state."""
        self.group = group
        group.mkdir(parents=True, exist_ok=True)
        (group / "cgroup.procs").write_text("")


def refusing_runner(argv: cabc.Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Answer every user-manager query with a failure.

    Returns
    -------
    subprocess.CompletedProcess[str]
        A failed result carrying a transport fault on standard error.
    """
    return subprocess.CompletedProcess(argv, 1, "", "Connection timed out")


def _requested(argv: cabc.Sequence[str], verb: str) -> bool:
    """Report whether an argv asks the manager for one control verb.

    Both transports are recognised, so the assertion holds whichever one the
    managed cache fell back to: the private path passes ``stop``/``start``
    directly, and the public bus names the D-Bus method instead.

    Returns
    -------
    bool
        Whether the argv names the requested verb on either transport.
    """
    return verb in argv or f"{verb.title()}Unit" in argv


def _settle(state: CacheState) -> None:
    """Keep the scripted control group consistent with the service state."""
    if state.group is not None:
        members = f"{LIVE_PID}\n" if state.active else ""
        (state.group / "cgroup.procs").write_text(members)


def runner_for(state: CacheState) -> ManagerRunner:
    """Build the manager runner that answers every command from ``state``.

    Parameters
    ----------
    state : CacheState
        The scripted service whose calls are recorded and whose fields answer.

    Returns
    -------
    ManagerRunner
        A runner recording each argv and answering from the scripted state.
    """

    def run(argv: cabc.Sequence[str]) -> subprocess.CompletedProcess[str]:
        """Answer one manager command from the scripted state.

        Returns
        -------
        subprocess.CompletedProcess[str]
            The scripted query answer, or the accepted, refused or unreached job.
        """
        state.calls.append(list(argv))
        if state.manager_down or argv[0] != "systemctl":
            return subprocess.CompletedProcess(argv, 1, "", "Connection refused")
        if "show" in argv:
            _settle(state)
            text = (
                f"MainPID={LIVE_PID if state.active else 0}\n"
                f"ControlGroup=/{CACHE_UNIT}\n"
                f"ActiveState={'active' if state.active else 'inactive'}\n"
            )
            return subprocess.CompletedProcess(argv, 0, text, "")
        verb = "stop" if "stop" in argv else "start"
        if verb == "stop":
            state.marker_at_stop = state.marker.exists()
        else:
            state.marker_at_start = state.marker.exists()
        if verb == state.refuse:
            return subprocess.CompletedProcess(argv, 1, "", "Job failed")
        # A stop job is only accepted: the service stays live until the test
        # ends it, exactly as a queued --no-block job does.
        state.active = state.active or verb == "start"
        _settle(state)
        return subprocess.CompletedProcess(argv, 0, "", "")

    return run


def manager_for(state: CacheState) -> UserManager:
    """Build the scripted user-manager port over ``state``.

    Parameters
    ----------
    state : CacheState
        The scripted service the port answers from.

    Returns
    -------
    UserManager
        A manager port whose every command reaches the scripted runner.
    """
    return UserManager(runner_for(state))


def gauges(
    *,
    queued: int = 0,
    running: int = 0,
    draining: int = 0,
    server_drain: int = 0,
) -> Counts:
    """Build a gauge snapshot without constructing real compiler groups.

    The three group-state counts are the axes; ``held`` is derived from them
    because every group is in exactly one state, so the production reading
    (``Admission.counts``: ``len(self.groups)`` against the same partition)
    always satisfies ``held == running + draining + server_drain``. Deriving
    it keeps an impossible snapshot — a token held by no group, or a group
    holding two — out of the fixture; construct ``Counts`` directly for a
    hostile reading.

    Parameters
    ----------
    queued : int
        Requests still waiting for a token.
    running : int
        Groups whose leaders have not ended.
    draining : int
        Groups whose leader has ended but whose processes may still run.
    server_drain : int
        Empty groups retaining a token for server work.

    Returns
    -------
    Counts
        The identifier-free admission snapshot.
    """
    held = running + draining + server_drain
    return Counts(queued, held, running, draining, server_drain, capacity=2)


# The backoff between retries is 30 s times the attempt count, so a step well
# past the limit lets a loop drive every attempt without sleeping.
MACHINE_STEP_SECONDS = 1000.0


def settle_transaction(
    machine: CacheRecovery,
    snapshot: list[Counts],
    *,
    ticks: int = 12,
    start: float = 0.0,
) -> float:
    """Drive a transaction to its end by granting the accounting proof.

    The accounted reading is substituted the moment the machine reaches the
    accounting phase, which is what the supervisor's own drain probe does once
    it has verified a stopped and empty service: the only thing that advances
    the transaction past that phase is scripted here, never awaited.

    Parameters
    ----------
    machine : CacheRecovery
        The transaction under test.
    snapshot : list[Counts]
        The one-element gauge cell the machine reads.
    ticks : int
        How many steps to drive.
    start : float
        The instant the drive starts from.

    Returns
    -------
    float
        The instant the transaction last ticked at.
    """
    now = start
    for _ in range(ticks):
        now += MACHINE_STEP_SECONDS
        if machine.describe(now)["phase"] == "accounting":
            snapshot[0] = gauges()
        machine.tick(now)
    return now


def fixture_cgroup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, procs: str = "4242\n"
) -> None:
    """Point the managed cache's control-group reads at a fixture listing.

    Parameters
    ----------
    tmp_path : Path
        The directory standing in for every unit's control group.
    monkeypatch : pytest.MonkeyPatch
        Fixture used to redirect the control-group lookup.
    procs : str
        The ``cgroup.procs`` contents; the default lists one surviving process.
    """
    (tmp_path / "cgroup.procs").write_text(procs, encoding="ascii")
    monkeypatch.setattr(cgroups, "directory", lambda _path: tmp_path)
