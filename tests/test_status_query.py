"""Tests for fetching, describing and printing the supervisor's status."""

import json
import socket
import typing as typ
from pathlib import Path

import pytest

from buildlimits import descriptor_budget, status, status_cli
from buildlimits.config import SupervisorConfig
from buildlimits.framing import encode_frame
from buildlimits.protocol import (
    Queued,
    StatusReport,
    StatusRequest,
    decode_request,
    encode_reply,
    recv_frame,
)
from buildlimits.status_client import StatusQueryError, query
from buildlimits.supervisor import Supervisor

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from syrupy.assertion import SnapshotAssertion

    from buildlimits.systemd import UserManager

ENV = {"BUILD_LIMITS_SOCKET": "/run/test/supervisor.sock"}
HEALTHY: dict[str, object] = {
    "capacity": 2,
    "held": 0,
    "healthy": True,
    "caching": True,
}


class FakeSupervisor:
    """The far end of a status query, answering from a script."""

    def __init__(self, answer: bytes | None) -> None:
        """Prepare a connected pair with ``answer`` already sent, if any."""
        self.client, self.server = socket.socketpair()
        self.paths: list[Path] = []
        if answer is not None:
            self.server.sendall(answer)
        self.server.shutdown(socket.SHUT_WR)

    def connect(self, path: Path, timeout: float) -> socket.socket:
        """Hand over the client end in place of a real connection."""
        self.paths.append(path)
        self.client.settimeout(timeout)
        return self.client

    def received(self) -> object:
        """Decode the request the query sent."""
        frame = recv_frame(self.server)
        assert frame is not None, "the query sent a request"
        return decode_request(frame[0])


@pytest.fixture
def supervisor_end() -> cabc.Iterator[cabc.Callable[[bytes | None], FakeSupervisor]]:
    """Create fake supervisors and close their sockets afterwards."""
    made: list[FakeSupervisor] = []

    def make(answer: bytes | None) -> FakeSupervisor:
        """Create one fake supervisor."""
        made.append(FakeSupervisor(answer))
        return made[-1]

    yield make
    for fake in made:
        fake.client.close()
        fake.server.close()


def test_a_status_report_is_returned(
    supervisor_end: cabc.Callable[[bytes | None], FakeSupervisor],
) -> None:
    """The query sends a status request to the configured socket."""
    fake = supervisor_end(encode_reply(StatusReport(HEALTHY)))
    assert query(env=ENV, connect=fake.connect) == HEALTHY, "the document returns"
    assert fake.paths == [Path(ENV["BUILD_LIMITS_SOCKET"])], "the socket is configured"
    assert fake.received() == StatusRequest(), "a status request was sent"


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (None, "closed the connection without replying"),
        (encode_reply(Queued(position=1)), "instead of its status"),
        (encode_frame({"type": "nonsense"}), "cannot query the supervisor"),
    ],
    ids=["no-reply", "wrong-reply", "malformed"],
)
def test_a_bad_answer_is_a_query_error(
    supervisor_end: cabc.Callable[[bytes | None], FakeSupervisor],
    answer: bytes | None,
    message: str,
) -> None:
    """Every unusable answer is reported as one error type."""
    fake = supervisor_end(answer)
    with pytest.raises(StatusQueryError, match=message):
        query(env=ENV, connect=fake.connect)


def test_an_unreachable_supervisor_is_a_query_error() -> None:
    """A connection failure names the socket and keeps its cause."""

    def refuse(path: Path, timeout: float) -> socket.socket:
        """Fail as connecting to a stopped supervisor would."""
        raise ConnectionRefusedError(111, "Connection refused", str(path))

    with pytest.raises(StatusQueryError, match=r"supervisor\.sock") as caught:
        query(env=ENV, connect=refuse)
    assert isinstance(caught.value.__cause__, ConnectionRefusedError), "cause kept"


def test_the_default_transport_reaches_a_real_socket(tmp_path: Path) -> None:
    """Without injection the query connects to the Unix socket it is given."""
    with pytest.raises(StatusQueryError, match="No such file"):
        query(env={"BUILD_LIMITS_SOCKET": str(tmp_path / "absent.sock")})


@pytest.mark.parametrize(
    "case",
    [
        ([], HEALTHY, "capacity 2, held 0, healthy", 0),
        ([], {**HEALTHY, "healthy": False}, "UNHEALTHY", 1),
        (["--json"], HEALTHY, '"capacity": 2', 0),
    ],
    ids=["healthy", "unhealthy", "json"],
)
def test_the_command_prints_and_reports_health(
    capsys: pytest.CaptureFixture[str],
    case: tuple[list[str], dict[str, object], str, int],
) -> None:
    """The exit status says whether the supervisor answered and is healthy."""
    argv, state, expected, code = case
    assert status_cli.main(argv, query=lambda: state) == code, "the exit status"
    assert expected in capsys.readouterr().out, "the document is printed"


def test_the_command_reports_an_unreachable_supervisor(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failed query prints the reason on standard error and exits 1."""

    def fail() -> dict[str, object]:
        """Fail as an unreachable supervisor would."""
        msg = "cannot query the supervisor at /x.sock: refused"
        raise StatusQueryError(msg)

    assert status_cli.main([], query=fail) == 1, "the command fails"
    captured = capsys.readouterr()
    assert not captured.out, "nothing is printed as a status"
    assert captured.err == (
        "build-limits-status: cannot query the supervisor at /x.sock: refused\n"
    ), "the reason is named"


def test_an_idle_supervisor_describes_itself(
    snapshot: SnapshotAssertion, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The document reports configuration and an empty queue."""
    # Model diagnostic inputs without changing the runner's resource limits.
    monkeypatch.setattr(
        descriptor_budget.resource, "getrlimit", lambda _kind: (64, 128)
    )
    monkeypatch.setattr(descriptor_budget, "open_count", lambda: 12)
    monkeypatch.setattr(
        status.descriptors, "open_descriptors_except", lambda _: list(range(12))
    )
    supervisor = Supervisor(
        SupervisorConfig(slots=3), typ.cast("UserManager", object()), clock=lambda: 0.0
    )
    supervisor.requests.descriptors.configure(slots=3)
    with supervisor.selector:
        document = status.describe(supervisor)
        assert json.loads(json.dumps(document)) == document, "the document is JSON"
        assert document["device"] == str(supervisor.settings.device), "device reported"
        assert (document["capacity"], document["held"]) == (3, 0), "nothing is held"
        assert not document["queued"], "no request waits"
        assert not document["groups"], "no group runs"
        assert document["health_reason"] is None, (
            "an idle healthy instance has no fault"
        )
        assert document["drain_fault"] is None, "no cached drain check has failed"
        assert document["drain_fault_kind"] is None, "no drain fault is classified"
        assert document["drain_attempts"] == 0, "no recovery retry was needed"
        assert document["recovery_action"] is None, "an operator action is unnecessary"
        descriptors = document["descriptors"]
        assert isinstance(descriptors, dict), "descriptor status is a structured object"
        assert descriptors["soft"] == 64, "effective soft limit is reported"
        assert descriptors["hard"] == 128, "reachable hard limit is reported"
        assert descriptors["soft_limit"] == descriptors["soft"], "soft aliases agree"
        assert descriptors["hard_limit"] == descriptors["hard"], "hard aliases agree"
        assert descriptors["sampling_errno"] is None, "successful sampling has no errno"
        assert descriptors["open"] == 12, "sampling reports the process baseline"
        assert descriptors["retained"] == 0, "an idle supervisor owns no queued rights"
        assert descriptors["high_water"] == 0, "no request retention was observed"
        assert descriptors["budget"] == 30, (
            "control and launch reserves remain available"
        )
        # The device path depends on configuration defaults, not on this contract.
        assert {**document, "device": "<device>"} == snapshot, "the document changed"
