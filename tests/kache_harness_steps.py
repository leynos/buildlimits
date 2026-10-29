"""Harness steps that wait on a supervisor configured with kache.

They live apart from ``systemd_harness`` to keep that module inside the
project's line limit; each one still needs the harness's own journal, status
and unit names, so they are written as a mixin the harness inherits.
"""

import time
import typing as typ

from kache_unit_files import start_kache_daemon

if typ.TYPE_CHECKING:
    from pathlib import Path


class KacheHarnessSteps:
    """Waiting and daemon steps the kache systemd scenarios share.

    The members below are the harness facts each step reads; declaring them
    here states the mixin's contract without importing the harness, which
    would be a cycle.
    """

    #: The transient unit this harness owns for the fake kache daemon.
    kache_unit: str
    #: The socket the fake daemon binds, exported to every group.
    kache_socket: Path
    #: The harness slice cleanup stops units in.
    parent_slice: str

    def status(self) -> dict[str, object]:
        """Read the supervisor's status document.

        Returns
        -------
        dict[str, object]
            The document the harness's supervisor reports.
        """
        raise NotImplementedError

    def journal(self) -> str:
        """Read the supervisor's journal.

        Returns
        -------
        str
            The journal text the harness has captured so far.
        """
        raise NotImplementedError

    def _start_kache_daemon(self) -> None:
        """Start the fake kache daemon this harness owns, before admission."""
        start_kache_daemon(self.kache_unit, self.kache_socket, self.parent_slice)

    def _await_kache_fallback(self) -> None:
        """Wait until the supervisor has read kache as unavailable.

        The first reading is taken as part of start-up, so waiting here keeps
        a scenario from racing it: a build admitted before the reading would
        be composed without a notice and would prove nothing about the
        fallback.

        Status alone cannot show that the reading happened, because admission
        is uncached before the first reading as well as after it. Start-up's
        final metric is the evidence: it is published after the reachability
        reading and after the supervisor reports READY.

        Raises
        ------
        RuntimeError
            If the supervisor reads a configured, unrunnable kache as healthy
            or never finishes its first reading.
        """
        if not self._await_start_metric():
            msg = "the isolated supervisor never finished starting"
            raise RuntimeError(msg)
        if self.status()["caching"]:
            msg = "the isolated supervisor composed with a kache that cannot run"
            raise RuntimeError(msg)

    def _await_start_metric(self) -> bool:
        """Wait for the metric start-up publishes after its cache reading.

        Each read is a journal query, so it is paced rather than run on the
        harness's usual tight poll.

        Returns
        -------
        bool
            Whether the metric appeared in time.
        """
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if "metric supervisor_started" in self.journal():
                return True
            time.sleep(0.2)
        return False
