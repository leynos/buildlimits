"""Completed cached request metadata remains available while tokens drain."""

import json
import socket
import typing as typ

import pytest
from request_helpers import compile_request

from buildlimits.admission import GroupState
from buildlimits.config import SupervisorConfig
from buildlimits.protocol import Exit
from buildlimits.status_render import render
from buildlimits.supervisor import Supervisor


def test_completed_request_keeps_metadata_until_server_drain_finishes() -> None:
    """Plain and structured status identify retained work after socket cleanup."""
    supervisor = Supervisor(
        SupervisorConfig(), typ.cast("typ.Any", object()), clock=lambda: 15.0
    )
    supervisor.admission.cached = True
    try:
        client, server = socket.socketpair()
        with client, server:
            entry = supervisor.requests._admit(server, compile_request(()), [])
            assert entry is not None, "a real request owns its socket"
            supervisor.admission.submit(entry.request_id, cacheable=True)
            supervisor.admission.token_arrived(b"+")
            group = next(iter(supervisor.admission.groups.values()))
            group.state = GroupState.SERVER_DRAIN
            entry.group_id = group.group_id
            entry.admitted = 10.0
            supervisor.requests.finish(entry.request_id, Exit(code=0))
            assert not supervisor.requests.pending, (
                "completion closes requester ownership"
            )
            document = supervisor.status()
            encoded = json.loads(json.dumps(document))
            metadata = encoded["groups"][0]
            assert metadata["metadata_available"], "drain metadata survives completion"
            assert metadata["label"] == entry.request.label, (
                "the compiler stays identified"
            )
            assert metadata["directory"] == entry.request.cwd, (
                "its directory stays identified"
            )
            assert metadata["running_seconds"] == pytest.approx(5.0), (
                "admission time survives socket closure"
            )
            text = render(document)
            assert entry.request.label in text and entry.request.cwd in text, text
            assert "None in None, Nones" not in text, (
                "no missing-value placeholder leaks"
            )
            supervisor.requests.prune_completed({group.group_id})
            assert group.group_id in supervisor.requests.completed_metadata, (
                "held metadata remains"
            )
            supervisor.requests.prune_completed(set())
            assert not supervisor.requests.completed_metadata, (
                "finished groups release metadata"
            )
    finally:
        supervisor.requests.close_all()
        supervisor.selector.close()
