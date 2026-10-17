"""Own requester connections and descriptors until the final reply."""

import contextlib
import os
import typing as typ

from buildlimits import attributes
from buildlimits.descriptor_budget import DescriptorBudget
from buildlimits.descriptors import close_all
from buildlimits.framing import DescriptorTruncationError
from buildlimits.journal import log
from buildlimits.metrics import Metrics, exit_outcome, refusal_reason
from buildlimits.pending_request import Pending as Pending
from buildlimits.protocol import (
    CompileRequest,
    Exit,
    Finished,
    ProtocolError,
    RecoverRequest,
    Refused,
    Reply,
    StatusRequest,
    decode_request,
    recv_frame,
)
from buildlimits.request_connections import (
    answer_recover,
    answer_status,
    drop_recorded,
    record_pending,
    refuse_unrecorded,
    reject_connection,
    send_recorded,
    truncation_refusal,
    unrecorded,
)
from buildlimits.request_connections import check_peer as check_peer
from buildlimits.request_connections import close_quietly as close_quietly
from buildlimits.request_connections import listen as listen
from buildlimits.request_connections import send_quietly as send_quietly
from buildlimits.request_validation import (
    CompileContext,
    CompileRefusal,
    DescriptorHeadroom,
    prepare_compile,
)
from buildlimits.requester_metrics import (
    admission_fields,
    finished_fields,
    rejected_fields,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import selectors
    import socket

    from buildlimits.admission import Counts
    from buildlimits.metrics import MetricsPort


REQUEST_READ_TIMEOUT = 5.0


class Requests:
    """Every request the supervisor has accepted and not yet answered.

    Parameters
    ----------
    selector : selectors.BaseSelector
        The event loop's selector, which watches each open connection.
    clock : Callable[[], float]
        A monotonic clock.
    metrics : MetricsPort | None
        Injected failure-isolated metric emitter.
    counts : Callable[[], Counts] | None
        Reads current admission gauges for each terminal record.
    """

    def __init__(
        self,
        selector: selectors.BaseSelector,
        clock: cabc.Callable[[], float],
        *,
        metrics: MetricsPort | None = None,
        counts: cabc.Callable[[], Counts] | None = None,
    ) -> None:
        """Start with no requests."""
        self.metrics = metrics if metrics is not None else Metrics()
        self.counts = counts
        self.selector = selector
        self.clock = clock
        # Opening one cache-recovery transaction for a recovery request. The
        # supervisor binds the operator trigger and its clock, so a supervisor
        # without a cache answers the request with a refusal instead.
        self.recovery: cabc.Callable[[], dict[str, object]] | None = None
        self.pending: dict[int, Pending] = {}
        self._next_request = 1
        self.failed_replies: set[int] = set()
        self.completed_metadata: dict[str, dict[str, object]] = {}
        self.descriptors = DescriptorBudget()

    @property
    def retained(self) -> int:
        """Count owned request connections and received descriptors.

        Returns
        -------
        int
            Descriptors retained by pending requests, independently of tokens.
        """
        return sum(
            len(entry.fds) + int(entry.conn is not None)
            for entry in self.pending.values()
        )

    def prune_completed(self, groups: cabc.Container[str]) -> None:
        """Retain descriptions only while admission still owns their tokens.

        Parameters
        ----------
        groups : Container[str]
            Group identifiers still accounted for by admission.
        """
        self.completed_metadata = {
            key: value
            for key, value in self.completed_metadata.items()
            if key in groups
        }

    def close_all(self) -> None:
        """Close all request-owned resources at supervisor shutdown."""
        for request_id in list(self.pending):
            self.forget(request_id)
        self.failed_replies.clear()
        self.completed_metadata.clear()

    def accept(
        self, listener: socket.socket, status: cabc.Callable[[], dict[str, object]]
    ) -> Pending | None:
        """Accept one connection and read its request.

        Parameters
        ----------
        listener : socket.socket
            The listening socket, which is readable.
        status : Callable[[], dict[str, object]]
            Produces the status document for a status request.

        Returns
        -------
        Pending | None
            The queued compile request, or ``None`` for status, recovery or
            refusal. A recovery request runs through :attr:`recovery`, which
            the supervisor sets once its managed cache exists.
        """
        # Received descriptors remain ours until registration commits ownership.
        fds: list[int] = []
        conn, _address = listener.accept()
        try:
            conn.settimeout(REQUEST_READ_TIMEOUT)
            check_peer(conn)
            self.descriptors.recheck_if_idle(self.retained, receiving=1)
            frame = recv_frame(
                conn, max_fds=self.descriptors.receipt_ceiling(self.retained)
            )
            if frame is None:
                conn.close()
                return None
            body, fds = frame
            request = decode_request(body)
        except DescriptorTruncationError as error:
            refusal = truncation_refusal(error.body, self.descriptors.budget)
            self._rejected(refusal.reason)
            return refuse_unrecorded(conn, fds, refusal)
        except (ProtocolError, OSError) as error:
            reject_connection(conn, fds, error, log)
            return None
        except BaseException:
            owned, fds[:] = list(fds), []
            close_all(owned)
            close_quietly(conn)
            raise
        with unrecorded(conn, fds):
            match request:
                case StatusRequest():
                    return answer_status(conn, fds, status)
                case RecoverRequest():
                    answer_recover(conn, fds, self.recovery)
                    return None
                case CompileRequest():
                    return self._admit(conn, request, fds)

    def _admit(
        self, conn: socket.socket, request: CompileRequest, fds: list[int]
    ) -> Pending | None:
        """Record a validated compile request, or refuse and reclaim it.

        Returns
        -------
        Pending | None
            The recorded request, or ``None`` after refusal.
        """
        self.descriptors.recheck_if_idle(self.retained, receiving=1 + len(fds))
        plan = prepare_compile(
            request,
            len(fds),
            DescriptorHeadroom(
                self.descriptors.budget - self.retained, self.descriptors.budget
            ),
            context=CompileContext(
                os.getpriority(os.PRIO_PROCESS, 0), attributes.current_limits()
            ),
        )
        if isinstance(plan, CompileRefusal):
            self._rejected(plan.reason)
            return refuse_unrecorded(conn, fds, Refused(plan.reason, plan.message))
        request_id = self._next_request
        entry = Pending(request_id, conn, request, fds, plan, arrived=self.clock())
        try:
            self._record(entry)
        except OSError, ValueError, KeyError:
            self._rejected("busy")
            return refuse_unrecorded(
                conn,
                fds,
                Refused("busy", "cannot register request connection; retry admission"),
            )
        self._next_request += 1
        return entry

    def _rejected(self, reason: str) -> None:
        """Count a validated request refused before registration."""
        fields = admission_fields(None if self.counts is None else self.counts())
        fields.update(rejected_fields(reason))
        self._emit(**fields)

    def _record(self, entry: Pending) -> None:
        """Commit request-connection ownership, rolling back failed registration."""
        record_pending(
            self.selector,
            self.pending,
            entry,
            lambda: self.descriptors.observe(self.retained),
        )

    def disconnected(self, request_id: int) -> Pending | None:
        """Check whether a readable connection means its requester has gone.

        Parameters
        ----------
        request_id : int
            The request whose connection became readable.

        Returns
        -------
        Pending | None
            The disconnected request, or ``None`` while connected.
        """
        entry = self.pending.get(request_id)
        if entry is None or entry.conn is None:
            return None
        try:
            data = entry.conn.recv(1)
        except BlockingIOError:
            return None
        except OSError:
            data = b""
        if data:
            # The protocol has nothing more for the client to send.
            return None
        entry.cancellation = "disconnect"
        drop_recorded(self.selector, entry)
        return entry

    def notify(self, request_id: int, reply: Reply) -> None:
        """Send a progress reply, ignoring a requester that has gone.

        Parameters
        ----------
        request_id : int
            The request to reply to.
        reply : Reply
            The reply.
        """
        if (entry := self.pending.get(request_id)) is not None:
            self._reply(entry, reply)

    def finish(self, request_id: int, exit_: Exit) -> None:
        """Report a group's result and forget its request.

        Parameters
        ----------
        request_id : int
            The request the group served.
        exit_ : Exit
            How the group's leader ended.
        """
        entry = self.pending.get(request_id)
        if entry is None:
            return
        if entry.group_id is not None:
            self.completed_metadata[entry.group_id] = {
                "label": entry.request.label,
                "directory": entry.request.cwd,
                "admitted": entry.admitted,
            }
        try:
            if entry.exec_error is not None:
                self._reply(entry, Refused("exec-failed", entry.exec_error))
            else:
                self._reply(entry, Finished(exit_))
            runtime = self.clock() - (entry.admitted or entry.arrived)
            log(
                f"finished request {request_id} ({entry.group_id}) after "
                f"{runtime:.1f}s: code={exit_.code} signal={exit_.signal}"
            )
        finally:
            self._finished(
                entry,
                outcome="exec_failed"
                if entry.exec_error is not None
                else exit_outcome(exit_),
            )

    def refuse(self, request_id: int, reason: str) -> None:
        """Refuse a request and forget it.

        Parameters
        ----------
        request_id : int
            The request to refuse.
        reason : str
            The machine-readable reason.
        """
        entry = self.pending.get(request_id)
        if entry is None:
            return
        message = entry.exec_error or f"the supervisor refused: {reason}"
        refusal = "exec-failed" if entry.exec_error is not None else reason
        try:
            self._reply(entry, Refused(refusal, message))
        finally:
            self._finished(
                entry,
                outcome="exec_failed" if entry.exec_error is not None else "refused",
                reason=refusal_reason(refusal),
            )

    def cancel_queued(self, request_id: int) -> None:
        """Finish a disconnected request which was never admitted.

        Parameters
        ----------
        request_id : int
            The removed waiting request; repeated calls are harmless.
        """
        entry = self.pending.get(request_id)
        if entry is not None:
            self._finished(entry, outcome="cancelled")

    def _finished(self, entry: Pending, *, outcome: str, reason: str = "none") -> None:
        """Emit a terminal record, reclaiming ownership even if measurement fails."""
        try:
            fields = admission_fields(None if self.counts is None else self.counts())
            fields.update(
                finished_fields(entry, self.clock(), outcome=outcome, reason=reason)
            )
            self._emit(**fields)
        finally:
            self.forget(entry.request_id)

    def _emit(self, **fields: object) -> None:
        """Isolate only transport failures so metrics cannot change admission."""
        with contextlib.suppress(Exception):
            self.metrics.emit("request_finished", **fields)

    def forget(self, request_id: int) -> None:
        """Release everything held for a request.

        Parameters
        ----------
        request_id : int
            The request to forget.
        """
        self.failed_replies.discard(request_id)
        entry = self.pending.pop(request_id, None)
        if entry is None:
            return
        drop_recorded(self.selector, entry)
        fds, entry.fds = entry.fds, []
        close_all(fds)

    def _reply(self, entry: Pending, reply: Reply) -> None:
        """Send a recorded reply and reclaim failed connections."""
        if not send_recorded(entry, reply, REQUEST_READ_TIMEOUT):
            drop_recorded(self.selector, entry)
            self.failed_replies.add(entry.request_id)
