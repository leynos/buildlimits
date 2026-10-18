"""Exercise native journal serialization over real temporary datagram sockets."""

import socket
from pathlib import Path

import pytest
from journal_fixtures import journal_receiver

from buildlimits.journal_native import MAX_DATAGRAM_BYTES, JournalSink
from buildlimits.metrics import Metrics


def test_native_datagram_has_fixed_text_fields(tmp_path: Path) -> None:
    """The journal receives one plain ASCII datagram without binary fields."""
    path = tmp_path / "journal.sock"
    with journal_receiver(path) as receiver:
        sink = JournalSink(path, identifier="build-limits-supervisor")
        try:
            Metrics(sink).emit("request_queued", queued=2)
            payload = receiver.recv(MAX_DATAGRAM_BYTES)
            assert payload.decode("ascii").splitlines() == [
                "MESSAGE=build-limits metric request_queued",
                "PRIORITY=6",
                "SYSLOG_IDENTIFIER=build-limits-supervisor",
                "BUILD_LIMITS_SCHEMA=1",
                "BUILD_LIMITS_SOURCE=supervisor",
                "BUILD_LIMITS_EVENT=request_queued",
                "BUILD_LIMITS_QUEUED=2",
            ], "native field contract"
            assert sink._socket is not None and not sink._socket.getblocking(), (
                "nonblocking"
            )
        finally:
            sink.close()


@pytest.mark.parametrize(
    "value", ["line\nbreak", "snowman ☃", "null\0byte", "cr\rbyte"]
)
def test_invalid_native_values_are_rejected(tmp_path: Path, value: str) -> None:
    """Text-only records never enable newline injection or binary encoding."""
    with pytest.raises(OSError, match="field value"):
        JournalSink(tmp_path / "absent").emit({"BUILD_LIMITS_EVENT": value})


def test_large_datagram_is_dropped_without_memfd(tmp_path: Path) -> None:
    """Oversized text cannot allocate a memfd or send a partial record."""
    with pytest.raises(OSError, match="datagram limit"):
        JournalSink(tmp_path / "absent").emit({
            "BUILD_LIMITS_EVENT": "x" * MAX_DATAGRAM_BYTES
        })


def test_absent_journal_is_a_silent_null_sink(tmp_path: Path) -> None:
    """Minimal hosts can run the supervisor without any journal socket."""
    emitter = Metrics(JournalSink(tmp_path / "absent"))
    emitter.emit("supervisor_started")
    assert emitter.dropped_records == 0, "null sink succeeds silently"


def test_socket_error_reopens_on_next_attempt(tmp_path: Path) -> None:
    """A replaced journal socket is recovered without blocking retries."""
    path = tmp_path / "journal.sock"
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(str(path))
    sink = JournalSink(path)
    emitter = Metrics(sink)
    try:
        emitter.emit("supervisor_started")
        receiver.recv(MAX_DATAGRAM_BYTES)
        receiver.close()
        path.unlink()
        emitter.emit("request_queued")
        assert emitter.dropped_records == 1, "failed send counted"
        assert sink._socket is None, "failed descriptor released"
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as replacement:
            replacement.bind(str(path))
            replacement.settimeout(1)
            emitter.emit("request_queued")
            assert b"BUILD_LIMITS_EVENT=request_queued" in replacement.recv(
                MAX_DATAGRAM_BYTES
            ), "reopened"
    finally:
        receiver.close()
        sink.close()


@pytest.mark.parametrize(
    "name",
    [
        "MESSAGE",
        "PRIORITY",
        "SYSLOG_IDENTIFIER",
        "_PID",
        "build_limits_event",
        "BUILD_LIMITS_BAD=NAME",
        "BUILD_LIMITS_BAD\nNAME",
        "BUILD_LIMITS_BAD NAME",
        "BUILD_LIMITS_☃",
    ],
)
def test_invalid_native_names_are_rejected(tmp_path: Path, name: str) -> None:
    """Authored fields cannot impersonate journal metadata or inject separators."""
    with pytest.raises(OSError, match=r"^invalid native journal field name$"):
        JournalSink(tmp_path / "absent").emit({name: "valid"})


@pytest.mark.parametrize(
    "identifier", ["line\nbreak", "cr\rbyte", "null\0byte", "snowman ☃"]
)
def test_invalid_identifier_is_rejected(tmp_path: Path, identifier: str) -> None:
    """Generated identifier values obey the same text-only restrictions."""
    sink = JournalSink(tmp_path / "absent", identifier=identifier)
    with pytest.raises(OSError, match=r"^invalid native journal field value$"):
        sink.emit({"BUILD_LIMITS_EVENT": "supervisor_started"})


def test_invalid_name_precedes_value_and_size_errors(tmp_path: Path) -> None:
    """Name errors take precedence over invalid and oversized final values."""
    sink = JournalSink(tmp_path / "absent", identifier="invalid\nidentifier")
    with pytest.raises(OSError, match=r"^invalid native journal field name$"):
        sink.emit({"MESSAGE": "invalid\n" + "x" * MAX_DATAGRAM_BYTES})


@pytest.mark.parametrize("extra_bytes", [0, 1])
def test_datagram_size_boundary(tmp_path: Path, extra_bytes: int) -> None:
    """The exact byte budget sends whole records; one extra byte sends nothing."""
    path = tmp_path / "journal.sock"
    prefix = (
        b"MESSAGE=build-limits metric supervisor_started\n"
        b"PRIORITY=6\n"
        b"BUILD_LIMITS_EVENT=supervisor_started\n"
        b"BUILD_LIMITS_PADDING="
    )
    padding = "x" * (MAX_DATAGRAM_BYTES - len(prefix) - 1 + extra_bytes)
    expected = prefix + padding.encode("ascii") + b"\n"
    assert len(expected) == MAX_DATAGRAM_BYTES + extra_bytes, "complete wire size"
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind(str(path))
        receiver.settimeout(0.05)
        sink = JournalSink(path)
        record = {
            "BUILD_LIMITS_EVENT": "supervisor_started",
            "BUILD_LIMITS_PADDING": padding,
        }
        try:
            if extra_bytes:
                with pytest.raises(
                    OSError, match=r"^native journal metric exceeds datagram limit$"
                ):
                    sink.emit(record)
                with pytest.raises(TimeoutError):
                    receiver.recv(MAX_DATAGRAM_BYTES + 1)
            else:
                sink.emit(record)
                assert receiver.recv(MAX_DATAGRAM_BYTES + 1) == expected, (
                    "whole payload"
                )
        finally:
            sink.close()


def test_datagram_without_identifier_preserves_wire_order(tmp_path: Path) -> None:
    """Generated fields precede caller fields without an optional identifier."""
    path = tmp_path / "journal.sock"
    with journal_receiver(path) as receiver:
        sink = JournalSink(path)
        try:
            sink.emit({
                "BUILD_LIMITS_EVENT": "request_queued",
                "BUILD_LIMITS_QUEUED": "2",
            })
            assert receiver.recv(MAX_DATAGRAM_BYTES) == (
                b"MESSAGE=build-limits metric request_queued\n"
                b"PRIORITY=6\n"
                b"BUILD_LIMITS_EVENT=request_queued\n"
                b"BUILD_LIMITS_QUEUED=2\n"
            ), "exact ordered wire bytes"
        finally:
            sink.close()
