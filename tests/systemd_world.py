"""What the steps of one systemd acceptance scenario share.

A scenario's steps are separate functions bound to one feature file, so the
supervisor they started and the builds they ran have to live somewhere between
them. This module is that somewhere: the harness every scenario needs, the
builds it must cancel, and the scripts that make a compiler observable from
outside its own scope.

It also carries the checks several features make in the same words. A cached
group, an in-scope group and a directly compiled one all return their token the
same way, so "the slot is free again" is written once here rather than restated
per feature.
"""

import typing as typ

import pytest
from systemd_harness import Harness, kill_group, pid_alive, wait_for

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import subprocess
    from pathlib import Path


class World:
    """One scenario's supervisor, the builds it ran and the pids they wrote.

    A feature that needs more records subclasses this and keeps them itself,
    so every scenario gets the same harness access, cleanup and build
    bookkeeping without restating them.

    Attributes
    ----------
    harness : Harness | None
        The supervisor this scenario started, once a step has created it.
    builds : list[subprocess.Popen[bytes]]
        Every build the scenario started, so teardown can cancel each one.
    pids : dict[str, int]
        Identifiers read from pid files, keyed by the name of the file the
        compiler wrote them to.
    """

    def __init__(self) -> None:
        """Create a world with nothing started."""
        self.harness: Harness | None = None
        self.builds: list[subprocess.Popen[bytes]] = []
        self.pids: dict[str, int] = {}

    @property
    def h(self) -> Harness:
        """Read the harness, once a step has created it.

        Returns
        -------
        Harness
            The started harness.

        Raises
        ------
        RuntimeError
            If the scenario used the harness before creating it, which is a
            mistake in the feature's steps rather than a fault in the
            supervisor.
        """
        if self.harness is None:
            msg = "a scenario step must create the harness first"
            raise RuntimeError(msg)
        return self.harness

    def marker(self, name: str) -> Path:
        """Read the path of a file in the harness's work directory.

        Parameters
        ----------
        name : str
            The file's name.

        Returns
        -------
        Path
            The file's path.
        """
        return self.h.work / name

    def run(self, script: str, *, piped: bool = False) -> subprocess.Popen[bytes]:
        """Start a build and keep it for cleanup.

        Parameters
        ----------
        script : str
            The shell script standing in for the compiler.
        piped : bool
            Whether to connect the build's standard streams to pipes.

        Returns
        -------
        subprocess.Popen[bytes]
            The running adapter.
        """
        process = self.h.compile(script, piped=piped)
        self.builds.append(process)
        return process

    def run_ok(self, script: str) -> None:
        """Run a build, wait for it, and require that it succeeded.

        Parameters
        ----------
        script : str
            The shell script standing in for the compiler.

        Raises
        ------
        AssertionError
            If the build failed; the message is the supervisor's journal.
        """
        if self.run(script).wait(timeout=60) != 0:
            raise AssertionError(self.h.journal())

    def capture(self, script: str) -> tuple[bytes, bytes]:
        """Run a build to completion and return what it wrote to each stream.

        Parameters
        ----------
        script : str
            The shell script standing in for the compiler.

        Returns
        -------
        tuple[bytes, bytes]
            Everything the build wrote to standard output and standard error.

        Raises
        ------
        AssertionError
            If the build failed; the message is what it wrote to standard
            error, which is where an adapter explains a degraded build.
        """
        process = self.run(script, piped=True)
        stdout, stderr = process.communicate(b"", timeout=60)
        if process.returncode != 0:
            raise AssertionError(stderr)
        return stdout, stderr

    def cancel_background_build(self) -> None:
        """Run a build with a background child, then cancel it while it waits.

        The pids are read before the group is signalled, so the check that
        follows can say each process is gone rather than that no process was
        ever seen.
        """
        process = self.run(background_script(self))
        self.read_pid("leader")
        self.read_pid("background")
        kill_group(process)
        process.wait(timeout=30)

    def read_pid(self, name: str) -> int:
        """Wait for a pid file a compiler wrote, and record what it holds.

        Parameters
        ----------
        name : str
            The pid file's name in the harness's work directory, which is also
            the key the identifier is recorded under.

        Returns
        -------
        int
            The identifier the compiler wrote.

        Raises
        ------
        AssertionError
            If the compiler never wrote the pid.
        """
        path = self.marker(name)
        if not wait_for(lambda: path.exists() and bool(path.read_text().strip())):
            msg = f"the compiler never wrote its {name} pid"
            raise AssertionError(msg)
        self.pids[name] = int(path.read_text())
        return self.pids[name]

    def assert_background_group_gone(self) -> None:
        """Check a cancelled group's compiler and its background child ended.

        Raises
        ------
        AssertionError
            If either process is still running.
        """
        for name in ("leader", "background"):
            pid = self.pids[name]
            if not wait_for(lambda pid=pid: not pid_alive(pid)):
                msg = f"the {name} process survived its group"
                raise AssertionError(msg)

    def assert_slot_free(self) -> None:
        """Wait for every token the supervisor took to come back.

        Raises
        ------
        AssertionError
            If a token is still held; the message is the supervisor's journal.
        """
        if not wait_for(self.holds_no_tokens):
            raise AssertionError(self.h.journal())

    def holds_no_tokens(self) -> bool:
        """Report whether every token the supervisor took has come back.

        A scenario polls this rather than reading status itself, so "the slot
        is free again" means the same thing whether the group it observed was
        cached, in scope, or compiling directly.

        Returns
        -------
        bool
            Whether the supervisor holds no tokens.
        """
        return self.h.status()["held"] == 0

    def assert_parent_lacks(self, needle: str) -> None:
        """Check the compiler's parent is not the named cache client.

        Parameters
        ----------
        needle : str
            The text that would name the client on the parent's command line.

        Raises
        ------
        AssertionError
            If the compiler recorded no parent, or the parent it recorded
            names the client.
        """
        parent = self.marker("parent").read_text()
        if not parent:
            msg = "the compiler recorded its parent's command line"
            raise AssertionError(msg)
        if needle in parent:
            raise AssertionError(parent)

    def close(self) -> None:
        """Cancel every build, then close the harness and what it owns."""
        for process in self.builds:
            kill_group(process)
            process.wait(timeout=30)
        if self.harness is not None:
            self.harness.close()


def scenario_world[W: World](
    build: cabc.Callable[[], W],
) -> cabc.Callable[[], cabc.Iterator[W]]:
    """Build the ``world`` fixture a scenario module registers.

    Parameters
    ----------
    build : Callable[[], W]
        Constructs the feature's own world, with whatever records its steps
        keep beside the shared ones.

    Returns
    -------
    Callable[[], Iterator[W]]
        A pytest fixture yielding one world and closing it afterwards.
    """

    @pytest.fixture
    def world() -> cabc.Iterator[W]:
        """Provide a scenario's world and clean up everything it created.

        Yields
        ------
        W
            The world one scenario's steps share, closed when it ends.
        """
        state = build()
        try:
            yield state
        finally:
            state.close()

    return world


def background_script(world: World) -> str:
    """Start a compiler that runs a background child and waits for it.

    The pids are recorded before the scenario cancels the group, so the check
    that follows can say each process is gone rather than that no process was
    ever seen.

    Parameters
    ----------
    world : World
        The scenario's world, whose work directory names the pid files.

    Returns
    -------
    str
        The shell script.
    """
    return (
        f"sleep 300 & echo $! > {world.marker('background')}; "
        f"echo $$ > {world.marker('leader')}; wait"
    )


def parent_script(world: World, name: str = "parent") -> str:
    """Record the command line of the process that started the compiler.

    A compiler composed through a cache client has that client as its parent,
    and one compiled directly has the adapter. Reading the parent's own
    ``cmdline`` is how a scenario tells the two apart without trusting what the
    supervisor says it did.

    Parameters
    ----------
    world : World
        The scenario's world, whose work directory holds the file.
    name : str
        The file to write the command line to.

    Returns
    -------
    str
        The shell script.
    """
    return f"tr '\\0' ' ' < /proc/$PPID/cmdline > {world.marker(name)}"
