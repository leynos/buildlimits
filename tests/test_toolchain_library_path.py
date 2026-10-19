"""The adapter keeps a real toolchain compiler's library lookup out of rustlib.

Cargo gives a build script the toolchain's ``lib/rustlib/<triple>/lib``
directory on ``LD_LIBRARY_PATH`` (see ADR 002). When ``rustc-dev`` duplicated
the compiler driver there, the copy is found first and its ``$ORIGIN/../lib``
no longer reaches ``libLLVM``, so ``rustc --version`` dies with status 127 and
the build script sees empty output. These tests drive ``adapter._main`` against
fake toolchain trees and check the environment each launch path receives.
"""

import socket
import typing as typ

import pytest
from adapter_doubles import script_adapter
from toolchain_trees import ToolchainTree, make_shim, make_toolchain

from buildlimits import adapter
from buildlimits.protocol import (
    Admitted,
    CompileRequest,
    Exit,
    Finished,
    decode_request,
    encode_reply,
    recv_frame,
)

if typ.TYPE_CHECKING:
    from pathlib import Path

POISON_ENV = "LD_LIBRARY_PATH"


class _ExecedError(Exception):
    """Raised by a faked ``os.execvpe`` instead of replacing the process."""


@pytest.fixture
def supervisor_end(
    monkeypatch: pytest.MonkeyPatch,
    socket_pair: tuple[socket.socket, socket.socket],
) -> socket.socket:
    """Route the adapter to a fake connection and return the supervisor's end."""
    client, server = socket_pair
    script_adapter(monkeypatch, iter([client]), lambda exclude: [])
    monkeypatch.setattr(adapter, "in_admitted_scope", lambda env: False)
    server.sendall(encode_reply(Admitted(group="g1", cached=True, notice="")))
    server.sendall(encode_reply(Finished(exit=Exit(code=0))))
    return server


def _admitted_request(supervisor_end: socket.socket) -> CompileRequest:
    """Read the one request the adapter sent to the fake supervisor.

    Returns
    -------
    CompileRequest
        The decoded request, whose argv and environment the test inspects.
    """
    frame = recv_frame(supervisor_end)
    assert frame is not None, "the adapter must send a request"
    request = decode_request(frame[0])
    assert isinstance(request, CompileRequest), "a compile request"
    return request


@pytest.fixture
def tree(tmp_path: Path) -> ToolchainTree:
    """Build a toolchain whose rustlib directory duplicates the driver."""
    return make_toolchain(tmp_path)


def test_admitted_request_carries_the_toolchain_lib_before_a_poisoned_path(
    monkeypatch: pytest.MonkeyPatch, supervisor_end: socket.socket, tree: ToolchainTree
) -> None:
    """A rustlib-only path reaches the supervisor with the toolchain lib first."""
    monkeypatch.setenv(POISON_ENV, str(tree.rustlib))
    status = adapter._main([str(tree.rustc), "--version"], cacheable=True)
    assert status == 0, "the fake compiler finishes"
    request = _admitted_request(supervisor_end)
    assert request.argv == (str(tree.rustc), "--version"), "argv is unchanged"
    assert request.env[POISON_ENV] == f"{tree.lib}:{tree.rustlib}", (
        "the toolchain lib precedes the rustlib directory that holds the duplicate"
    )


def test_already_admitted_exec_gets_the_same_environment(
    monkeypatch: pytest.MonkeyPatch, tree: ToolchainTree
) -> None:
    """The direct-exec fast path hands the compiler the repaired environment."""
    monkeypatch.setenv(POISON_ENV, str(tree.rustlib))
    monkeypatch.setattr(adapter, "in_admitted_scope", lambda env: True)
    seen: list[tuple[str, tuple[str, ...], dict[str, str]]] = []

    def fake_execvpe(file: str, args: list[str], env: dict[str, str]) -> typ.NoReturn:
        """Record the exec and stop instead of replacing the process."""
        seen.append((file, tuple(args), env))
        raise _ExecedError

    monkeypatch.setattr(adapter.os, "execvpe", fake_execvpe, raising=False)
    with pytest.raises(_ExecedError):
        adapter._main([str(tree.rustc), "--version"], cacheable=True)
    assert seen[0][:2] == (str(tree.rustc), (str(tree.rustc), "--version")), (
        "the compiler and argv are untouched"
    )
    assert seen[0][2][POISON_ENV] == f"{tree.lib}:{tree.rustlib}", (
        "the exec environment carries the toolchain lib first"
    )


def test_a_rustup_shim_keeps_the_callers_path(
    monkeypatch: pytest.MonkeyPatch, supervisor_end: socket.socket, tmp_path: Path
) -> None:
    """The shim repairs its own lookup, so the adapter forwards the path verbatim."""
    shim = make_shim(tmp_path)
    monkeypatch.setenv(POISON_ENV, "/somewhere/rustlib")
    adapter._main([str(shim), "--version"], cacheable=True)
    assert _admitted_request(supervisor_end).env[POISON_ENV] == "/somewhere/rustlib", (
        "the proxy is not a toolchain binary and is left alone"
    )
