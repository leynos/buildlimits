"""Frozen gauges and compiler-group lifecycle values for admission.

Owned by the admission core; snapshots contain only domain state.
"""

import collections
import dataclasses as dc
import enum
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.effects import Exit, GroupId, RequestId


class GroupState(enum.Enum):
    """The lifecycle of an admitted compiler group."""

    RUNNING = "running"
    DRAINING = "draining"
    SERVER_DRAIN = "server-drain"


@dc.dataclass(frozen=True, slots=True)
class Counts:
    """Identifier-free snapshot of the admission state.

    Attributes
    ----------
    queued : int
        Requests still waiting for a token.
    held : int
        Tokens held across every group state.
    running : int
        Groups whose leaders have not ended.
    draining : int
        Groups waiting for their scopes to empty.
    server_drain : int
        Empty groups retaining a token for server work.
    capacity : int
        Configured capacity of the shared pool.
    cached_moving : int
        Running or draining groups composed with sccache. Only these can be
        interrupted by stopping the managed cache: a group composed without it
        never talks to the server, so recovery must not wait for it.
    """

    queued: int
    held: int
    running: int
    draining: int
    server_drain: int
    capacity: int
    cached_moving: int


@dc.dataclass(slots=True)
class Group:
    """One admitted compiler group and the token it holds.

    Attributes
    ----------
    group_id : GroupId
        The group's identifier and transient scope name.
    request_id : RequestId
        The request that the group serves.
    token : bytes
        The pool token the group holds.
    cached : bool
        Whether the group runs through the managed sccache server.
    state : GroupState
        Where the group is in its lifecycle.
    cancelled : bool
        Whether the requester cancelled the group.
    exit : Exit | None
        How the group's leading process ended, once known.
    """

    group_id: GroupId
    request_id: RequestId
    token: bytes
    cached: bool
    state: GroupState = GroupState.RUNNING
    cancelled: bool = False
    exit: Exit | None = None

    @property
    def may_orphan_server_work(self) -> bool:
        """Whether this group's ending may have left work in the sccache server.

        A cancelled group or one whose exit was never established is always
        suspect, because nothing observed how it ended; a known ending is
        suspect only for a signal, which the leader may not have passed on to
        the server before it died. A clean exit is the one ending that proves
        no such work remains.

        Returns
        -------
        bool
            Whether the group's token requires server-work accounting.
        """
        if self.cancelled or self.exit is None:
            return True
        return self.exit.signal is not None


def snapshot(groups: cabc.Iterable[Group], queued: int, capacity: int) -> Counts:
    """Derive the gauges from the groups, without exposing any identifier.

    Parameters
    ----------
    groups : Iterable[Group]
        Every group holding a token.
    queued : int
        Requests still waiting for a token.
    capacity : int
        Configured capacity of the shared pool.

    Returns
    -------
    Counts
        A frozen snapshot of the queue and the group states.

    Examples
    --------
    >>> snapshot([], 2, 4).queued
    2
    """
    held = list(groups)
    states = collections.Counter(group.state for group in held)
    cached = collections.Counter(group.state for group in held if group.cached)
    return Counts(
        queued,
        len(held),
        states[GroupState.RUNNING],
        states[GroupState.DRAINING],
        states[GroupState.SERVER_DRAIN],
        capacity,
        cached[GroupState.RUNNING] + cached[GroupState.DRAINING],
    )


class Gauges:
    """Read-only views of the admission state, shared by the admission core.

    The accessors read only the queue, the groups and the health flags, which
    keeps them beside the snapshot they build and out of the decision logic.
    """

    __slots__ = ()

    healthy: bool
    closing: bool
    capacity: int
    queue: collections.deque[RequestId]
    groups: dict[GroupId, Group]

    @property
    def admitting(self) -> bool:
        """Whether new groups may launch: healthy and not closing.

        Returns
        -------
        bool
            True while the supervisor is open for work.
        """
        return self.healthy and not self.closing

    @property
    def winding_down(self) -> bool:
        """Whether a healthy supervisor is closing for a graceful stop.

        Returns
        -------
        bool
            True from the graceful stop until the supervisor exits.
        """
        return self.healthy and self.closing

    def counts(self) -> Counts:
        """Read gauges without exposing request or group identifiers.

        Returns
        -------
        Counts
            A frozen snapshot derived from the queue and group states.
        """
        return snapshot(self.groups.values(), len(self.queue), self.capacity)

    def cached_tokens_held(self) -> int:
        """Count tokens held by groups composed with sccache.

        Returns
        -------
        int
            The number of cached groups holding a token.
        """
        return sum(1 for group in self.groups.values() if group.cached)

    def tokens_held(self) -> int:
        """Count tokens held by all groups.

        Returns
        -------
        int
            The number of groups holding a token.
        """
        return len(self.groups)
