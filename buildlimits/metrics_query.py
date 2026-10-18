"""Read and validate bounded compile-admission metrics from the user journal.

This module gives the development-host report a capped history window while
discarding raw journal content and enforcing the supervisor trust boundary.
Use :func:`query` to obtain validated records for aggregation::

    window = query(since="1h", max_records=10_000, source="all")
    print(len(window.records), window.truncated)

The result distinguishes a readable empty window from query failure and marks
windows that reach their raw record cap as truncated.
"""

import dataclasses
import re
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed journal command, never a shell.
import typing as typ

from buildlimits import metrics_transport
from buildlimits.metrics import FIELD_KINDS
from buildlimits.metrics_records import SUPERVISOR_UNIT as SUPERVISOR_UNIT
from buildlimits.metrics_records import MetricsQueryError as MetricsQueryError
from buildlimits.metrics_records import Record as Record
from buildlimits.metrics_records import _decode

if typ.TYPE_CHECKING:
    import collections.abc as cabc

DEFAULT_MAX_RECORDS = 10_000
MAX_RECORDS = 100_000
QUERY_FIELDS = frozenset({
    "BUILD_LIMITS_SCHEMA",
    "BUILD_LIMITS_SOURCE",
    "BUILD_LIMITS_EVENT",
    "_SYSTEMD_USER_UNIT",
    "__REALTIME_TIMESTAMP",
    *(f"BUILD_LIMITS_{name.upper()}" for name in FIELD_KINDS),
})
WINDOW_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


@dataclasses.dataclass(frozen=True, slots=True)
class Window:
    """A bounded journal result and its configured rate denominator.

    Attributes
    ----------
    records : tuple[Record, ...]
        Accepted records; source selection happens after the journal cap.
    seconds : int
        Requested lookback duration.
    truncated : bool
        Whether the raw journal result filled the requested cap.
    """

    records: tuple[Record, ...]
    seconds: int
    truncated: bool


def window_seconds(window: str) -> int:
    """Parse a positive lookback expressed in seconds, minutes, hours or days.

    Parameters
    ----------
    window : str
        A positive integer followed by ``s``, ``m``, ``h``, ``d`` or ``w``.

    Returns
    -------
    int
        The lookback in seconds.

    Raises
    ------
    ValueError
        If the lookback is malformed or zero.
    """
    matched = re.fullmatch(r"([1-9][0-9]{0,8})([smhdw])", window)
    if matched is None:
        msg = "--since must be a positive duration such as 30m or 1h"
        raise ValueError(msg)
    return int(matched[1]) * WINDOW_UNITS[matched[2]]


def journal_runner(argv: cabc.Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run a bounded journal query without a shell.

    Parameters
    ----------
    argv : Sequence[str]
        The fixed journal command and validated query options.

    Returns
    -------
    subprocess.CompletedProcess[str]
        Captured journal output and status.
    """
    return metrics_transport.run(argv)


def query(
    since: str = "1h",
    max_records: int = DEFAULT_MAX_RECORDS,
    source: str = "all",
    runner: cabc.Callable[
        [cabc.Sequence[str]], subprocess.CompletedProcess[str]
    ] = journal_runner,
) -> Window:
    """Query the user journal and enforce source trust before aggregation.

    Parameters
    ----------
    since : str
        Positive duration lookback, normalized for journalctl.
    max_records : int
        Raw record cap from 1 to 100,000, including rejected records.
    source : str
        ``all``, ``supervisor`` or ``adapter``.
    runner : Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
        Injected command transport.

    Returns
    -------
    Window
        Accepted records with a raw-cap truncation indicator.

    Raises
    ------
    ValueError
        If query options are invalid.
    MetricsQueryError
        If journalctl fails, reports diagnostics or emits malformed JSON.
    """  # ruff: ignore[docstring-extraneous-exception] - propagated by _validate_query_options, _query_lines and _decode.
    seconds = _validate_query_options(since, max_records, source)
    argv = [
        "journalctl",
        "--user",
        "--output=json",
        "--no-pager",
        f"--output-fields={','.join(sorted(QUERY_FIELDS))}",
        f"--since=-{since}",
        f"--lines={max_records}",
        "BUILD_LIMITS_SCHEMA=1",
    ]
    lines = _query_lines(argv, max_records, runner)
    return Window(_decode(lines, source), seconds, len(lines) == max_records)


def _validate_query_options(since: str, max_records: int, source: str) -> int:
    """Validate options in public error order and return lookback seconds.

    Returns
    -------
    int
        The parsed positive lookback in seconds.

    Raises
    ------
    ValueError
        If the window, raw record cap or source is invalid.
    """
    seconds = window_seconds(since)
    is_counter = isinstance(max_records, int) and not isinstance(max_records, bool)
    if not is_counter or not 1 <= max_records <= MAX_RECORDS:
        msg = "--max-records must be between 1 and 100000"
        raise ValueError(msg)
    if source not in {"all", "supervisor", "adapter"}:
        msg = "--source must be all, supervisor or adapter"
        raise ValueError(msg)
    return seconds


def _query_lines(
    argv: cabc.Sequence[str],
    max_records: int,
    runner: cabc.Callable[[cabc.Sequence[str]], subprocess.CompletedProcess[str]],
) -> list[str]:
    """Read capped nonblank lines, translating transport failures with causes.

    Returns
    -------
    list[str]
        Nonblank raw lines, unchanged and within the record cap.

    Raises
    ------
    MetricsQueryError
        If execution fails, diagnostics are nonblank or output exceeds the cap.
    """
    try:
        result = runner(argv)
    except metrics_transport.JournalOutputError as error:
        raise MetricsQueryError(str(error)) from error
    except (OSError, subprocess.SubprocessError) as error:
        msg = "cannot execute the user-journal query"
        raise MetricsQueryError(msg) from error
    msg = "user-journal query failed; verify journal access with journalctl --user"
    if (result.returncode, result.stderr.strip()) == (0, ""):
        lines = list(filter(str.strip, result.stdout.splitlines()))
        if len(lines) <= max_records:
            return lines
        msg = "journal query exceeded its record cap"
    raise MetricsQueryError(msg)
