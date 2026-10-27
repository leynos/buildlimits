"""Timing shim for host Cargo compiler probes."""

import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

    from systemd_harness import Harness

TIMING_SHIM_SOURCE = r"""#!/usr/bin/env python3
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

adapter = Path(__file__).with_name("build-limits-rustc")
started = time.monotonic()
error = None
try:
    status = subprocess.call([str(adapter), *sys.argv[1:]])
except OSError as exception:
    error = str(exception)
    print(f"rustc timing shim: {error}", file=sys.stderr)
    status = 127
ended = time.monotonic()
record = {
    "argv": sys.argv[1:],
    "started": started,
    "ended": ended,
    "status": status,
    "workspace_wrapper": os.environ.get("RUSTC_WORKSPACE_WRAPPER"),
}
if error is not None:
    record["error"] = error
path = Path(os.environ["RUSTC_QUERY_TIMING_LOG"])
descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
try:
    os.write(descriptor, (json.dumps(record, sort_keys=True) + "\n").encode())
finally:
    os.close(descriptor)
if status < 0:
    signum = -status
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)
sys.exit(status)
"""


def install_timing_shim(harness: Harness) -> tuple[Path, Path]:
    """Install a timing wrapper beside the adapter in the harness work tree.

    Parameters
    ----------
    harness : Harness
        The isolated host harness.

    Returns
    -------
    tuple[Path, Path]
        The executable shim and its JSON-lines log path.

    Examples
    --------
    Install the shim and pass its path as ``RUSTC_WRAPPER``::

        shim, log = install_timing_shim(harness)
        environment = cargo_environment(harness, shim, log)
    """
    directory = harness.work / "timing-shim"
    directory.mkdir()
    adapter_sibling = directory / "build-limits-rustc"
    adapter_sibling.symlink_to(harness.adapter.resolve())
    shim = directory / "rustc-timing-shim"
    shim.write_text(TIMING_SHIM_SOURCE)
    shim.chmod(0o755)
    return shim, directory / "rustc-probes.jsonl"
