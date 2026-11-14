"""Decide which supervisor lines the journal receives, and at which priority.

The composition root resolves :class:`RoutineLines` once at start-up, so the
decision depends on the start-up environment rather than on whatever the
process environment holds when a line is written.
"""

import dataclasses
import os
import sys
import typing as typ

from buildlimits import journal

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.effects import Exit
    from buildlimits.requesters import Pending

# Routine lines repeat what the metric records already hold, once per compile,
# so they are written only when an operator asks for them.
ROUTINE_LINES_VARIABLE = "BUILD_LIMITS_ROUTINE_LINES"
# systemd names the stream it connected for the unit's standard error here.
JOURNAL_STREAM_VARIABLE = "JOURNAL_STREAM"
# systemd reads a leading ``<N>`` as the line's syslog priority when stderr is a
# journal stream, so requested routine lines stay out of ``journalctl -p info``.
DEBUG_PRIORITY = "<7>"


def stream_is_journal(stream: typ.TextIO, environ: cabc.Mapping[str, str]) -> bool:
    """Report whether ``stream`` is the journal stream systemd connected.

    Parameters
    ----------
    stream : IO[str]
        The stream a routine line would be written to.
    environ : Mapping[str, str]
        The environment naming the journal stream, as systemd sets it.

    Returns
    -------
    bool
        True only when ``JOURNAL_STREAM`` names the stream's device and inode,
        so a terminal or a redirected file never sees a priority prefix.
    """
    device, _separator, inode = environ.get(JOURNAL_STREAM_VARIABLE, "").partition(":")
    try:
        status = os.fstat(stream.fileno())
        return (status.st_dev, status.st_ino) == (int(device), int(inode))
    except AttributeError, OSError, ValueError:
        return False


@dataclasses.dataclass(frozen=True, slots=True)
class RoutineLines:
    """One supervisor's resolved routine-line setting.

    Parameters
    ----------
    enabled : bool
        Whether routine lines are written at all.
    debug_prefix : str
        The priority prefix for routine lines, empty when stderr is not the
        stream systemd connected.

    Examples
    --------
    The switch is read from the environment, and writing is silent either way:
    a suppressed routine line reaches no stream, and a requested one reaches
    standard error rather than standard output.

    >>> RoutineLines.from_environment({}, sys.stderr).enabled
    False
    >>> RoutineLines.from_environment({ROUTINE_LINES_VARIABLE: "1"}).enabled
    True
    """

    enabled: bool = False
    debug_prefix: str = ""

    @classmethod
    def from_environment(
        cls,
        environ: cabc.Mapping[str, str] | None = None,
        stream: typ.TextIO | None = None,
    ) -> RoutineLines:
        """Resolve the setting from the start-up environment.

        Parameters
        ----------
        environ : Mapping[str, str] | None
            The process environment; ``os.environ`` when ``None``.
        stream : IO[str] | None
            The stream later written to; ``sys.stderr`` when ``None``.

        Returns
        -------
        RoutineLines
            The resolved setting.
        """
        resolved = os.environ if environ is None else environ
        destination = sys.stderr if stream is None else stream
        return cls(
            enabled=resolved.get(ROUTINE_LINES_VARIABLE) == "1",
            debug_prefix=(
                DEBUG_PRIORITY if stream_is_journal(destination, resolved) else ""
            ),
        )

    def write(self, message: str, *, routine: bool) -> None:
        """Write one supervisor line, applying the routine-line setting.

        Parameters
        ----------
        message : str
            The line, without the supervisor's prefix.
        routine : bool
            Whether the line records healthy, high-volume work.
        """
        if not routine:
            journal.log(message)
        elif self.enabled:
            journal.log(message, priority_prefix=self.debug_prefix)


def is_routine_finish(entry: Pending, exit_: Exit) -> bool:
    """Report whether a finish only confirms healthy work.

    Parameters
    ----------
    entry : Pending
        The finished request; a recorded exec error means it never started.
    exit_ : Exit
        How the group's leader ended.

    Returns
    -------
    bool
        True only for a clean exit, which the metric records already describe.

    Examples
    --------
    >>> is_routine_finish(entry, Exit(code=0))  # doctest: +SKIP
    True
    >>> is_routine_finish(entry, Exit(code=1))  # doctest: +SKIP
    False
    """
    return entry.exec_error is None and exit_.code == 0 and exit_.signal is None
