"""Decide which supervisor lines the journal receives, and at which priority.

The composition root calls :func:`journal_writer` once at start-up, so the
decision depends on the start-up environment rather than on whatever the
process environment holds when a line is written.
"""

import dataclasses
import enum
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


class JournalStream(enum.Enum):
    """What the start-up environment says about one supervisor stream.

    Attributes
    ----------
    JOURNAL
        ``JOURNAL_STREAM`` names this stream's device and inode.
    OTHER
        ``JOURNAL_STREAM`` is absent or malformed, or names another stream.
    UNINSPECTABLE
        ``JOURNAL_STREAM`` is well formed, but the stream has no descriptor
        this process can inspect, so the probe cannot tell the two apart.
    """

    JOURNAL = "journal"
    OTHER = "other"
    UNINSPECTABLE = "uninspectable"


def _named_stream(environ: cabc.Mapping[str, str]) -> tuple[int, int] | None:
    """Parse the device and inode systemd recorded, if it recorded both.

    Returns
    -------
    tuple[int, int] | None
        The named device and inode, or ``None`` when the variable is absent or
        does not carry two integers.
    """
    device, _separator, inode = environ.get(JOURNAL_STREAM_VARIABLE, "").partition(":")
    try:
        return int(device), int(inode)
    except ValueError:
        return None


def journal_stream(
    stream: typ.TextIO, environ: cabc.Mapping[str, str]
) -> JournalStream:
    """Classify ``stream`` against the journal stream systemd connected.

    Parameters
    ----------
    stream : IO[str]
        The stream a routine line would be written to.
    environ : Mapping[str, str]
        The environment naming the journal stream, as systemd sets it.

    Returns
    -------
    JournalStream
        ``JOURNAL`` only when ``JOURNAL_STREAM`` names the stream's device and
        inode, so a terminal or a redirected file never sees a priority prefix.
        A stream the probe cannot inspect is reported as ``UNINSPECTABLE``
        rather than folded into ``OTHER``, so the caller decides what an
        unreadable descriptor means instead of the probe guessing.

    Notes
    -----
    ``fileno`` is not total: a stream without a descriptor raises
    ``AttributeError``, an in-memory stream raises ``OSError`` through
    ``io.UnsupportedOperation``, and a closed stream raises ``ValueError``.
    All three are the same fact for this probe, so all three are inspected
    rather than raised.
    """
    named = _named_stream(environ)
    if named is None:
        return JournalStream.OTHER
    try:
        status = os.fstat(stream.fileno())
    except AttributeError, OSError, ValueError:
        return JournalStream.UNINSPECTABLE
    if (status.st_dev, status.st_ino) == named:
        return JournalStream.JOURNAL
    return JournalStream.OTHER


@dataclasses.dataclass(frozen=True, slots=True)
class RoutineLines:
    """One supervisor's resolved routine-line setting.

    Parameters
    ----------
    enabled : bool
        Whether routine lines are written at all.
    debug_prefix : str
        The priority prefix for routine lines, empty unless the stream they are
        written to is systemd's journal stream.

    Examples
    --------
    >>> RoutineLines().enabled
    False
    >>> RoutineLines(enabled=True, debug_prefix=DEBUG_PRIORITY).debug_prefix
    '<7>'
    """

    enabled: bool = False
    debug_prefix: str = ""


def resolve_routine_lines(
    environ: cabc.Mapping[str, str] | None = None,
    stream: typ.TextIO | None = None,
) -> RoutineLines:
    """Resolve the setting from the start-up environment.

    Parameters
    ----------
    environ : Mapping[str, str] | None
        The process environment; ``os.environ`` when ``None``.
    stream : IO[str] | None
        The stream the lines will be written to; ``sys.stderr`` when ``None``.

    Returns
    -------
    RoutineLines
        The resolved setting.

    Notes
    -----
    A stream the probe cannot inspect resolves without a priority prefix. Only
    a journal stream understands the prefix, and a line that printed ``<7>``
    literally would be worse than a routine line written at the default
    priority, so the fail-safe keeps the line visible.
    """
    resolved = os.environ if environ is None else environ
    destination = sys.stderr if stream is None else stream
    prefix = (
        DEBUG_PRIORITY
        if journal_stream(destination, resolved) is JournalStream.JOURNAL
        else ""
    )
    return RoutineLines(
        enabled=resolved.get(ROUTINE_LINES_VARIABLE) == "1",
        debug_prefix=prefix,
    )


@dataclasses.dataclass(frozen=True, slots=True)
class RoutineLineWriter:
    """Write supervisor lines to one stream, applying the routine setting.

    Parameters
    ----------
    lines : RoutineLines
        The resolved setting.
    stream : IO[str] | None
        The stream that receives every line, including the one whose device and
        inode decided ``lines.debug_prefix``; ``sys.stderr`` when ``None``.

    Notes
    -----
    The writer owns both halves of the decision: :attr:`lines` says what to
    write, and :attr:`stream` is where it is written. Passing the same stream
    to the probe and to the writer is therefore structural rather than a
    convention a caller has to remember.

    Examples
    --------
    Standard output stands in for the journal here so the example can show the
    line; a supervisor always injects the stream it resolved the prefix for.

    >>> writer = RoutineLineWriter(RoutineLines(True, DEBUG_PRIORITY), sys.stdout)
    >>> writer.write("admitted request 1", routine=True)
    <7>build-limits-supervisor: admitted request 1
    """

    lines: RoutineLines = RoutineLines()
    stream: typ.TextIO | None = None

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
            journal.log(message, stream=self.stream)
        elif self.lines.enabled:
            journal.log(
                message,
                stream=self.stream,
                priority_prefix=self.lines.debug_prefix,
            )


def journal_writer(
    environ: cabc.Mapping[str, str] | None = None,
    stream: typ.TextIO | None = None,
) -> RoutineLineWriter:
    """Resolve the start-up setting and bind it to the stream it governs.

    Parameters
    ----------
    environ : Mapping[str, str] | None
        The process environment; ``os.environ`` when ``None``.
    stream : IO[str] | None
        The stream that receives every line; ``sys.stderr`` when ``None``.

    Returns
    -------
    RoutineLineWriter
        A writer whose stream is the one its priority prefix was resolved for.
    """
    destination = sys.stderr if stream is None else stream
    return RoutineLineWriter(
        resolve_routine_lines(environ, destination),
        destination,
    )


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
