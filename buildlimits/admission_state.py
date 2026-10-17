"""Frozen gauges and compiler-group lifecycle values for admission.

Owned by the admission core; snapshots contain only domain state.
"""

import dataclasses as dc
import enum
import typing as typ

if typ.TYPE_CHECKING:
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
    """

    queued: int
    held: int
    running: int
    draining: int
    server_drain: int
    capacity: int


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
