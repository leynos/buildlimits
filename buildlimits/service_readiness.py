"""Signal service readiness only after admission startup has accounted for work.

Only Supervisor owns this boundary. The notification orders a new managed
sccache server after admission's restart barrier without changing token state.
"""

import contextlib
import os
import socket


def notify_ready() -> None:
    """Notify systemd after pool, listener and control watches are established.

    A supervisor invoked outside a notifying service has no notification socket
    and returns normally. Errors inside a notifying service prevent readiness;
    existing compiler work has already been checked before this call.
    """
    _notify(b"READY=1\nSTATUS=Compiler admission ready; managed cache may start")


def notify_status(status: str) -> None:
    """Tell ``systemctl status`` what a stopping supervisor is waiting for.

    The text is advisory and never changes state, so a manager that cannot be
    reached is not an error here: the supervisor must go on draining.

    Parameters
    ----------
    status : str
        One short line, without credentials or request identifiers.
    """
    with contextlib.suppress(OSError):
        _notify(f"STATUS={status[:200]}".encode("ascii", "replace"))


def _notify(message: bytes) -> None:
    """Send one datagram to the manager's notification socket, if there is one.

    Raises
    ------
    OSError
        If the configured systemd notification socket cannot be reached.
    """  # ruff: ignore[docstring-extraneous-exception] - socket creation and notification propagate OSError.
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    with socket.socket(
        socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC
    ) as notification:
        notification.connect(address)
        notification.sendall(message)
