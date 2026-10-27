"""What the composition root hands the supervisor for each configured mode.

:mod:`buildlimits.cache_backends` is the one place a mode string becomes an
adapter. Every other module receives a ``CacheBackend`` and cannot see the
mode, so an argument the root forgets to pass here is invisible everywhere
else: the adapter falls back to its own default and the configured value is
silently ignored.

That is what these tests exist to catch. Each one drives ``select`` with a
non-default setting and then asks the adapter it built -- through composition,
not through its attributes -- what it would actually run, because the value
health checked and the value the group runs are the two halves that must
agree.
"""

import typing as typ
from pathlib import Path

import pytest

from buildlimits import cache_backends
from buildlimits.config import SupervisorConfig

if typ.TYPE_CHECKING:
    from buildlimits.systemd import UserManager

#: A manager no adapter under test acts on: selection only reads the settings.
MANAGER: typ.Final = typ.cast("UserManager", object())


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (
            SupervisorConfig(
                cache_mode="sccache", sccache_executable="/managed/sccache"
            ),
            "/managed/sccache",
        ),
        (
            SupervisorConfig(cache_mode="kache", kache_executable="/managed/kache"),
            "/managed/kache",
        ),
    ],
    ids=["sccache", "kache"],
)
def test_the_configured_client_is_the_one_composed(
    configured: SupervisorConfig, expected: str
) -> None:
    """A configured client reaches argv, rather than the adapter's default."""
    built = cache_backends.select(configured, MANAGER)
    argv, _env = built.composition.compose(("/t/rustc", "-vV"), {})

    assert argv[0] == expected, (
        f"{configured.cache_mode} mode composed {argv[0]!r}; the client was dropped"
    )
    assert argv[1:] == ("/t/rustc", "-vV"), "only the client prefixes the compiler"


def test_direct_mode_selects_no_cache_and_still_builds_the_managed_adapter() -> None:
    """A mode without a cache composes nothing and leaves recovery inert."""
    built = cache_backends.select(SupervisorConfig(cache_mode="direct"), MANAGER)

    assert built.composition.name == "direct", "direct mode composes no cache"
    assert built.managed.name == "sccache", (
        "the managed adapter is built in every mode so recovery can be wired"
    )


def test_every_mode_states_its_capability() -> None:
    """Only sccache's work outlives its group, and each adapter says so."""
    capabilities = {
        mode: cache_backends.select(
            SupervisorConfig(cache_mode=mode), MANAGER
        ).composition.in_scope_work
        for mode in ("sccache", "kache", "direct")
    }

    assert capabilities == {"sccache": False, "kache": True, "direct": True}, (
        "an unstated capability would silently default to holding tokens"
    )


def test_the_composed_client_is_the_resolved_one(tmp_path: Path) -> None:
    """A configured client is resolved once and composed as that file.

    A bare name is looked up on the supervisor's ``PATH``. Writing the lookup
    into argv means the group runs the file health verified instead of letting
    the launch search the requesting build's ``PATH``, which may differ.
    """
    client = tmp_path / "kache"
    client.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    client.chmod(0o755)

    built = cache_backends.select(
        SupervisorConfig(cache_mode="kache", kache_executable=str(client)), MANAGER
    )
    argv, _env = built.composition.compose(("/t/rustc", "-vV"), {})

    assert argv[0] == str(client), "the composed client is the file that was probed"


def test_the_configured_daemon_and_store_reach_a_kache_group() -> None:
    """Health and composition must name the same daemon and the same store.

    The probe checks the socket and directory this configuration names, so a
    group that let kache resolve its own would write somewhere nothing checked.
    """
    built = cache_backends.select(
        SupervisorConfig(
            cache_mode="kache",
            kache_socket=Path("/run/user/1/buildlimits/kache.sock"),
            kache_cache_dir=Path("/var/cache/kache"),
        ),
        MANAGER,
    )
    _argv, env = built.composition.compose(("/t/rustc", "-vV"), {})

    assert env["KACHE_SOCKET_PATH"] == "/run/user/1/buildlimits/kache.sock", (
        "the group names the daemon health probed"
    )
    assert env["KACHE_CACHE_DIR"] == "/var/cache/kache", (
        "the group writes the store health checked"
    )
