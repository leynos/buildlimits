"""Shared doubles for the metrics adapter tests."""


def unavailable_snapshot() -> dict[str, object]:
    """Model a cached-state reader failure without sampling resources.

    Raises
    ------
    RuntimeError
        Always, carrying a private message that must not reach the sink.
    """
    msg = "private cached-state fault"
    raise RuntimeError(msg)
