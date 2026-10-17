"""The adapters' heartbeat: progress lines on standard error.

Cargo shows nothing while a compiler runs, so a queued or long compilation
looks hung. The adapter writes a line at a fixed interval, saying first what
the build is waiting for and then how long it has been compiling, as the RFC
requires. Compiling lines keep the ``[rustc-heartbeat]`` prefix of
``notdeadyet``, the managed compiler wrapper the adapter replaces, and the
interval honours its ``RUSTC_HEARTBEAT_SECS`` unless
``BUILD_LIMITS_HEARTBEAT_SECS`` overrides it.
"""

import contextlib
import sys
import time
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc

DEFAULT_HEARTBEAT_SECONDS = 45.0


def crate_label(argv: cabc.Sequence[str]) -> str:
    """Name the crate being compiled, for heartbeat lines.

    Parameters
    ----------
    argv : Sequence[str]
        The compiler argument vector.

    Returns
    -------
    str
        The ``--crate-name`` value, or the compiler's file name.

    Examples
    --------
    >>> crate_label(["rustc", "--crate-name", "serde", "src/lib.rs"])
    'serde'
    >>> crate_label(["/x/rustc", "-vV"])
    'rustc'
    """
    for index, argument in enumerate(argv[:-1]):
        if argument == "--crate-name":
            return argv[index + 1]
    return Path(argv[0]).name


def heartbeat_seconds(env: cabc.Mapping[str, str]) -> float:
    """Read the heartbeat interval.

    Parameters
    ----------
    env : Mapping[str, str]
        The environment.

    Returns
    -------
    float
        ``BUILD_LIMITS_HEARTBEAT_SECS``, then ``RUSTC_HEARTBEAT_SECS``, then
        the default; invalid or non-positive values fall back to the default.
    """
    for name in ("BUILD_LIMITS_HEARTBEAT_SECS", "RUSTC_HEARTBEAT_SECS"):
        with contextlib.suppress(KeyError, ValueError):
            value = float(env[name])
            if value > 0:
                return value
    return DEFAULT_HEARTBEAT_SECONDS


class Heartbeat:
    """Writes progress lines to standard error at a fixed interval.

    Parameters
    ----------
    label : str
        The crate being compiled.
    interval : float
        Seconds between lines.
    clock : Callable[[], float]
        A monotonic clock.
    """

    def __init__(
        self,
        label: str,
        interval: float,
        clock: cabc.Callable[[], float] = time.monotonic,
    ) -> None:
        """Start the waiting phase at the current time."""
        self.label = label
        self.interval = interval
        self.clock = clock
        self.phase_started = clock()
        self.next_due = self.phase_started + interval
        self.running = False
        self.ahead = 0

    def admitted(self) -> None:
        """Switch from waiting lines to compiling lines."""
        self.running = True
        self.phase_started = self.clock()
        self.next_due = self.phase_started + self.interval

    def timeout(self) -> float:
        """Return the seconds until the next line is due.

        Returns
        -------
        float
            A non-negative wait.
        """
        return max(0.0, self.next_due - self.clock())

    def tick(self, waiting_for: str = "a compile slot") -> str | None:
        """Produce a line if one is due.

        Parameters
        ----------
        waiting_for : str
            What a waiting line says the build is waiting for.

        Returns
        -------
        str | None
            The line to write, or ``None``.
        """
        now = self.clock()
        if now < self.next_due:
            return None
        self.next_due = now + self.interval
        elapsed = int(now - self.phase_started)
        if self.running:
            return f"[rustc-heartbeat] {self.label}: still compiling after {elapsed}s"
        ahead = f"{self.ahead} ahead, " if waiting_for == "a compile slot" else ""
        return (
            f"[build-limits] {self.label}: waiting for {waiting_for} "
            f"({ahead}{elapsed}s)"
        )


def write_line(line: str | None) -> None:
    """Write one admission progress line, if supplied.

    Parameters
    ----------
    line : str | None
        Progress text; None means no heartbeat is due.
    """
    if line is not None:
        with contextlib.suppress(OSError):
            sys.stderr.write(line + "\n")
            sys.stderr.flush()
