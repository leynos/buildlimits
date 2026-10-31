"""Cadence and failure back-off for optional full-emit child samples."""

import typing as typ

from buildlimits.outcome_ports import SampleFailure

if typ.TYPE_CHECKING:
    from buildlimits.outcome_ports import ServerProcessPort

CHILD_SAMPLE_SECONDS = 0.25
_MAX_FAILURE_BACKOFF_SECONDS = 8.0


class ChildSampler:
    """Sample a managed server at a bounded cadence while cached work moves."""

    def __init__(self, server: ServerProcessPort) -> None:
        """Bind the in-memory process port without sampling at construction."""
        self._server = server
        self._next_at = 0.0
        self._failure_backoff = CHILD_SAMPLE_SECONDS

    def poll(
        self, now: float, *, cached_moving: int
    ) -> frozenset[str] | SampleFailure | None:
        """Return one due sample, or None when cadence or activity blocks it.

        Returns
        -------
        frozenset[str] | SampleFailure | None
            One bounded sample result, or None when no sample was due.
        """
        if cached_moving <= 0:
            self._next_at = now
            self._failure_backoff = CHILD_SAMPLE_SECONDS
            return None
        if now < self._next_at:
            return None
        result = self._server.full_emit_metadata()
        if isinstance(result, SampleFailure):
            self._next_at = now + self._failure_backoff
            self._failure_backoff = min(
                self._failure_backoff * 2, _MAX_FAILURE_BACKOFF_SECONDS
            )
            return result
        self._next_at = now + CHILD_SAMPLE_SECONDS
        self._failure_backoff = CHILD_SAMPLE_SECONDS
        return result


__all__ = ["CHILD_SAMPLE_SECONDS", "ChildSampler"]
