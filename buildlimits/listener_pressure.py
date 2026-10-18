"""Control-path descriptor pressure without changing compiler admission health.

The listener temporarily leaves new connections in its kernel backlog. Existing
registered requester sockets and pool/signal watches remain usable. Only the
supervisor uses this selector policy; request ownership stays in Requests.
"""

import contextlib
import errno
import selectors
import typing as typ

from buildlimits.journal import log
from buildlimits.metrics import Metrics

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import socket

    from buildlimits.descriptor_budget import DescriptorBudget
    from buildlimits.metrics import MetricsPort


RETRY_SECONDS = 0.1


class ListenerPressure:
    """Pause new acceptance after EMFILE/ENFILE and retry on the next tick.

    Parameters
    ----------
    selector : selectors.BaseSelector
        Supervisor control-loop selector.
    clock : Callable[[], float]
        Monotonic clock used to bound retries and diagnostic frequency.
    metrics : MetricsPort | None
        Failure-isolated observation port; absent discards metric records.
    """

    def __init__(
        self,
        selector: selectors.BaseSelector,
        clock: cabc.Callable[[], float],
        *,
        metrics: MetricsPort | None = None,
    ) -> None:
        """Start with acceptance enabled and no emitted pressure diagnostic."""
        self.selector = selector
        self.clock = clock
        self.metrics = metrics if metrics is not None else Metrics()
        self._pause_started = 0.0
        self.paused = False
        self.next_log = 0.0
        self.next_resume = 0.0

    def pause(
        self,
        listener: socket.socket,
        number: int,
        limits: DescriptorBudget,
        retained: int,
    ) -> None:
        """Preserve backlog requests while releasing the listener watch.

        Parameters
        ----------
        listener : socket.socket
            Owned listening socket; it remains open.
        number : int
            EMFILE or ENFILE from accepting a new connection.
        limits : DescriptorBudget
            Effective startup limits and reserved descriptor budget.
        retained : int
            Request-owned descriptor count, independent of compiler tokens.
        """
        with contextlib.suppress(KeyError, ValueError, OSError):
            self.selector.unregister(listener)
        was_paused = self.paused
        self.paused = True
        now = self.clock()
        self.next_resume = now + RETRY_SECONDS
        if not was_paused:
            self._pause_started = now
            self._emit("paused", number, now)
        if now >= self.next_log:
            self.next_log = now + 5.0
            with contextlib.suppress(OSError):
                log(
                    f"request accept pressure: errno={number} "
                    f"nofile={limits.soft}:{limits.hard} retained={retained}; "
                    "retrying after a tick"
                )

    def resume(self, listener: socket.socket | None) -> None:
        """Restore the watch on a later tick without bypassing admission.

        Parameters
        ----------
        listener : socket.socket | None
            The same owned listener, when the supervisor has started.

        Notes
        -----
        Registration propagates OSError for failures other than descriptor pressure.
        The ownership validator also propagates RuntimeError if a different
        selector owner has registered the listener's descriptor.
        """
        now = self.clock()
        if not self.paused or listener is None:
            return
        if now < self.next_resume:
            return
        # Failed watch registration must also wait before trying again.
        self.next_resume = now + RETRY_SECONDS
        if self._attempt_resume(listener, now):
            self.paused = False
            self._emit("resumed", None, now)

    def _attempt_resume(self, listener: socket.socket, now: float) -> bool:
        """Observe a due registration attempt without changing error authority.

        Returns
        -------
        bool
            True after restoration, or False while descriptor pressure persists.

        Raises
        ------
        OSError
            If restoration fails for a reason other than descriptor pressure.
        """
        try:
            self._restore_watch(listener)
        except OSError as error:
            self._emit("resume_failed", error.errno, now)
            if error.errno not in {errno.EMFILE, errno.ENFILE}:
                raise
            return False
        except Exception:
            # Observe registration faults without translating their exceptions.
            self._emit("resume_failed", None, now)
            raise
        return True

    def _emit(self, state: str, number: int | None, now: float) -> None:
        """Read cached listener state and drop all observation failures."""
        with contextlib.suppress(Exception):
            self.metrics.emit(
                "accept_pressure",
                accept_state=state,
                accept_errno={errno.EMFILE: "emfile", errno.ENFILE: "enfile"}.get(
                    number, "none"
                ),
                accept_paused=self.paused,
                accept_pause_seconds=max(0.0, now - self._pause_started),
            )

    def _restore_watch(self, listener: socket.socket) -> None:
        """Register only an unowned listener descriptor.

        Raises
        ------
        RuntimeError
            If a different selector owner already holds this descriptor.
        """
        try:
            existing = self.selector.get_key(listener)
        except KeyError:
            self.selector.register(listener, selectors.EVENT_READ, "listener")
            return
        if existing.data != "listener":
            msg = "listener descriptor is registered to a different owner"
            raise RuntimeError(msg)
