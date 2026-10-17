"""Requester descriptor ownership across recording and reply failures.

Disposable experiments use real socket connections and duplicate rights, with
owned-descriptor baselines checked after cleanup. Run from the repository root
with ``uv run pytest -v
tests/test_request_ownership.py``.
"""

import errno
import os
import selectors
import socket

import pytest
from descriptor_helpers import isolated, open_fds, still_open
from request_helpers import compile_request

from buildlimits.framing import MAX_FRAME_BYTES, ProtocolError
from buildlimits.protocol import Queued, StatusReport
from buildlimits.requesters import Requests


def _release(
    requests: Requests, received: int, left: socket.socket, right: socket.socket
) -> None:
    """Forget every recorded request, then close whatever the test still owns."""
    for request_id in list(requests.pending):
        requests.forget(request_id)
    if still_open([received]):
        os.close(received)
    left.close()
    right.close()


def _record_failure(report: int) -> object:
    """Refuse selector registration after receipt but before request ownership."""
    del report
    with selectors.DefaultSelector() as selector:
        requests = Requests(selector, lambda: 0.0)
        baseline = open_fds()
        with pytest.MonkeyPatch.context() as patch:
            left, right = socket.socketpair()
            received = os.dup(right.fileno())

            def refuse(*_args: object) -> None:
                """Fail registration without registering any unrelated handle."""
                raise OSError(errno.EIO, "injected selector refusal")

            patch.setattr(selector, "register", refuse)
            caught = None
            try:
                try:
                    requests._admit(right, compile_request((0,)), [received])
                except OSError as error:
                    caught = error.errno
                result = {
                    "errno": caught,
                    "pending": len(requests.pending),
                    "received_closed": not still_open([received]),
                    "conn_closed": right.fileno() == -1,
                }
            finally:
                _release(requests, received, left, right)
        result["baseline"] = open_fds() == baseline
        return result


def test_registration_failure_closes_unrecorded_ownership() -> None:
    """A failed selector registration must not leave a pending request or rights."""
    result = isolated(_record_failure)
    assert isinstance(result, dict), "the child reports ownership evidence"
    assert result["errno"] is None and result["pending"] == 0, (
        "recording failure is controlled and leaves no pending entry"
    )
    assert result["received_closed"] and result["conn_closed"], (
        "refusal closes rights and connection"
    )
    assert result["baseline"], "all owned descriptors return to baseline"


def _reply_failure(report: int, *, oversized: bool) -> object:
    """Make a recorded reply fail through peer closure or oversized framing."""
    del report
    with selectors.DefaultSelector() as selector:
        requests = Requests(selector, lambda: 0.0)
        baseline = open_fds()
        left, right = socket.socketpair()
        received = os.dup(right.fileno())
        try:
            entry = requests._admit(right, compile_request((0,)), [received])
            assert entry is not None, "the request is recorded before its reply fails"
            if not oversized:
                left.close()
            reply = (
                StatusReport({"probe": "x" * (2 * MAX_FRAME_BYTES)})
                if oversized
                else Queued(1)
            )
            caught = None
            try:
                requests.notify(entry.request_id, reply)
            except ProtocolError:
                caught = "ProtocolError"
            result = {
                "error": caught,
                "conn_closed": entry.conn is None,
                "cancel_ids": sorted(getattr(requests, "failed_replies", set())),
            }
        finally:
            _release(requests, received, left, right)
        result["baseline"] = open_fds() == baseline
        return result


@pytest.mark.parametrize("oversized", [False, True])
def test_failed_replies_schedule_cancellation_and_release_the_connection(
    oversized: bool,
) -> None:
    """Reply failures must follow the disconnect cancellation path."""
    result = isolated(lambda report: _reply_failure(report, oversized=oversized))
    assert isinstance(result, dict), "the child reports reply-failure evidence"
    assert result["error"] is None and result["conn_closed"], (
        "failed replies close their connection without escaping"
    )
    assert result["cancel_ids"] == [1], (
        "failed reply schedules the recorded admission cancellation"
    )
    assert result["baseline"], "cancellation cleanup recovers descriptor ownership"
