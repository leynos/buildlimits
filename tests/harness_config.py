"""Owned harness configuration assets; lifecycle and token authority stay in Harness."""

import dataclasses as dc
import json
import sys
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    from systemd_harness import Harness

FIXTURES = Path(__file__).with_name("fixtures")


@dc.dataclass(slots=True)
class KacheSettings:
    """How a harness selects the kache backend.

    It lives here, with the other configuration assets, so that the harness
    module can import it without gaining a cycle through the writer that reads
    a harness's settings.

    Parameters
    ----------
    enabled : bool
        Whether to compose compilers with kache.
    executable : str | None
        A client to configure in place of the kache stand-in, so a scenario can
        stand in for a host where kache is configured but not installed.
    serving : bool
        Whether this harness expects kache to serve builds. When it does, the
        harness starts the fake daemon and waits for composition to become
        healthy; a scenario that configures a client which cannot run sets
        this false, because it means to observe the fallback rather than wait
        for a cache that will never appear.
    """

    enabled: bool = False
    executable: str | None = None
    serving: bool = True


def write_config(harness: Harness) -> None:
    """Write owned launcher and supervisor configuration fixtures.

    Only the selected backend's keys are written. A key belonging to another
    backend is rejected by the configuration loader, so a document carrying
    both sets would be invalid rather than a test of the mode it names.

    Parameters
    ----------
    harness : Harness
        Fixture owner whose runtime, compiler pool and unique units are used.
    """
    settings: dict[str, object] = {
        "device": str(harness.pool),
        "slots": harness.slots,
        "socket_path": str(harness.runtime / "supervisor.sock"),
        "lock_path": str(harness.runtime / "supervisor.lock"),
        "compilers_slice": harness.compilers_slice,
        "cancel_grace_seconds": 2.0,
        "drain_timeout_seconds": 20.0,
        # A scenario that drives the operator's own command pins the policy to
        # "manual", so only that command can open the transaction.
        "cache_recovery": harness.cache_recovery,
        # A per-harness barrier keeps a recovery transaction out of the
        # machine-wide %t/buildlimits/cache-recovery path, which other agents'
        # supervisors read.
        "recovery_marker": str(harness.runtime / "cache-recovery"),
    }
    settings |= _cache_settings(harness)
    lines = [f"{key} = {json.dumps(value)}" for key, value in settings.items()]
    (harness.runtime / "supervisor.toml").write_text("\n".join(lines) + "\n")


def _cache_settings(harness: Harness) -> dict[str, object]:
    """Name the one cache backend this harness is configured for.

    Parameters
    ----------
    harness : Harness
        Fixture owner whose mode, runtime and units the settings describe.

    Returns
    -------
    dict[str, object]
        The mode and the settings that mode owns, and nothing else.
    """
    if harness.kache.enabled:
        return {
            "cache_mode": "kache",
            "kache_executable": _kache_client(harness),
            "kache_socket": str(harness.kache_socket),
            "kache_cache_dir": str(harness.kache_store),
        }
    return {
        "cache_mode": "sccache" if harness.cached else "direct",
        "sccache_executable": _sccache_client(harness),
        "sccache_unit": harness.sccache_unit,
        "sccache_socket": str(harness.runtime / "sccache.sock"),
    }


def _stand_in(harness: Harness, name: str, fixture: str, *verb: str) -> str:
    """Write a launcher for a fixture backend and return its path.

    Parameters
    ----------
    harness : Harness
        Owner of the runtime directory the launcher is written into.
    name : str
        The launcher's file name, which differs per backend so two
        launchers in one runtime never overwrite each other.
    fixture : str
        The fixture module under ``tests/fixtures`` to run.
    verb : str
        Any subcommand word the fixture needs before the compiler's own
        arguments, such as the sccache stand-in's ``client``.

    Returns
    -------
    str
        The launcher's path, which the supervisor is configured with.
    """
    launcher = harness.runtime / name
    words = " ".join(verb)
    launcher.write_text(
        f'#!/bin/sh\nexec {sys.executable} {FIXTURES / fixture} {words} "$@"\n'
    )
    launcher.chmod(0o755)
    return str(launcher)


def _sccache_client(harness: Harness) -> str:
    """Write the sccache stand-in's launcher and return its path.

    Returns
    -------
    str
        The launcher's path, which the supervisor is configured with.
    """
    return _stand_in(harness, "fake-sccache", "fake_sccache.py", "client")


def _kache_client(harness: Harness) -> str:
    """Return the configured kache client, writing the stand-in when unnamed.

    A harness that names its own client keeps it, which is how a scenario
    stands in for a host where kache is configured but not installed.

    Returns
    -------
    str
        The configured client, which may not exist on this host.
    """
    if harness.kache.executable is not None:
        return harness.kache.executable
    return _stand_in(harness, "fake-kache", "fake_kache.py")
