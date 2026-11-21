"""The bounded line a command writes when it retries an unsettled reading.

A retried scan is otherwise invisible: only the final settled or unsettled
result reaches the report. Writing one line per attempt lets an operator see a
store that keeps moving, while the notice stays free of paths and host detail.
"""

import sys

__all__ = ["ignore_retry", "report_retry"]


def report_retry(operation: str, attempt: int, category: str) -> None:
    """Write one identifier-free retry notice to standard error.

    Parameters
    ----------
    operation : str
        The closed operation name, such as ``"snapshot"``.
    attempt : int
        The attempt that failed, counting from one.
    category : str
        The closed reason the reading was refused, such as
        ``"inventory_changed"``.

    Examples
    --------
    ``report_retry("snapshot", 2, "inventory_changed")`` writes
    ``retry: snapshot attempt 2 reason inventory_changed``.
    """
    print(f"retry: {operation} attempt {attempt} reason {category}", file=sys.stderr)


def ignore_retry(_operation: str, _attempt: int, _category: str) -> None:
    """Discard one retry notice when the caller reports none."""
