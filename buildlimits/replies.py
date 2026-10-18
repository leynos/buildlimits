"""Supervisor replies: how a request progresses and how its compiler ended.

The adapter reads these frames on its connection until it receives a
``Finished`` or ``Refused`` reply; a status request gets one ``StatusReport``.
:mod:`buildlimits.protocol` re-exports every name here.
"""

import dataclasses as dc

from buildlimits import wire
from buildlimits.effects import Exit
from buildlimits.framing import ProtocolError, decode_body, encode_frame


@dc.dataclass(frozen=True, slots=True)
class Queued:
    """The request is waiting for a slot.

    Attributes
    ----------
    position : int
        Requests ahead of this one, counted from zero.
    """

    position: int


@dc.dataclass(frozen=True, slots=True)
class Admitted:
    """The request holds a slot and its compiler is starting.

    Attributes
    ----------
    group : str
        The compiler group's identifier, also its transient scope name.
    cached : bool
        Whether the group runs through the managed sccache server.
    notice : str
        A line for the build's standard error, such as why the group is not
        cached; empty when there is nothing to say.
    """

    group: str
    cached: bool
    notice: str = ""


@dc.dataclass(frozen=True, slots=True)
class Finished:
    """The compiler group ended and its slot is accounted for.

    Attributes
    ----------
    exit : Exit
        How the group's leading process ended.
    """

    exit: Exit


@dc.dataclass(frozen=True, slots=True)
class Refused:
    """The supervisor will not run the request.

    Attributes
    ----------
    reason : str
        A stable, machine-readable reason code.
    message : str
        A human-readable explanation.
    """

    reason: str
    message: str


@dc.dataclass(frozen=True, slots=True)
class StatusReport:
    """A snapshot of the supervisor's state.

    Attributes
    ----------
    state : dict[str, object]
        The JSON-compatible status document.
    """

    state: dict[str, object]


@dc.dataclass(frozen=True, slots=True)
class RecoverReport:
    """How the managed cache's recovery transaction stands.

    Attributes
    ----------
    state : dict[str, object]
        The transaction's phase, the consecutive failures of its last action,
        the last fault text, the seconds until that action is retried, and why
        the transaction could not start.
    """

    state: dict[str, object]


type Reply = Queued | Admitted | Finished | Refused | StatusReport | RecoverReport


def encode_reply(reply: Reply) -> bytes:
    """Encode a supervisor reply as one frame.

    Parameters
    ----------
    reply : Reply
        The reply to encode.

    Returns
    -------
    bytes
        The length-prefixed frame.

    Examples
    --------
    >>> decode_reply(encode_reply(Queued(position=2))[4:])
    Queued(position=2)
    """
    match reply:
        case Queued(position=position):
            payload: dict[str, object] = {"type": "queued", "position": position}
        case Admitted(group=group, cached=cached, notice=notice):
            payload = {
                "type": "admitted",
                "group": group,
                "cached": cached,
                "notice": notice,
            }
        case Finished(exit=exit_):
            payload = {"type": "finished", "code": exit_.code, "signal": exit_.signal}
        case Refused(reason=reason, message=message):
            payload = {"type": "refused", "reason": reason, "message": message}
        case StatusReport(state=state):
            payload = {"type": "status", "state": state}
        case RecoverReport(state=state):
            payload = {"type": "recovering", "state": state}
    return encode_frame(payload)


def decode_reply(body: bytes) -> Reply:
    """Decode a supervisor reply frame body.

    Parameters
    ----------
    body : bytes
        The frame body, without its length prefix.

    Returns
    -------
    Reply
        The decoded reply.

    Raises
    ------
    ProtocolError
        If the message is not a well-formed reply.
    """
    message = decode_body(body)
    try:
        match message.get("type"):
            case "queued":
                return Queued(position=wire.as_int(message["position"]))
            case "admitted":
                return Admitted(
                    group=wire.as_str(message["group"]),
                    cached=wire.as_bool(message["cached"]),
                    notice=wire.as_str(message["notice"]),
                )
            case "finished":
                return Finished(
                    exit=Exit(
                        code=wire.as_optional_int(message["code"]),
                        signal=wire.as_optional_int(message["signal"]),
                    )
                )
            case "refused":
                return Refused(
                    reason=wire.as_str(message["reason"]),
                    message=wire.as_str(message["message"]),
                )
            case "status":
                return StatusReport(state=wire.as_dict(message["state"]))
            case "recovering":
                return RecoverReport(state=wire.as_dict(message["state"]))
            case other:
                msg = f"unknown reply type {other!r}"
                raise ProtocolError(msg)
    except KeyError as error:
        msg = f"reply lacks {error.args[0]!r}"
        raise ProtocolError(msg) from error
