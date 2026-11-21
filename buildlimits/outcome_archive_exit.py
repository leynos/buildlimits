"""Map an archive run's outcome to the status a systemd timer records.

The report model describes what happened; the process status is the command's
contract with the timer. Keeping the mapping here lets the model stay free of
process concerns and gives the command and its tests one definition of the
documented statuses.
"""

import typing as typ

if typ.TYPE_CHECKING:
    from buildlimits.outcome_archive_report import RunStatus

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_CORRUPT = 3
EXIT_TEMPFAIL = 75

__all__ = [
    "EXIT_CORRUPT",
    "EXIT_FAILED",
    "EXIT_OK",
    "EXIT_TEMPFAIL",
    "EXIT_USAGE",
    "exit_status",
]


def exit_status(status: RunStatus) -> int:
    """Return the process status a timer should record for one outcome.

    Parameters
    ----------
    status : RunStatus
        The closed run classification the report carries.

    Returns
    -------
    int
        Zero for success, one for a failed run, three for corruption, and 75
        when a retry may succeed.

    Examples
    --------
    A busy run is retryable rather than failed, so a timer tries again::

        exit_status("busy") == EXIT_TEMPFAIL
    """
    if status in {"busy", "no_headroom", "unsettled"}:
        return EXIT_TEMPFAIL
    if status == "corrupt":
        return EXIT_CORRUPT
    if status == "failed":
        return EXIT_FAILED
    return EXIT_OK
