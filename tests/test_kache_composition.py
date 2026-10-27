"""What a kache-mode group actually runs, over the shapes Cargo can ask for.

Composition happens in one place -- :meth:`buildlimits.groups.Groups.launch_spec`
-- so this module asks that one question of every caller shape that has bitten
before: a plain compiler, a workspace wrapper Cargo installed ahead of it, and a
caller that had already wrapped the compiler in a cache of its own.

Two settings are part of the contract rather than tuning. ``KACHE_SCHEDULER``
is disabled because a whole-pool request under memory pressure would admit a
second time, and the fail-open would run a compile the pool never saw. Every
``SCCACHE_*`` variable is dropped because the group is not talking to sccache,
and forwarding one would point kache's compiler at a server this supervisor
never accounted for.

The last cases check the adapter side, which is the other place an argument
vector is read: it recognises a toolchain behind a leading cache layer, and it
must never rewrite the vector it was given.
"""

import selectors
import typing as typ
from pathlib import Path

import pytest
from request_helpers import launch_pending
from toolchain_trees import make_toolchain

from buildlimits import compiler_argv
from buildlimits.config import SupervisorConfig
from buildlimits.groups import Groups
from buildlimits.kache import LOCAL_ONLY, SCHEDULER_DISABLE, Kache
from buildlimits.toolchain_env import LIBRARY_PATH_VARIABLE, toolchain_environment

if typ.TYPE_CHECKING:
    from buildlimits.cache_backend import CacheBackend
    from buildlimits.systemd import UserManager

#: The configured client, distinct from every caller-supplied layer below so a
#: row cannot pass by accidentally leaving the caller's own client in place.
MANAGED = "/managed/kache"

#: A caller's environment, carrying both a setting kache must drop and one it
#: forwards. The forwarded entry proves filtering is by prefix, not wholesale.
CALLER_ENV: typ.Final[dict[str, str]] = {
    "PATH": "/toolchain/bin",
    "SCCACHE_SERVER_UDS": "/run/user/1/buildlimits/sccache.sock",
    "SCCACHE_IGNORE_SERVER_IO_ERROR": "1",
}

SOCKET = Path("/run/user/1/buildlimits/kache.sock")

#: ``(case id, requested argv, the argv the group must run)``.
COMPOSITIONS: typ.Final[tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]] = (
    ("plain", ("/t/rustc", "-vV"), (MANAGED, "/t/rustc", "-vV")),
    (
        "workspace-wrapper",
        ("/t/cargo-wrapper", "/t/rustc", "-vV"),
        (MANAGED, "/t/cargo-wrapper", "/t/rustc", "-vV"),
    ),
    (
        "callers-kache-layer",
        ("/caller/kache", "/t/rustc", "-vV"),
        (MANAGED, "/t/rustc", "-vV"),
    ),
    (
        "callers-sccache-layer",
        ("/caller/sccache", "/t/rustc", "-vV"),
        (MANAGED, "/t/rustc", "-vV"),
    ),
)

CASES = pytest.mark.parametrize(
    ("requested", "expected"),
    [(requested, expected) for _, requested, expected in COMPOSITIONS],
    ids=[case for case, _, _ in COMPOSITIONS],
)


def _kache_groups(selector: selectors.BaseSelector) -> Groups:
    """Build the registry a kache-mode supervisor composes its groups with."""
    settings = SupervisorConfig(
        cache_mode="kache",
        kache_executable=MANAGED,
        kache_socket=SOCKET,
    )
    backend: CacheBackend = Kache(settings.kache_executable, settings.kache_socket)
    return Groups(
        settings,
        typ.cast("UserManager", object()),
        selector,
        lambda: 0.0,
        backend,
    )


@CASES
def test_kache_composition(
    selector: selectors.BaseSelector,
    requested: tuple[str, ...],
    expected: tuple[str, ...],
) -> None:
    """Every caller shape is composed with exactly one leading kache client."""
    spec = _kache_groups(selector).launch_spec(
        launch_pending(requested, CALLER_ENV), cached=True
    )
    assert spec.argv == expected, "one managed client wraps the bare compiler"


def test_kache_groups_disable_the_scheduler_and_stay_local() -> None:
    """The two settings that keep an admitted group from outgrowing its scope."""
    argv, env = Kache(MANAGED, SOCKET).compose(("/t/rustc", "-vV"), CALLER_ENV)
    assert argv == (MANAGED, "/t/rustc", "-vV"), argv
    assert env["KACHE_SCHEDULER"] == SCHEDULER_DISABLE == "0", (
        "a second admission under pressure would run a compile the pool never saw"
    )
    assert env["KACHE_LOCAL_ONLY"] == LOCAL_ONLY == "1", (
        "the remote backends are out of scope for this issue"
    )
    assert env["KACHE_SOCKET_PATH"] == str(SOCKET), "the group names its daemon"


def test_kache_groups_carry_no_sccache_settings() -> None:
    """No caller variable survives, and nothing names the other backend."""
    _argv, env = Kache(MANAGED, SOCKET).compose(("/t/rustc", "-vV"), CALLER_ENV)
    assert not [key for key in env if key.startswith("SCCACHE_")], (
        "a forwarded SCCACHE_* variable would point at a server nobody accounted for"
    )
    assert env["PATH"] == "/toolchain/bin", "the rest of the environment is forwarded"


def test_an_uncached_group_forwards_the_callers_lines(
    selector: selectors.BaseSelector,
) -> None:
    """Only a cached group is composed, so an uncached one is untouched."""
    spec = _kache_groups(selector).launch_spec(
        launch_pending(("/t/rustc", "-vV"), CALLER_ENV), cached=False
    )
    assert spec.argv == ("/t/rustc", "-vV"), spec.argv
    assert spec.env["SCCACHE_SERVER_UDS"] == CALLER_ENV["SCCACHE_SERVER_UDS"], (
        "an uncached group forwards the caller's environment verbatim"
    )


@pytest.mark.parametrize("layer", ["kache", "sccache"])
def test_the_adapter_recognises_the_compiler_behind_a_cache_layer(
    tmp_path: Path, layer: str
) -> None:
    """Recognition sees through the layer, and leaves the vector as it found it.

    The adapter builds the environment a compiler runs in, which means finding
    the toolchain behind a cache layer a caller installed. Reading the vector
    must not become rewriting it: the composed argv is the one that executes.
    """
    tree = make_toolchain(tmp_path)
    requested = [f"/opt/bin/{layer}", str(tree.rustc), "-vV"]
    before = list(requested)
    env = {LIBRARY_PATH_VARIABLE: "/existing"}
    result = toolchain_environment(requested, env, "/")
    assert result[LIBRARY_PATH_VARIABLE].endswith("/existing"), (
        "the toolchain behind the layer was recognized"
    )
    assert result[LIBRARY_PATH_VARIABLE].startswith(f"{tree.lib}:"), tree.lib
    assert requested == before, "the requested vector is read, never rewritten"
    assert compiler_argv.compiler_argv(tuple(requested)) == (str(tree.rustc), "-vV"), (
        "the same vector, read by the composition seam, loses only its layer"
    )
