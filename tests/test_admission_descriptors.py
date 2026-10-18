"""Supervisor descriptor-target boundaries and rejected-request ownership.

Run with ``uv run pytest -v
tests/test_admission_descriptors.py`` from the repository root.
"""

import os
import resource
import selectors
import socket

import pytest
from descriptor_helpers import open_fds
from request_helpers import compile_request, refusal_from

from buildlimits.requesters import Requests


@pytest.mark.parametrize("target", [63, 64, 127])
def test_admission_respects_the_supervisors_reachable_hard_limit(
    monkeypatch: pytest.MonkeyPatch, target: int
) -> None:
    """A reachable target is recorded; unreachable targets close all ownership."""
    baseline = open_fds()
    with selectors.DefaultSelector() as selector:
        server, client = socket.socketpair()
        source = os.open("/dev/null", os.O_RDWR)
        requests = Requests(selector, clock=lambda: 0.0)
        monkeypatch.setattr(resource, "getrlimit", lambda _kind: (64, 64))
        try:
            entry = requests._admit(server, compile_request((target,)), [source])
            if target < 64:
                assert entry is not None, "target 63 fits under hard64"
                requests.forget(entry.request_id)
            else:
                assert entry is None, "target at or above hard is unreachable"
                reply = refusal_from(client)
                assert reply.reason == "descriptors", (
                    "the refusal names descriptor reachability"
                )
                assert "hard limit 64" in reply.message, (
                    "the effective ceiling is diagnosable"
                )
            assert not requests.pending, "neither path leaves a recorded request"
            with pytest.raises(OSError, match=r"\[Errno 9\]"):
                os.fstat(source)
        finally:
            for request_id in list(requests.pending):
                requests.forget(request_id)
            server.close()
            client.close()
    assert open_fds() == baseline, "both recorded and refused paths recover baseline"
