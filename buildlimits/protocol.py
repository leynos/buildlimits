"""Wire protocol between the compiler adapters and the admission supervisor.

Messages travel over a Unix stream socket as frames: a four-byte big-endian
length followed by that many bytes of UTF-8 JSON. A compile request carries
the caller's inheritable file descriptors as ``SCM_RIGHTS`` ancillary data on
the same ``sendmsg`` call as its frame, so the supervisor receives the
descriptors together with the request that names their target numbers.

Every message carries the protocol ``version``. A peer that receives another
version rejects the message rather than guessing at its meaning.
"""

import dataclasses as dc

from buildlimits import wire
from buildlimits.attributes import ProcessAttributes
from buildlimits.framing import (
    MAX_FDS,
    MAX_FRAME_BYTES,
    PROTOCOL_VERSION,
    ProtocolError,
    decode_body,
    encode_frame,
    recv_frame,
    send_frame,
)
from buildlimits.replies import (
    Admitted,
    Exit,
    Finished,
    Queued,
    RecoverReport,
    Refused,
    Reply,
    StatusReport,
    decode_reply,
    encode_reply,
)

__all__ = [
    "MAX_FDS",
    "MAX_FRAME_BYTES",
    "PROTOCOL_VERSION",
    "Admitted",
    "CompileRequest",
    "Exit",
    "Finished",
    "ProcessAttributes",
    "ProtocolError",
    "Queued",
    "RecoverReport",
    "RecoverRequest",
    "Refused",
    "Reply",
    "Request",
    "StatusReport",
    "StatusRequest",
    "decode_body",
    "decode_reply",
    "decode_request",
    "encode_frame",
    "encode_reply",
    "encode_request",
    "recv_frame",
    "send_frame",
]


@dc.dataclass(frozen=True, slots=True)
class CompileRequest:
    """A request to run one compiler invocation under admission.

    Attributes
    ----------
    argv : tuple[str, ...]
        The compiler argument vector, executable first. It is never evaluated
        as shell text.
    cwd : str
        The caller's working directory.
    env : dict[str, str]
        The caller's complete environment.
    attributes : ProcessAttributes
        The caller's umask, niceness and resource limits.
    fd_targets : tuple[int, ...]
        The descriptor number each passed descriptor must occupy in the
        compiler, in the order the descriptors were sent.
    label : str
        A short human label, normally the crate name, for diagnostics.
    cacheable : bool
        Whether the invocation may run through the configured cache backend.
        Rustdoc invocations are not cacheable.
    """

    argv: tuple[str, ...]
    cwd: str
    env: dict[str, str]
    attributes: ProcessAttributes
    fd_targets: tuple[int, ...]
    label: str
    cacheable: bool = True


@dc.dataclass(frozen=True, slots=True)
class StatusRequest:
    """A request for the supervisor's current state."""


@dc.dataclass(frozen=True, slots=True)
class RecoverRequest:
    """A request to recover the managed cache's retained work.

    The request asks the supervisor to stop the managed sccache service under
    the restart barrier and verify it is stopped and empty. It is answered as
    soon as the transaction is open; the requester learns the outcome from the
    status document or from the tokens returning.
    """


type Request = CompileRequest | StatusRequest | RecoverRequest


def encode_request(request: Request) -> bytes:
    """Encode a request as one frame.

    Parameters
    ----------
    request : Request
        The request to encode.

    Returns
    -------
    bytes
        The length-prefixed frame.

    Examples
    --------
    >>> decode_request(encode_request(StatusRequest())[4:])
    StatusRequest()
    """
    # Exhaustive over ``Request``: the compile branch is the fallthrough, so a
    # future request type fails here rather than encoding as a compile request.
    match request:
        case StatusRequest():
            payload: dict[str, object] = {"type": "status"}
        case RecoverRequest():
            payload = {"type": "recover"}
        case CompileRequest():
            payload = {
                "type": "compile",
                "argv": list(request.argv),
                "cwd": request.cwd,
                "env": request.env,
                "umask": request.attributes.umask,
                "nice": request.attributes.nice,
                "rlimits": {k: list(v) for k, v in request.attributes.rlimits.items()},
                "fd_targets": list(request.fd_targets),
                "label": request.label,
                "cacheable": request.cacheable,
            }
    return encode_frame(payload)


def decode_request(body: bytes) -> Request:
    """Decode a request frame body.

    Parameters
    ----------
    body : bytes
        The frame body, without its length prefix.

    Returns
    -------
    Request
        The decoded request.

    Raises
    ------
    ProtocolError
        If the message is not a well-formed request.
    """
    message = decode_body(body)
    kind = message.get("type")
    if kind == "status":
        return StatusRequest()
    if kind == "recover":
        return RecoverRequest()
    if kind != "compile":
        msg = f"unknown request type {kind!r}"
        raise ProtocolError(msg)
    return _checked(_compile_request(message))


def _compile_request(message: dict[str, object]) -> CompileRequest:
    """Build a compile request from its decoded fields.

    Returns
    -------
    CompileRequest
        The request, not yet checked for consistency.

    Raises
    ------
    ProtocolError
        If a field is missing or has the wrong type.
    """
    try:
        return CompileRequest(
            argv=wire.as_str_tuple(message["argv"]),
            cwd=wire.as_str(message["cwd"]),
            env=wire.as_str_dict(message["env"]),
            attributes=ProcessAttributes(
                umask=wire.as_int(message["umask"]),
                nice=wire.as_int(message["nice"]),
                rlimits={
                    wire.as_str(name): wire.as_limit_pair(pair)
                    for name, pair in wire.as_dict(message["rlimits"]).items()
                },
            ),
            fd_targets=tuple(
                wire.as_int(value) for value in wire.as_list(message["fd_targets"])
            ),
            label=wire.as_str(message["label"]),
            cacheable=wire.as_bool(message["cacheable"]),
        )
    except KeyError as error:
        msg = f"compile request lacks {error.args[0]!r}"
        raise ProtocolError(msg) from error


def _checked(request: CompileRequest) -> CompileRequest:
    """Check a compile request's fields against each other.

    Returns
    -------
    CompileRequest
        The same request.

    Raises
    ------
    ProtocolError
        If the argument vector is empty or the descriptor targets repeat or
        are negative.
    """
    if not request.argv:
        msg = "compile request has an empty argument vector"
        raise ProtocolError(msg)
    targets = request.fd_targets
    if len(set(targets)) != len(targets) or min(targets, default=0) < 0:
        msg = "descriptor targets must be distinct and non-negative"
        raise ProtocolError(msg)
    return request
