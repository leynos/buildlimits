"""Pure JSON payload and length validation for the compile-admission wire format.

Socket I/O, deadlines and descriptor ownership belong to framing. That module
re-exports this codec's established API for protocol consumers.
"""

import json
import struct
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 8 * 1024 * 1024
_LENGTH = struct.Struct(">I")


class ProtocolError(Exception):
    """A peer sent a frame or message that this protocol cannot accept."""


def encode_frame(payload: cabc.Mapping[str, object]) -> bytes:
    """Encode a JSON object as a versioned, length-prefixed frame.

    Parameters
    ----------
    payload : Mapping[str, object]
        The message body. ``version`` is added.

    Returns
    -------
    bytes
        The frame.

    Raises
    ------
    ProtocolError
        If the encoded body exceeds :data:`MAX_FRAME_BYTES`.
    """
    body = json.dumps({"version": PROTOCOL_VERSION, **payload}).encode()
    if len(body) > MAX_FRAME_BYTES:
        msg = f"frame of {len(body)} bytes exceeds the {MAX_FRAME_BYTES}-byte limit"
        raise ProtocolError(msg)
    return _LENGTH.pack(len(body)) + body


def decode_body(body: bytes) -> dict[str, object]:
    """Decode and version-check one frame body.

    Parameters
    ----------
    body : bytes
        The frame body, without its length prefix.

    Returns
    -------
    dict[str, object]
        The decoded message.

    Raises
    ------
    ProtocolError
        If the body is not a JSON object of this protocol version.
    """
    try:
        message = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        msg = "frame body is not JSON"
        raise ProtocolError(msg) from error
    if not isinstance(message, dict):
        msg = "frame body is not a JSON object"
        raise ProtocolError(msg)
    if message.get("version") != PROTOCOL_VERSION:
        msg = f"unsupported protocol version {message.get('version')!r}"
        raise ProtocolError(msg)
    return typ.cast("dict[str, object]", message)


def _frame_length(header: bytes) -> int:
    """Validate the declared frame size before reading its body.

    Returns
    -------
    int
        The accepted body length.

    Raises
    ------
    ProtocolError
        If the declared length exceeds the protocol ceiling.
    """
    (length,) = _LENGTH.unpack(header)
    if length > MAX_FRAME_BYTES:
        msg = f"frame of {length} bytes exceeds the {MAX_FRAME_BYTES}-byte limit"
        raise ProtocolError(msg)
    return length
