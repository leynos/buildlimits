"""Shared doubles for the metrics adapter tests."""


class SwitchableSnapshot:
    """A cached-state reader a test can fail on demand.

    Parameters
    ----------
    state : dict[str, object]
        Values the reader returns while it is readable.
    """

    def __init__(self, state: dict[str, object]) -> None:
        """Hold the values and start readable."""
        self.state = state
        self.readable = True

    def __call__(self) -> dict[str, object]:
        """Read the held values, or raise while the reader is unreadable.

        Returns
        -------
        dict[str, object]
            The held values.

        Raises
        ------
        RuntimeError
            While ``readable`` is false, carrying a private message that must
            not reach the sink.
        """
        if not self.readable:
            msg = "private cached-state fault"
            raise RuntimeError(msg)
        return self.state


def unavailable_snapshot() -> dict[str, object]:
    """Model a cached-state reader failure without sampling resources.

    Raises
    ------
    RuntimeError
        Always, carrying a private message that must not reach the sink.
    """
    msg = "private cached-state fault"
    raise RuntimeError(msg)
