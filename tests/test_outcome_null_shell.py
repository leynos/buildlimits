"""Default-off recording owns no process, file, socket or thread resource."""

import builtins
import socket
import subprocess
import threading
import typing as typ
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from buildlimits.supervisor_outcome_setup import GroupEnding
from buildlimits.supervisor_outcomes import build_shell

if typ.TYPE_CHECKING:
    from buildlimits.pending_request import Pending
    from buildlimits.supervisor import Supervisor


def test_off_mode_has_no_recording_resource_effects() -> None:
    """The null shell remains inert even when every resource is watched."""
    supervisor = SimpleNamespace(
        settings=SimpleNamespace(cache_outcomes="off"),
    )
    with (
        mock.patch.object(subprocess, "Popen") as popen,
        mock.patch.object(builtins, "open") as open_file,
        mock.patch.object(socket, "socket") as open_socket,
        mock.patch.object(threading, "Thread") as start_thread,
        mock.patch.object(Path, "open") as open_path,
    ):
        shell = build_shell(typ.cast("Supervisor", supervisor))
        shell.launched(typ.cast("Pending", object()), "g1")
        shell.emptied(GroupEnding("g1", True, True))
        shell.on_tick(0.0)
        assert shell.describe() is None, "default-off status remains absent"
        shell.close()

    for resource in (popen, open_file, open_socket, start_thread, open_path):
        resource.assert_not_called()
