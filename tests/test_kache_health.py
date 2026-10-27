"""What kache mode means by "the client works", and who it asks.

Health has three parts, and the first is the one that can lie. The configured
client may be an absolute path or a bare name; a bare name is a search, and a
search has an environment. If health searches the supervisor's ``PATH`` while
composition searches the requesting build's, the answer and the action describe
different files: health can approve a client the group cannot run, or reject
one it could.

These tests pin the resolution to one answer, taken once. A configured client
is resolved the way the health probe will run it, and the composed argv carries
that resolved file rather than the name, so the two cannot disagree. The
remaining parts of health -- the store and the daemon -- are checked only for
the order in which they can veto, because a client that does not resolve makes
the other two questions meaningless.
"""

import os
import typing as typ
from pathlib import Path

from buildlimits.kache import Kache

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    import pytest

#: The document a healthy `kache daemon status --json` prints.
RUNNING = '{"daemon_running": true}'


def _script(
    directory: Path, body: str, *, name: str = "kache", mode: int = 0o755
) -> Path:
    """Write a stand-in client and make it runnable.

    Parameters
    ----------
    directory : Path
        Where to write the client.
    body : str
        The shell script, including its shebang.
    name : str
        The client's file name.
    mode : int
        The permission bits to give it.

    Returns
    -------
    Path
        The written script.
    """
    path = directory / name
    path.write_text(body, encoding="ascii")
    path.chmod(mode)
    return path


def _client(directory: Path, name: str = "kache") -> Path:
    """Write a client that reports the documented status document."""
    return _script(directory, f"#!/bin/sh\nprintf '%s\\n' '{RUNNING}'\n", name=name)


def _silent(directory: Path) -> Path:
    """Write a client that runs and says nothing at all."""
    return _script(directory, "#!/bin/sh\nexit 0\n")


def test_a_bare_name_is_resolved_on_the_supervisors_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A name health resolves is the file the group runs."""
    client = _client(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))

    kache = Kache("kache")
    argv, _env = kache.compose(("/t/rustc", "-vV"), {})

    assert kache.is_reachable(), "the client resolves and the daemon answers"
    assert argv[0] == str(client), (
        "a bare name must become the file health probed, or the launch searches again"
    )


def test_a_name_that_does_not_resolve_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A client the supervisor cannot find is a health failure, not a crash."""
    monkeypatch.setenv("PATH", str(tmp_path))

    assert not Kache("kache").is_reachable(), "a missing client is unavailable"


def test_an_absolute_client_is_used_as_given(tmp_path: Path) -> None:
    """An absolute path needs no search and is composed unchanged."""
    client = _client(tmp_path, "managed")
    kache = Kache(str(client))

    assert kache.is_reachable(), "the configured file is executable"
    assert kache.compose(("/t/rustc",), {})[0][0] == str(client), (
        "an absolute client is not rewritten"
    )


def test_a_client_that_is_not_executable_is_unavailable(tmp_path: Path) -> None:
    """The configured file exists but cannot be run, so no group may use it."""
    client = _script(tmp_path, "#!/bin/sh\nexit 0\n", mode=0o644)

    assert not Kache(str(client)).is_reachable(), (
        "a file the supervisor cannot execute is not a usable client"
    )


def test_the_store_is_checked_before_the_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unusable store is unavailable even when the client resolves.

    The store check can only veto after the client resolves, so this also
    pins the order: a client that does not resolve would return before the
    store were ever consulted, and the fixture would prove nothing.
    """
    _client(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path))
    store = tmp_path / "store"
    store.write_text("not a directory", encoding="ascii")

    assert not Kache("kache", cache_dir=store).is_reachable(), (
        "a store that cannot be written makes the cache unavailable"
    )
    assert Kache("kache").is_reachable(), (
        "the same client and daemon are healthy once the store is out of the way"
    )


def test_a_client_that_answers_nothing_is_unavailable(tmp_path: Path) -> None:
    """Output that is not the documented document is not evidence of a daemon."""
    assert not Kache(str(_silent(tmp_path))).is_reachable(), (
        "an empty status document does not prove a running daemon"
    )


def test_the_probe_asks_about_the_configured_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe environment is the supervisor's, pointed at the group's socket.

    Both halves matter: the socket must be the configured one rather than
    kache's default, and anything else the operator set must survive, or a
    host whose daemon is configured through the environment would be probed
    with settings the probe never saw.
    """
    recorded = tmp_path / "probe-env"
    client = _script(
        tmp_path,
        f'#!/bin/sh\nprintf "%s\\n%s\\n" "$KACHE_SOCKET_PATH" "$MARKER" > {recorded}\n'
        f'printf \'{{"daemon_running": true, "socket": "%s"}}\\n\' "$KACHE_SOCKET_PATH"\n',
    )
    socket = tmp_path / "kache.sock"
    monkeypatch.setitem(os.environ, "MARKER", "inherited")

    assert Kache(str(client), socket_path=socket).is_reachable(), (
        "the document names the configured socket"
    )
    seen_socket, seen_marker = recorded.read_text(encoding="ascii").split()
    assert seen_socket == str(socket), (
        "the probe exported the socket rather than relying on kache's default"
    )
    assert seen_marker == "inherited", "the probe inherits the supervisor's settings"


def test_a_socket_the_document_does_not_name_is_unavailable(tmp_path: Path) -> None:
    """A daemon answering on another endpoint is not the one the group uses."""
    client = _script(
        tmp_path,
        '#!/bin/sh\nprintf \'{"daemon_running": true, "socket": "/elsewhere"}\\n\'\n',
    )

    assert not Kache(str(client), socket_path=tmp_path / "kache.sock").is_reachable(), (
        "a daemon on a different socket is not the daemon this group was promised"
    )


def test_composition_never_forwards_a_caller_sccache_variable() -> None:
    """The composed environment is kache's, whatever the caller exported."""
    caller: cabc.Mapping[str, str] = {
        "SCCACHE_SERVER_UDS": "/run/sccache.sock",
        "PATH": os.environ.get("PATH", ""),
    }

    _argv, env = Kache("/managed/kache").compose(("/t/rustc",), caller)

    assert not [key for key in env if key.startswith("SCCACHE_")], (
        "a forwarded SCCACHE_* variable would name a server nobody accounted for"
    )
    assert env["PATH"] == caller["PATH"], "the rest of the environment is untouched"
