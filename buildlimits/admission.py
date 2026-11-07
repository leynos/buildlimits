"""The admission state machine: queueing, token accounting and draining.

This core performs no I/O: each event updates state and returns effects the caller
must carry out, in order. The supervisor executes those effects against the
token pool, systemd and client connections. Keeping the decisions here makes
the RFC's invariants checkable without devices, sockets or processes:

- at most one token is held per compiler group, and a waiting request holds
  none;
- a token returns only after its group's control group is empty, and exactly
  once;
- a group composed with a detached cache that ends abnormally keeps its token
  until the cache's live compilations fit within the server-drain tokens that
  remain after its return, while a group whose cache keeps its work inside the
  scope returns its token as soon as the scope empties;
- an unhealthy supervisor admits nothing and drains everything;
- a closing supervisor launches nothing but lets running groups finish, then
  refuses what waits so that its requesters can try again.

Tokens are opaque single bytes. A token read from the pool is written back
unchanged, as the jobserver protocol requires.
"""

import collections
import dataclasses as dc
import typing as typ

from buildlimits.admission_state import Counts, Gauges, Group, GroupState
from buildlimits.effects import (
    Effect,
    Exit,
    GroupId,
    Launch,
    NotifyFinished,
    NotifyQueued,
    Refuse,
    RequestId,
    RequestToken,
    ReturnToken,
    Shutdown,
    Terminate,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

__all__ = [
    "Admission",
    "Counts",
    "Effect",
    "Group",
    "GroupState",
    "Launch",
    "NotifyFinished",
    "NotifyQueued",
    "Refuse",
    "RequestToken",
    "ReturnToken",
    "Shutdown",
    "Terminate",
]


@dc.dataclass(slots=True)
class Admission(Gauges):
    """The supervisor's admission state.

    Parameters
    ----------
    cached : bool
        Whether new groups run through the configured cache. The supervisor
        updates this as the backend's health changes.
    cache_detached : bool
        Whether that backend's accepted work may outlive a group's scope. The
        supervisor projects it once from the backend, so admission never
        reads a mode string; it defaults to ``True`` so a cached group whose
        backend was never attributed keeps its token.
    capacity : int
        Configured pool capacity reported by the gauge seam.
    closing : bool
        Whether the supervisor is winding down: it launches and terminates
        nothing, and queues what arrives while a group still runs.
    cache_allowed : Callable[[], bool]
        Whether composition with the server is permitted at all right now.
        Defaults to always true, so an admission state that is not wired to a
        server keeps the plain reading of ``cached``; the supervisor installs
        its watch's predicate so that a recovery which has taken the cache out
        of service cannot be raced by a launch composed from a stale
        ``cached`` value. It is consulted where the launch decision is made
        rather than where the token is requested, because that is the last
        point at which the answer is still current.

    Examples
    --------
    >>> admission = Admission()
    >>> admission.submit(1)
    [NotifyQueued(request_id=1, position=0), RequestToken()]
    >>> admission.token_arrived(b"+")
    [Launch(request_id=1, group_id='g1', cached=False)]
    >>> admission.leader_exited("g1", Exit(code=0))
    [Terminate(group_id='g1')]
    >>> admission.group_emptied("g1")[0]
    ReturnToken(token=b'+')
    """

    cached: bool = False
    # The composition root projects the backend's ``in_scope_work`` here. The
    # default presumes detached, so a caller that composes a cached group and
    # does not say otherwise retains its token rather than releasing it: an
    # unattributed cache must never be the reason a token goes back early.
    cache_detached: bool = True
    capacity: int = 0
    healthy: bool = True
    queue: collections.deque[RequestId] = dc.field(default_factory=collections.deque)
    cacheable: dict[RequestId, bool] = dc.field(default_factory=dict)
    groups: dict[GroupId, Group] = dc.field(default_factory=dict)
    token_requested: bool = False
    closing: bool = False
    cache_allowed: cabc.Callable[[], bool] = dc.field(default=lambda: True, repr=False)
    _next_group: int = 1

    def submit(self, request_id: RequestId, *, cacheable: bool = True) -> list[Effect]:
        """Queue a new request.

        Parameters
        ----------
        request_id : RequestId
            A request identifier unique for the supervisor's lifetime.
        cacheable : bool
            Whether the request may run through the configured cache.
            Rustdoc invocations are not cacheable.

        Returns
        -------
        list[Effect]
            A queue notification and, when needed, a token request; or a
            refusal while unhealthy or once a closing supervisor has no group.
        """
        if not self.healthy:
            return [Refuse(request_id, "unhealthy")]
        if self.closing and not self.groups:
            return [Refuse(request_id, "restarting")]
        self.queue.append(request_id)
        self.cacheable[request_id] = cacheable
        effects: list[Effect] = [NotifyQueued(request_id, len(self.queue) - 1)]
        return effects + self._demand()

    def token_arrived(self, token: bytes) -> list[Effect]:
        """Admit the oldest waiting request with a token read from the pool.

        Parameters
        ----------
        token : bytes
            The single-byte token the pool supplied.

        Returns
        -------
        list[Effect]
            A launch and any further token request, or the token's return
            when nothing is waiting or the supervisor is unhealthy or closing.
        """
        self.token_requested = False
        if not self.admitting or not self.queue:
            return [ReturnToken(token)]
        request_id = self.queue.popleft()
        cached = self.cacheable.pop(request_id) and self.cached
        # Read at the decision itself: a token that waited through a recovery
        # must not be composed with a server the transaction has since stopped.
        cached = cached and self.cache_allowed()
        group_id = f"g{self._next_group}"
        self._next_group += 1
        self.groups[group_id] = Group(
            group_id,
            request_id,
            token,
            cached=cached,
            detached=cached and self.cache_detached,
        )
        effects: list[Effect] = [Launch(request_id, group_id, cached=cached)]
        effects.extend(self._positions())
        return effects + self._demand()

    def launch_failed(self, group_id: GroupId) -> list[Effect]:
        """Account for a group whose compiler never started.

        The caller has already killed the held child and confirmed that no
        process of the group runs, so nothing ran for the request. While a
        graceful stop is under way the request is therefore refused as
        ``restarting``, which its adapter retries, rather than as a launch
        failure: a stop can itself break the placement, for instance by
        signalling the ``busctl`` that creates the scope.

        Parameters
        ----------
        group_id : GroupId
            The group whose launch failed.

        Returns
        -------
        list[Effect]
            The token's return and the request's refusal.
        """
        group = self.groups.pop(group_id)
        reason = "restarting" if self.winding_down else "launch-failed"
        return [
            ReturnToken(group.token),
            Refuse(group.request_id, reason),
            *self._maybe_shutdown(),
        ]

    def cancel(self, request_id: RequestId) -> list[Effect]:
        """Cancel a request whose requester disconnected.

        Parameters
        ----------
        request_id : RequestId
            The cancelled request.

        Returns
        -------
        list[Effect]
            Nothing for a waiting request, which is simply removed; a
            termination for a running group, which keeps its token until
            it drains.
        """
        if request_id in self.queue:
            self.queue.remove(request_id)
            del self.cacheable[request_id]
            return self._positions()
        for group in self.groups.values():
            if group.request_id == request_id and not group.cancelled:
                group.cancelled = True
                if group.state is GroupState.RUNNING:
                    return [Terminate(group.group_id)]
        return []

    def leader_exited(self, group_id: GroupId, exit_: Exit) -> list[Effect]:
        """Record that a group's leading process ended.

        Parameters
        ----------
        group_id : GroupId
            The group whose leader ended.
        exit_ : Exit
            How it ended.

        Returns
        -------
        list[Effect]
            A termination of any leftover processes in the group.
        """
        group = self.groups[group_id]
        group.exit = exit_
        group.state = GroupState.DRAINING
        # Anything left after the leader is an orphan, such as a cache server.
        return [Terminate(group_id)]

    def group_emptied(self, group_id: GroupId) -> list[Effect]:
        """Account for a group whose control group is now empty.

        Parameters
        ----------
        group_id : GroupId
            The group that has no processes left.

        Returns
        -------
        list[Effect]
            The requester's result, and the token's return unless the group
            may have left a compilation running in a detached cache.
        """
        group = self.groups[group_id]
        # A group can empty before its leader's exit is reaped; report that
        # as an unknown ending rather than inventing a status.
        exit_ = group.exit if group.exit is not None else Exit()
        effects: list[Effect] = [NotifyFinished(group.request_id, exit_)]
        # Both halves are needed: a cached group whose backend works in scope
        # cannot leave anything behind, and an uncached one never reached a
        # cache. Only then does the ending itself have to be consulted.
        may_hold = group.detached and group.may_orphan_server_work
        if group.cached and may_hold:
            group.state = GroupState.SERVER_DRAIN
            return effects
        del self.groups[group_id]
        return [ReturnToken(group.token), *effects, *self._maybe_shutdown()]

    def server_activity(self, active_compiles: int) -> list[Effect]:
        """Release server-drain tokens that the server's load no longer needs.

        Oldest first, a token returns only if the server's live compilations
        still fit within the server-drain tokens that remain. Running and
        draining cached tokens never count: ``group_emptied`` returns them
        without another poll.

        Parameters
        ----------
        active_compiles : int
            Compilations currently running in the detached cache.

        Returns
        -------
        list[Effect]
            The returns of every token that can now be released.
        """
        draining = [
            group
            for group in self.groups.values()
            if group.state is GroupState.SERVER_DRAIN
        ]
        releasable = max(0, len(draining) - active_compiles)
        effects: list[Effect] = []
        for group in draining[:releasable]:
            del self.groups[group.group_id]
            effects.append(ReturnToken(group.token))
        return effects + self._maybe_shutdown()

    def health_failed(self) -> list[Effect]:
        """Stop admitting, refuse the queue, and drain every group.

        Returns
        -------
        list[Effect]
            Refusals for waiting requests, terminations for running groups,
            and a shutdown when nothing remains.
        """
        self.healthy = False
        effects: list[Effect] = [Refuse(rid, "unhealthy") for rid in self.queue]
        self.queue.clear()
        self.cacheable.clear()
        for group in self.groups.values():
            if group.state is GroupState.RUNNING:
                group.cancelled = True
                effects.append(Terminate(group.group_id))
        return effects + self._maybe_shutdown()

    def _wants_token(self) -> bool:
        """Report whether a request waits with no token already on its way.

        Returns
        -------
        bool
            Whether the pool should be asked for a token.
        """
        return self.admitting and bool(self.queue) and not self.token_requested

    def _demand(self) -> list[Effect]:
        """Request a token when requests wait and none is on its way.

        Returns
        -------
        list[Effect]
            A token request, or nothing.
        """
        if self._wants_token():
            self.token_requested = True
            return [RequestToken()]
        return []

    def _positions(self) -> list[Effect]:
        """Tell every waiting request its new position.

        Returns
        -------
        list[Effect]
            One queue notification per waiting request.
        """
        return [NotifyQueued(rid, index) for index, rid in enumerate(self.queue)]

    def begin_closing(self) -> list[Effect]:
        """Stop launching and let every running group finish; idempotent.

        Returns
        -------
        list[Effect]
            Nothing while groups remain; otherwise the refusal of what waits.
        """
        if self.closing:
            return []
        self.closing = True
        return self._maybe_shutdown()

    def _maybe_shutdown(self) -> list[Effect]:
        """Signal shutdown once an unhealthy or closing supervisor has drained.

        Returns
        -------
        list[Effect]
            Nothing while groups remain or the supervisor is open; otherwise
            what waits is refused as ``restarting`` (nothing ran for it, so its
            requester may retry), then a shutdown.
        """
        if self.admitting or self.groups:
            return []
        refused: list[Effect] = [Refuse(rid, "restarting") for rid in self.queue]
        self.queue.clear()
        self.cacheable.clear()
        return [*refused, Shutdown()]
