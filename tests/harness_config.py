"""Owned harness configuration assets; lifecycle and token authority stay in Harness."""

import json
import sys
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    from systemd_harness import Harness

FIXTURES = Path(__file__).with_name("fixtures")


def write_config(harness: Harness) -> None:
    """Write owned launcher and supervisor configuration fixtures.

    Parameters
    ----------
    harness : Harness
        Fixture owner whose runtime, compiler pool and unique units are used.
    """
    sccache = harness.runtime / "fake-sccache"
    sccache.write_text(
        f'#!/bin/sh\nexec {sys.executable} {FIXTURES / "fake_sccache.py"} client "$@"\n'
    )
    sccache.chmod(0o755)
    settings = {
        "device": str(harness.pool),
        "slots": harness.slots,
        "socket_path": str(harness.runtime / "supervisor.sock"),
        "lock_path": str(harness.runtime / "supervisor.lock"),
        "compilers_slice": harness.compilers_slice,
        "cache_mode": "sccache" if harness.cached else "direct",
        "sccache_executable": str(sccache),
        "sccache_unit": harness.sccache_unit,
        "sccache_socket": str(harness.runtime / "sccache.sock"),
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
    lines = [f"{key} = {json.dumps(value)}" for key, value in settings.items()]
    (harness.runtime / "supervisor.toml").write_text("\n".join(lines) + "\n")
