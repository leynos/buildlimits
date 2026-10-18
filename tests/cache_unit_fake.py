"""A scripted user manager that plays one managed cache unit through its states.

The runner answers ``show`` from the unit's current state and moves the state
on each queued ``stop`` or ``start``, so a test observes the order in which the
supervisor wrote its barrier, asked for the stop and asked for the start,
without a systemd user manager.
"""

import dataclasses as dc
import subprocess  # ruff: ignore[suspicious-subprocess-import] - only CompletedProcess values, never a process.
import typing as typ

from buildlimits.timing import Clock

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

LIVE = "MainPID=4242\nControlGroup=\nActiveState=active\n"
STOPPING = "MainPID=4242\nControlGroup=\nActiveState=deactivating\n"
STOPPED = "MainPID=0\nControlGroup=\nActiveState=inactive\n"


@dc.dataclass(slots=True)
class FakeClock:
    """A hand-driven monotonic clock whose sleep only advances time."""

    time: float = 0.0

    def timing(self) -> Clock:
        """Expose the clock as the supervisor's time-and-sleep port.

        Returns
        -------
        Clock
            A port reading this clock and advancing it instead of sleeping.
        """
        return Clock(now=lambda: self.time, sleep=self.advance)

    def advance(self, seconds: float) -> None:
        """Advance without waiting."""
        self.time += seconds


@dc.dataclass(slots=True)
class CacheUnit:
    """One managed cache unit and the commands asked of it.

    Parameters
    ----------
    barrier : Path
        The restart barrier; its presence is recorded at every stop and start.
    state : str
        The ``show`` output now: ``LIVE``, ``STOPPING`` or ``STOPPED``.
    shows_to_stop : int
        How many ``show`` answers a stopping unit gives before it is stopped.
    refuses_stop : bool
        Whether the manager rejects a queued stop.
    never_stops : bool
        Whether a queued stop is accepted but the unit stays live.
    """

    barrier: Path
    state: str = LIVE
    shows_to_stop: int = 2
    refuses_stop: bool = False
    never_stops: bool = False
    jobs: list[str] = dc.field(default_factory=list)
    barrier_at_job: dict[str, bool] = dc.field(default_factory=dict)

    def runner(self, argv: cabc.Sequence[str]) -> subprocess.CompletedProcess[str]:
        """Answer one ``systemctl`` command as the scripted unit would.

        Returns
        -------
        subprocess.CompletedProcess[str]
            The scripted result.
        """
        if argv[0] == "busctl":
            # The public-bus fallback is unavailable, so a refused private job
            # surfaces as a refusal rather than being retried elsewhere.
            return subprocess.CompletedProcess(argv, 1, "", "no public bus")
        if "show" in argv:
            return self._show(argv)
        verb = "stop" if "stop" in argv else "start"
        self.jobs.append(verb)
        self.barrier_at_job[verb] = self.barrier.exists()
        if verb == "stop" and self.refuses_stop:
            return subprocess.CompletedProcess(argv, 1, "", "Access denied")
        if verb == "stop" and not self.never_stops:
            self.state = STOPPING
        elif verb == "start":
            self.state = LIVE
        return subprocess.CompletedProcess(argv, 0, "", "")

    def _show(self, argv: cabc.Sequence[str]) -> subprocess.CompletedProcess[str]:
        """Report the state, finishing a stop after its scripted polls.

        Returns
        -------
        subprocess.CompletedProcess[str]
            The unit's ``show`` output.
        """
        if self.state == STOPPING and not self.never_stops:
            self.shows_to_stop -= 1
            if self.shows_to_stop < 0:
                self.state = STOPPED
        return subprocess.CompletedProcess(argv, 0, self.state, "")
