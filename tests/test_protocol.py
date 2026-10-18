"""Tests for the adapter-supervisor wire protocol."""

import json
import os
import socket
import struct

import pytest
from hypothesis import given
from hypothesis import strategies as st

from buildlimits.protocol import (
    PROTOCOL_VERSION,
    Admitted,
    CompileRequest,
    Exit,
    Finished,
    ProcessAttributes,
    ProtocolError,
    Queued,
    RecoverReport,
    RecoverRequest,
    Refused,
    Reply,
    StatusReport,
    StatusRequest,
    decode_reply,
    decode_request,
    encode_frame,
    encode_reply,
    encode_request,
    recv_frame,
    send_frame,
)

# Surrogate escapes stand in for undecodable bytes in arguments and the
# environment, which Python represents that way.
_text = st.text(st.characters(codec="utf-8") | st.sampled_from(["\udcff", "\udc80"]))
_limits = st.tuples(st.integers(-1, 2**40), st.integers(-1, 2**40))

compile_requests = st.builds(
    CompileRequest,
    argv=st.lists(_text, min_size=1, max_size=6).map(tuple),
    cwd=_text,
    env=st.dictionaries(_text, _text, max_size=5),
    attributes=st.builds(
        ProcessAttributes,
        umask=st.integers(0, 0o777),
        nice=st.integers(-20, 19),
        rlimits=st.dictionaries(
            st.sampled_from(["RLIMIT_NOFILE", "RLIMIT_CORE"]), _limits
        ),
    ),
    fd_targets=st.lists(st.integers(0, 64), unique=True, max_size=6).map(tuple),
    label=_text,
    cacheable=st.booleans(),
)

replies = st.one_of(
    st.builds(Queued, position=st.integers(0, 1000)),
    st.builds(Admitted, group=_text, cached=st.booleans(), notice=_text),
    st.builds(
        Finished,
        exit=st.one_of(
            st.builds(Exit, code=st.integers(0, 255)),
            st.builds(Exit, signal=st.integers(1, 64)),
            st.just(Exit()),
        ),
    ),
    st.builds(Refused, reason=_text, message=_text),
    st.builds(StatusReport, state=st.dictionaries(_text, st.integers(), max_size=3)),
)


@given(compile_requests)
def test_compile_requests_round_trip(request: CompileRequest) -> None:
    """Every compile request decodes to itself, surrogates included."""
    assert decode_request(encode_request(request)[4:]) == request, (
        "a compile request round-trips through encode and decode"
    )


@given(replies)
def test_replies_round_trip(reply: Reply) -> None:
    """Every reply decodes to itself."""
    assert decode_reply(encode_reply(reply)[4:]) == reply, (
        "a reply round-trips through encode and decode"
    )


def test_status_request_round_trips() -> None:
    """A status request carries no payload."""
    assert decode_request(encode_request(StatusRequest())[4:]) == StatusRequest(), (
        "a status request round-trips with no payload"
    )


def test_recover_request_round_trips() -> None:
    """A recovery request carries no payload and never decodes as a compile."""
    assert decode_request(encode_request(RecoverRequest())[4:]) == RecoverRequest(), (
        "a recovery request round-trips with no payload"
    )


@given(st.dictionaries(_text, st.integers(), max_size=4))
def test_recover_reports_round_trip(state: dict[str, object]) -> None:
    """A recovery report carries the transaction's own document unchanged."""
    reply = RecoverReport(state=state)
    assert decode_reply(encode_reply(reply)[4:]) == reply, (
        "a recovery report round-trips with its state"
    )


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        (b"not json", "not JSON"),
        (b"[1, 2]", "not a JSON object"),
        (json.dumps({"version": 99, "type": "status"}).encode(), "unsupported"),
        (json.dumps({"version": PROTOCOL_VERSION, "type": "x"}).encode(), "unknown"),
        (
            json.dumps({"version": PROTOCOL_VERSION, "type": "compile"}).encode(),
            "lacks",
        ),
    ],
)
def test_malformed_requests_are_rejected(body: bytes, fragment: str) -> None:
    """Malformed request bodies raise a protocol error naming the problem."""
    with pytest.raises(ProtocolError, match=fragment):
        decode_request(body)


def _compile_payload(**overrides: object) -> bytes:
    """Build a compile request body with some fields replaced."""
    payload: dict[str, object] = {
        "version": PROTOCOL_VERSION,
        "type": "compile",
        "argv": ["rustc"],
        "cwd": "/",
        "env": {},
        "umask": 0o22,
        "nice": 0,
        "rlimits": {},
        "fd_targets": [0, 1, 2],
        "label": "x",
        "cacheable": True,
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


@pytest.mark.parametrize(
    "overrides",
    [
        {"argv": []},
        {"argv": "rustc"},
        {"fd_targets": [1, 1]},
        {"fd_targets": [-1]},
        {"umask": True},
        {"rlimits": {"RLIMIT_NOFILE": [1]}},
        {"cacheable": "yes"},
    ],
)
def test_invalid_compile_fields_are_rejected(overrides: dict[str, object]) -> None:
    """Wrongly typed or inconsistent fields are rejected, not coerced."""
    with pytest.raises(ProtocolError):
        decode_request(_compile_payload(**overrides))


def test_oversized_frames_are_refused() -> None:
    """Neither side encodes nor accepts a frame past the size limit."""
    with pytest.raises(ProtocolError, match="exceeds"):
        encode_frame({"blob": "x" * (9 * 1024 * 1024)})
    left, right = socket.socketpair()
    with left, right:
        left.sendall(struct.pack(">I", 9 * 1024 * 1024))
        with pytest.raises(ProtocolError, match="exceeds"):
            recv_frame(right)


def test_frames_carry_descriptors() -> None:
    """Descriptors sent with a frame arrive with it and refer to the same file."""
    left, right = socket.socketpair()
    read_end, write_end = os.pipe()
    with left, right:
        send_frame(left, encode_request(StatusRequest()), [write_end])
        received = recv_frame(right)
        assert received is not None, "the frame with descriptors is received"
        body, fds = received
        assert decode_request(body) == StatusRequest(), "the frame's body decodes"
        assert len(fds) == 1, "exactly one descriptor arrives with the frame"
        os.write(fds[0], b"through")
        assert os.read(read_end, 16) == b"through", (
            "the descriptors refer to the same file"
        )
        for fd in (*fds, read_end, write_end):
            os.close(fd)


def test_end_of_stream_between_frames_is_not_an_error() -> None:
    """A peer closing between frames yields ``None``."""
    left, right = socket.socketpair()
    with right:
        left.close()
        assert recv_frame(right) is None, "a peer closing between frames yields None"


def test_end_of_stream_inside_a_frame_is_an_error() -> None:
    """A peer closing inside a frame is a protocol error."""
    left, right = socket.socketpair()
    with right:
        left.sendall(struct.pack(">I", 10) + b"abc")
        left.close()
        with pytest.raises(ProtocolError, match="inside a frame"):
            recv_frame(right)
