"""The effects the admission core asks its caller to carry out.

Each :class:`~buildlimits.admission.Admission` event method returns a list of
these values, in the order the caller must perform them. They are plain data:
the supervisor maps each type to the operation that performs it. The module
imports nothing of the wire protocol, which re-exports :class:`Exit` for its
replies, so the admission core stays independent of the transport.
"""

import dataclasses as dc

type RequestId = int
type GroupId = str


@dc.dataclass(frozen=True, slots=True)
class Exit:
    """How an admitted compiler ended.

    Attributes
    ----------
    code : int | None
        The exit status, when the compiler exited normally.
    signal : int | None
        The terminating signal, when the compiler was killed.
    """

    code: int | None = None
    signal: int | None = None


@dc.dataclass(frozen=True, slots=True)
class RequestToken:
    """Ask the pool for one token. At most one request is outstanding."""


@dc.dataclass(frozen=True, slots=True)
class ReturnToken:
    """Write a token back to the pool.

    Attributes
    ----------
    token : bytes
        The token to return.
    """

    token: bytes


@dc.dataclass(frozen=True, slots=True)
class Launch:
    """Start the request's compiler in a new group holding ``token``.

    Attributes
    ----------
    request_id : RequestId
        The request to run.
    group_id : GroupId
        The new group's identifier.
    cached : bool
        Whether to run the compiler through the configured cache backend.
    """

    request_id: RequestId
    group_id: GroupId
    cached: bool


@dc.dataclass(frozen=True, slots=True)
class NotifyQueued:
    """Tell a requester how many requests are ahead of it.

    Attributes
    ----------
    request_id : RequestId
        The waiting request.
    position : int
        Requests ahead of it.
    """

    request_id: RequestId
    position: int


@dc.dataclass(frozen=True, slots=True)
class NotifyFinished:
    """Tell a requester how its compiler ended.

    Attributes
    ----------
    request_id : RequestId
        The request whose group has emptied.
    exit : Exit
        How the group's leading process ended.
    """

    request_id: RequestId
    exit: Exit


@dc.dataclass(frozen=True, slots=True)
class Refuse:
    """Tell a requester that its request will not run.

    Attributes
    ----------
    request_id : RequestId
        The refused request.
    reason : str
        A stable reason code.
    """

    request_id: RequestId
    reason: str


@dc.dataclass(frozen=True, slots=True)
class Terminate:
    """Terminate every process in a group; the caller escalates to KILL.

    Attributes
    ----------
    group_id : GroupId
        The group to terminate.
    """

    group_id: GroupId


@dc.dataclass(frozen=True, slots=True)
class Shutdown:
    """Every group has drained after a health failure; exit to restart."""


type Effect = (
    RequestToken
    | ReturnToken
    | Launch
    | NotifyQueued
    | NotifyFinished
    | Refuse
    | Terminate
    | Shutdown
)
