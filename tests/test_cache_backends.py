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
from buildlimits.cache_modes import MODES
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


@pytest.mark.parametrize("mode", sorted(MODES))
def test_the_mode_table_and_the_selected_adapter_agree(mode: str) -> None:
    """The two statements of each mode's capability cannot drift apart.

    ``MODES`` answers recovery, server-watch and status; the adapter answers
    the supervisor. A disagreement would let the supervisor hold a token under
    one rule while the machinery that decides when it comes back applied the
    opposite one, so every row is compared against the adapter it selects.
    """
    settings = SupervisorConfig(cache_mode=mode)
    if mode == "kache":
        settings = SupervisorConfig(
            cache_mode=mode, kache_socket=Path("/run/kache/kache.sock")
        )
    composition = cache_backends.select(settings, MANAGER).composition
    expected = MODES[mode]

    assert composition.in_scope_work is expected.in_scope_work, (
        f"{mode}: MODES and the selected adapter disagree on in_scope_work"
    )
    assert composition.name == expected.name, (
        f"{mode}: MODES and the selected adapter disagree on name"
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
