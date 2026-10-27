"""The operating-system side of admitted compiler groups.

:class:`Groups` launches each admitted request's compiler in its own
transient scope, reaps group leaders, terminates groups and notices when a
scope has emptied. Admission decides when each of these happens; this module
only performs them and reports what the operating system did.
"""

import contextlib
import dataclasses as dc
import os
import resource
import selectors
import typing as typ
from pathlib import Path

from buildlimits import cgroups, group_drain, kache_guard, launch
from buildlimits.cache_backend import CacheBackend, DirectCache
from buildlimits.cache_modes import is_cache
from buildlimits.compiler_argv import compiler_argv
from buildlimits.descriptors import close_all
from buildlimits.drain_metrics import DrainMetrics
from buildlimits.effects import Exit
from buildlimits.journal import log
from buildlimits.systemd import SystemdError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.config import SupervisorConfig
    from buildlimits.requesters import Pending
    from buildlimits.systemd import UserManager

SCOPE_MOVE_TIMEOUT = 5.0


@dc.dataclass(slots=True)
class Running:
    """The operating-system side of an admitted group.

    Attributes
    ----------
    group_id : str
        The admission group identifier.
    request_id : int
        The request it serves.
    scope_dir : Path
        The scope's control-group directory.
    child : launch.HeldChild
        The launch child, later the group's leader.
    kill_at : float | None
        When to escalate a termination to ``SIGKILL``.
    exec_error : str | None
        Bounded setup diagnostic retained when a held launch is abandoned.
    """

    group_id: str
    request_id: int
    scope_dir: Path
    child: launch.HeldChild
    kill_at: float | None = None
    exec_error: str | None = None


@dc.dataclass(slots=True)
class Groups:
    """Every admitted group whose scope has not yet emptied.

    Slots keep each group record's layout closed, matching the rest of the
    package. A suite that substitutes ``reap``, ``terminate`` or ``advance``
    patches the class rather than the instance: a slotted instance refuses a
    method rebound on it, while the class attribute is still replaceable.

    Parameters
    ----------
    settings : SupervisorConfig
        The supervisor's configuration.
    manager : UserManager
        The user manager port, which creates scopes.
    selector : selectors.BaseSelector
        The event loop's selector, which watches each launch child's error
        pipe.
    clock : Callable[[], float]
        A monotonic clock.
    backend : CacheBackend
        The cache every cached group is composed with. The composition root
        supplies it, so this module names no product.
    drain_metrics : DrainMetrics
        Shared operation observer for scope control.
    running : dict[str, Running]
        Groups whose scope has not yet emptied, by group identifier.
    slice_dir : Path or None
        The compilers' slice, known once the supervisor passes its barrier.
    guard : kache_guard.Strays
        The last reading of kache processes no admitted scope covers. It is
        only refreshed when the configured backend is in-scope, because only
        such a backend can start a kache process from inside a group.
    """

    settings: SupervisorConfig
    manager: UserManager
    selector: selectors.BaseSelector
    clock: cabc.Callable[[], float]
    backend: CacheBackend = dc.field(default_factory=DirectCache)
    drain_metrics: DrainMetrics = dc.field(default_factory=DrainMetrics)
    running: dict[str, Running] = dc.field(default_factory=dict)
    slice_dir: Path | None = None
    guard: kache_guard.Strays = dc.field(default_factory=kache_guard.Strays)

    def place(self, entry: Pending, group_id: str, *, cached: bool) -> Running | None:
        """Start a held launch child for a request and move it into a scope.

        The child waits at its barrier until :meth:`release`, so nothing of
        the compiler runs outside the scope.

        Parameters
        ----------
        entry : Pending
            The admitted request; its descriptors pass to the child.
        group_id : str
            The admission group identifier.
        cached : bool
            Whether to compose the compiler with the cache backend.

        Returns
        -------
        Running | None
            The placed group, or ``None`` when the child or its scope could
            not be made, in which case any child has been abandoned and the
            caller must account for the failed launch.

        Raises
        ------
        RuntimeError
            If called before the supervisor has passed its restart barrier.
        """
        if self.slice_dir is None:
            msg = "groups cannot be placed before the supervisor has started"
            raise RuntimeError(msg)
        entry.group_id = group_id
        entry.admitted = self.clock()
        spec = self.launch_spec(entry, cached=cached)
        try:
            child = launch.spawn_held(spec)
        except OSError as error:
            # The request's descriptors stay with it and close when the
            # caller refuses it.
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            program = ascii(Path(spec.argv[0]).name[:160])[1:-1]
            entry.exec_error = (
                f"cannot start {program}: stage=spawn errno={error.errno} "
                f"nofile={soft}:{hard}"
            )
            log(f"group {group_id}: {entry.exec_error}")
            return None
        fds, entry.fds = entry.fds, []
        close_all(fds)
        unit = f"{cgroups.SCOPE_PREFIX}{os.getpid()}-{group_id}{cgroups.SCOPE_SUFFIX}"
        problem = self._place_in_scope(child, unit)
        if problem is not None:
            entry.exec_error = child.abandon()
            log(
                f"group {group_id}: {problem}"
                + (f"; {entry.exec_error}" if entry.exec_error else "")
            )
            return None
        running = Running(group_id, entry.request_id, self.slice_dir / unit, child)
        self.running[group_id] = running
        return running

    def abandon_held(self, running: Running) -> None:
        """Stop an unreleased child before accounting for a failed launch.

        Parameters
        ----------
        running : Running
            Placed child whose admission reply could not reach its requester.
        """
        running.exec_error = running.child.abandon()
        self.running.pop(running.group_id, None)
        if running.exec_error:
            with contextlib.suppress(OSError):
                log(f"group {running.group_id}: {running.exec_error}")

    def release(self, running: Running) -> bool:
        """Watch setup before releasing a child that is already in its scope.

        Parameters
        ----------
        running : Running
            A group returned by :meth:`place`.

        Returns
        -------
        bool
            Whether the barrier was released; failures abandon the child.
        """
        errors = running.child.errors
        watched = False
        try:
            if errors is not None:
                self.selector.register(
                    errors, selectors.EVENT_READ, ("errors", running.group_id)
                )
                watched = True
            running.child.release()
        except (OSError, ValueError, KeyError) as error:
            if watched and errors is not None:
                with contextlib.suppress(KeyError, ValueError):
                    self.selector.unregister(errors)
            running.exec_error = running.child.abandon()
            self.running.pop(running.group_id, None)
            detail = f"; {running.exec_error}" if running.exec_error else ""
            log(
                f"group {running.group_id}: launch release failed: "
                f"{type(error).__name__}{detail}"
            )
            return False
        return True

    def exec_outcome(self, group_id: str) -> tuple[int, str] | None:
        """Read whether a released child managed to execute its compiler.

        Parameters
        ----------
        group_id : str
            The group whose error pipe is readable or whose leader ended.

        Returns
        -------
        tuple[int, str] | None
            The request and the reason its compiler failed to start, or
            ``None`` when it started or the outcome was already read.
        """
        running = self.running.get(group_id)
        if running is None or running.child.errors is None:
            return None
        message = launch.read_exec_outcome(
            running.child, before_close=self.selector.unregister
        )
        if running.child.errors is not None:
            return None
        if message is None:
            return None
        log(f"group {group_id}: {message}")
        return running.request_id, message

    def reap(self) -> list[tuple[Running, Exit]]:
        """Collect every group leader that has ended.

        Returns
        -------
        list[tuple[Running, Exit]]
            Each ended leader's group and exit.
        """
        ended = []
        for running in list(self.running.values()):
            status = running.child.reap()
            if status is not None:
                code, signal_number = launch.exit_of(status)
                ended.append((running, Exit(code, signal_number)))
        return ended

    def terminate(self, group_id: str) -> None:
        """Send ``SIGTERM`` to a group and schedule escalation to ``SIGKILL``.

        Parameters
        ----------
        group_id : str
            The group to terminate.
        """
        group_drain.terminate(self, group_id)

    def advance(self, draining: cabc.Container[str]) -> list[str]:
        """Escalate overdue terminations and find emptied draining scopes.

        Parameters
        ----------
        draining : Container[str]
            Groups admission is waiting to see empty.

        Returns
        -------
        list[str]
            Draining groups whose scopes have no processes left.
        """
        return group_drain.advance(self, draining)

    def remove(self, group_id: str) -> None:
        """Forget an emptied group and close what its launch child left.

        Parameters
        ----------
        group_id : str
            The emptied group.
        """
        self.running.pop(group_id).child.close()

    def launch_spec(self, entry: Pending, *, cached: bool) -> launch.LaunchSpec:
        """Compose the command, environment and set-up for a launch child.

        Parameters
        ----------
        entry : Pending
            The admitted request.
        cached : bool
            Whether to compose the compiler with the cache backend.

        Returns
        -------
        launch.LaunchSpec
            What the launch child will run.
        """
        request = entry.request
        argv = compiler_argv(request.argv)
        if cached:
            argv, env = self.backend.compose(argv, request.env)
        else:
            env = dict(request.env)
        return launch.LaunchSpec(
            argv=argv,
            env=env,
            cwd=request.cwd,
            received_fds=tuple(entry.fds),
            fd_targets=request.fd_targets,
            plan=entry.plan,
        )

    def cache_notice(self, entry: Pending, *, cached: bool) -> str:
        """Explain an uncached group that the configuration meant to cache.

        The line names the decision rather than diagnosing it: a group compiles
        directly both while the server cannot be reached and while a recovery
        holds the cache out of service, and calling the second case
        *unreachable* would tell the operator to look for a fault that is not
        there. What the build can act on is that this compilation is not cached.

        Parameters
        ----------
        entry : Pending
            The admitted request.
        cached : bool
            Whether the group runs through the configured cache backend.

        Returns
        -------
        str
            A line for the build's standard error, or an empty string.
        """
        # Rustdoc and configured direct mode never meant to be cached, and a
        # cached group is serving the request as asked. Only the remaining case
        # -- a request that wanted a cache and did not get one -- has anything
        # to tell the build.
        wanted = entry.request.cacheable and is_cache(self.settings.cache_mode)
        if cached or not wanted:
            return ""
        return self.backend.fallback_notice()

    def _place_in_scope(self, child: launch.HeldChild, unit: str) -> str | None:
        """Move a held child into a new scope and confirm that it moved.

        Returns
        -------
        str | None
            ``None`` once the child is inside the scope, otherwise the reason
            it is not.
        """
        try:
            self.manager.start_scope(unit, child.pid, self.settings.compilers_slice)
            joined = cgroups.wait_for_membership(child.pid, unit, SCOPE_MOVE_TIMEOUT)
        except (SystemdError, OSError) as error:
            return str(error)
        if not joined:
            return f"the launch child did not join {unit}"
        return None
