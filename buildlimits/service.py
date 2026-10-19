"""The supervisor's service entry point, ``build-limits-supervisor``.

The provisioning role runs this as a systemd user service with
``Restart=always``. It exits 2 for configuration, singleton-lock or
previous-cache accounting faults,
which a restart cannot fix, and 1 after a failed start or a health failure, so
that systemd restarts it through the restart barrier.

``SIGTERM`` stops it gracefully, so ``systemctl --user restart`` waits for
running compiler groups instead of cancelling them, and exits 0 once they have
finished. ``SIGINT`` stops it immediately, cancelling the queue and the groups.
"""

import argparse
import contextlib
import os
import signal
import typing as typ
from pathlib import Path

from buildlimits import cgroups, config, startup
from buildlimits.journal import log
from buildlimits.journal_native import JournalSink
from buildlimits.metrics import Metrics
from buildlimits.sccache import SccacheError, StartupAccountingError
from buildlimits.supervisor import Supervisor
from buildlimits.systemd import SystemdError

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import PurePosixPath


def main(argv: cabc.Sequence[str] | None = None) -> int:
    """Run the supervisor as a systemd user service.

    Parameters
    ----------
    argv : Sequence[str] | None
        Command-line arguments; ``sys.argv[1:]`` when ``None``.

    Returns
    -------
    int
        The exit status.
    """
    parser = argparse.ArgumentParser(prog="build-limits-supervisor")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path.home() / ".config" / "buildlimits" / "supervisor.toml",
        help="the supervisor configuration file",
    )
    options = parser.parse_args(argv)
    try:
        settings = config.load(options.config)
        lock = startup.acquire_lock(settings.lock_path)
    except (config.ConfigError, startup.StartupError) as error:
        log(str(error))
        return 2
    try:
        return _supervise(settings)
    finally:
        os.close(lock)


def _supervise(settings: config.SupervisorConfig) -> int:
    """Start the supervisor and serve until it stops, holding the lock.

    Returns
    -------
    int
        The exit status.
    """
    own = cgroups.own_path()
    if own is None:
        log("cannot determine the supervisor's control group")
        return 2
    sink = JournalSink(identifier="build-limits-supervisor")
    try:
        supervisor = Supervisor(settings, metrics=Metrics(sink, warn=log))
        try:
            return _serve(supervisor, own)
        finally:
            _release(supervisor)
    finally:
        with contextlib.suppress(Exception):
            sink.close()


def _release(supervisor: Supervisor) -> None:
    """Release the supervisor's descriptors without changing the exit status.

    The process is ending, so a failed release cannot be retried or acted on;
    letting it escape would only replace the status the service has already
    settled with a traceback, so it is logged instead.
    """
    try:
        supervisor.close()
    except Exception as error:  # ruff: ignore[blind-except] - exit status must survive a failed release.
        log(f"releasing the supervisor's descriptors failed: {type(error).__name__}")


def _serve(supervisor: Supervisor, own: PurePosixPath) -> int:
    """Start the configured service and install its orderly shutdown handlers.

    Returns
    -------
    int
        The supervisor result, or its existing categorized startup failure.
    """
    try:
        supervisor.start(own)
    except StartupAccountingError as error:
        log(f"start-up requires operator action: {error}")
        return 2
    except (startup.StartupError, OSError, SystemdError, SccacheError) as error:
        log(f"start-up failed: {error}")
        return 1
    # SIGTERM is what ``systemctl stop`` and ``restart`` send, so it must not
    # cancel work: running groups finish first. SIGINT is the explicit escape.
    signal.signal(signal.SIGTERM, lambda *_: supervisor.request_graceful_stop())
    signal.signal(signal.SIGINT, lambda *_: supervisor.request_stop())
    return supervisor.serve()
