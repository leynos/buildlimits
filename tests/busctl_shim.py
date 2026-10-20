"""A ``busctl`` stand-in that holds scope placement open until a test says so.

The supervisor places each admitted request's held launch child by running
``busctl`` to call ``StartTransientUnit``. While that command runs, the
supervisor's event loop is inside ``Groups.place``, with the launch child and the
``busctl`` process both in the supervisor's own control group. A restart that
lands in that window is the one that a ``KillMode=control-group`` stop breaks.
The shim makes the window as wide as a scenario needs, deterministically,
without a timing race.

The shim passes every call except ``StartTransientUnit`` straight to the real
``busctl``. A held ``StartTransientUnit`` records that it started, waits for the
release file, then runs the real command. If it receives ``SIGTERM`` while it
waits, it records that too, with a census of its control group, so a scenario
can prove who the stop signalled. It exits silently on the signal, as a
``busctl`` killed by it does.
"""

import stat
import sys
import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

REAL_BUSCTL = "/usr/bin/busctl"

_SCRIPT = f"""#!{sys.executable}
import os
import signal
import sys
import time
from pathlib import Path

here = Path(__file__).parent
real = "{REAL_BUSCTL}"


def census() -> str:
    # Who shares this process's control group, and in what state. This is the
    # evidence for which processes a stop signal can reach.
    own = Path("/proc/self/cgroup").read_text().split("::")[1].strip().lstrip("/")
    rows = []
    for pid in (Path("/sys/fs/cgroup") / own / "cgroup.procs").read_text().split():
        try:
            fields = Path(f"/proc/{{pid}}/stat").read_text().rsplit(")", 1)
        except OSError:
            continue  # the process exited after the cgroup listing
        comm = fields[0].split("(", 1)[1]
        rows.append(f"{{pid}} {{comm}} {{fields[1].split()[0]}}")
    return "\\n".join(rows)


def on_term(_signum, _frame):
    time.sleep(0.3)
    try:
        (here / "terminated").write_text(census())
    finally:
        os._exit(143)


if "StartTransientUnit" in sys.argv and (here / "hold").exists():
    signal.signal(signal.SIGTERM, on_term)
    (here / "entered").write_text(census())
    while not (here / "release").exists():
        time.sleep(0.05)
os.execv(real, [real, *sys.argv[1:]])
"""


class SlowBusctl:
    """A directory holding the shim, to be put ahead of the unit's ``PATH``.

    Parameters
    ----------
    directory : Path
        A directory owned by the scenario.
    """

    def __init__(self, directory: Path) -> None:
        """Install the shim as ``directory/busctl``."""
        self.directory = directory
        directory.mkdir(parents=True)
        shim = directory / "busctl"
        shim.write_text(_SCRIPT, encoding="utf-8")
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR)

    def raise_flag(self, name: str) -> None:
        """Raise a flag the shim reads: ``hold`` or ``release``."""
        (self.directory / name).touch()

    def seen(self, name: str) -> bool:
        """Report a flag the shim raised: ``entered`` or ``terminated``.

        Returns
        -------
        bool
            Whether a placement is waiting inside the shim (``entered``), or
            the waiting shim received ``SIGTERM`` (``terminated``).
        """
        return (self.directory / name).exists()

    def census(self, name: str) -> str:
        """Read what the shim recorded of its control group.

        Parameters
        ----------
        name : str
            ``entered`` for the moment the placement began, ``terminated`` for
            the moment after a ``SIGTERM`` reached the shim.

        Returns
        -------
        str
            One ``pid command state`` line per process in the shim's group.
        """
        return (self.directory / name).read_text(encoding="utf-8")
