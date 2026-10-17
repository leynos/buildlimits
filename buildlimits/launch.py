"""Starting an admitted compiler only once it is inside its scope.

The supervisor forks a launch child that reclaims unrelated inherited
descriptors and blocks on a barrier pipe. While it waits, the supervisor
places the child's process identifier in a new transient scope. Only then
does it release the barrier, and only then does the child set up the
caller's descriptors, working directory and attributes and execute the
compiler. No compiler therefore runs before its scope exists, and the passed
descriptors never have to survive a trip through systemd.

A close-on-exec error pipe reports failures before ``exec``: a successful
``exec`` closes it without writing, so end of file means the compiler started.
"""

import contextlib
import dataclasses as dc
import os
import resource
import signal
import typing as typ

from buildlimits import descriptors
from buildlimits.launch_setup import Setup

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.attributes import AttributePlan

EXEC_FAILURE_STATUS = 127
ABANDONED_STATUS = 126

# Python ignores SIGPIPE and may change these dispositions; a compiler must
# start with the defaults it would have had from Cargo.
_RESET_SIGNALS = (
    signal.SIGPIPE,
    signal.SIGXFSZ,
    signal.SIGINT,
    signal.SIGTERM,
    signal.SIGHUP,
    signal.SIGQUIT,
    signal.SIGCHLD,
)


@dc.dataclass(frozen=True, slots=True)
class LaunchSpec:
    """Everything the launch child needs to become the compiler.

    Attributes
    ----------
    argv : tuple[str, ...]
        The command to execute; ``argv[0]`` is looked up on the caller's
        ``PATH`` when it is not a path.
    env : dict[str, str]
        The compiler's environment.
    cwd : str
        The compiler's working directory.
    received_fds : tuple[int, ...]
        The caller's descriptors, as received by the supervisor.
    fd_targets : tuple[int, ...]
        The number each received descriptor must occupy.
    plan : AttributePlan
        The umask, niceness and limits to set.
    """

    argv: tuple[str, ...]
    env: dict[str, str]
    cwd: str
    received_fds: tuple[int, ...]
    fd_targets: tuple[int, ...]
    plan: AttributePlan


@dc.dataclass(slots=True)
class HeldChild:
    """A launch child waiting at its barrier.

    Attributes
    ----------
    pid : int
        The child's process identifier.
    barrier : int | None
        The barrier pipe's write end, until released.
    errors : int | None
        The error pipe's read end, until the outcome of ``exec`` is known.
    reaped : bool
        Whether child-process ownership has ended.
    error_chunks : list[bytes]
        Diagnostic fragments retained until the writer closes.
    """

    pid: int
    barrier: int | None
    errors: int | None
    reaped: bool = False
    error_chunks: list[bytes] = dc.field(default_factory=list)

    def reap(self) -> int | None:
        """Collect the child's exit status if it has ended.

        Returns
        -------
        int | None
            The wait status, or ``None`` while the child still runs.
        """
        if self.reaped:
            return None
        pid, status = os.waitpid(self.pid, os.WNOHANG)
        if pid == 0:
            return None
        self.reaped = True
        return status

    def release(self) -> None:
        """Let the scoped child execute; a close error cannot undo admission."""
        if self.barrier is None:
            return
        barrier, self.barrier = self.barrier, None
        try:
            os.write(barrier, b"go")
        finally:
            with contextlib.suppress(OSError):
                os.close(barrier)

    def abandon(self) -> str | None:
        """Reap an unreleased child and collect any available setup diagnostic.

        Returns
        -------
        str | None
            Bounded child setup failure, if already available.
        """
        self._reap_abandoned()
        message = None
        if self.errors is not None:
            # Parent setup may have failed before O_NONBLOCK was installed or
            # while it still owns a writer. Never block this cleanup path.
            with contextlib.suppress(OSError):
                os.set_blocking(self.errors, False)
                message = read_exec_outcome(self)
        self.close()
        return message

    def _reap_abandoned(self) -> None:
        """Terminate only this owned child when its exit has not been collected."""
        if self.reaped:
            return
        try:
            ended, _status = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            ended = self.pid
        if ended == 0:
            with contextlib.suppress(ProcessLookupError):
                os.kill(self.pid, signal.SIGKILL)
            with contextlib.suppress(ChildProcessError):
                os.waitpid(self.pid, 0)
        self.reaped = True

    def close(self) -> None:
        """End ownership before closing each remaining supervisor descriptor."""
        for name in ("barrier", "errors"):
            fd = getattr(self, name)
            if fd is not None:
                setattr(self, name, None)
                with contextlib.suppress(OSError):
                    os.close(fd)


def spawn_held(spec: LaunchSpec) -> HeldChild:
    """Fork a launch child that waits for :meth:`HeldChild.release`.

    The supervisor's copies of the received descriptors are left open; the
    caller closes them once the child exists.

    Parameters
    ----------
    spec : LaunchSpec
        What the child will run.

    Returns
    -------
    HeldChild
        The waiting child.

    Raises
    ------
    OSError
        If the pipes or the child cannot be created; nothing is left open.
    """
    pipes: list[int] = []
    try:
        barrier_read, barrier_write = os.pipe2(os.O_CLOEXEC)
        pipes += [barrier_read, barrier_write]
        error_read, error_write = os.pipe2(os.O_CLOEXEC)
        pipes += [error_read, error_write]
        pid = os.fork()
    except OSError:
        descriptors.close_all(pipes)
        raise
    if pid == 0:  # pragma: no cover - runs in the forked child only.
        try:
            _become_compiler(spec, barrier_read, error_write)
        finally:
            # Initialization must never unwind into the copied event loop.
            os._exit(EXEC_FAILURE_STATUS)
    child = HeldChild(pid=pid, barrier=barrier_write, errors=error_read)
    remaining = {barrier_read, error_write}
    try:
        for fd in tuple(remaining):
            remaining.remove(fd)
            os.close(fd)
        os.set_blocking(error_read, False)
    except BaseException:
        child.abandon()
        descriptors.close_all(remaining)
        raise
    return child


def read_exec_outcome(
    child: HeldChild, *, before_close: cabc.Callable[[int], object] | None = None
) -> str | None:
    """Read the outcome of the child's ``exec`` once its error pipe is ready.

    Parameters
    ----------
    child : HeldChild
        A released child whose error pipe is readable.
    before_close : Callable[[int], object] | None
        Release selector ownership at EOF, while the descriptor is still open.
        The pipe closes even if this callback fails.

    Returns
    -------
    str | None
        Failure message after EOF, otherwise ``None``. While ``errors`` is
        still open, ``None`` means the outcome is incomplete; keep watching it.
    """
    if child.errors is None:
        return None
    while True:
        try:
            data = os.read(child.errors, 4096)
        except BlockingIOError:
            return None
        if not data:
            break
        child.error_chunks.append(data)
    errors, child.errors = child.errors, None
    try:
        if before_close is not None:
            before_close(errors)
    finally:
        with contextlib.suppress(OSError):
            os.close(errors)
    message = b"".join(child.error_chunks).decode("utf-8", "replace") or None
    child.error_chunks.clear()
    return message


def exit_of(status: int) -> tuple[int | None, int | None]:
    """Translate a wait status into an exit code or a terminating signal.

    Parameters
    ----------
    status : int
        A status from ``os.waitpid``.

    Returns
    -------
    tuple[int | None, int | None]
        ``(code, None)`` for a normal exit, ``(None, signal)`` otherwise.
    """
    if os.WIFSIGNALED(status):
        return None, os.WTERMSIG(status)
    return os.waitstatus_to_exitcode(status), None


def _become_compiler(  # pragma: no cover - runs in the forked child only.
    spec: LaunchSpec, barrier_read: int, error_write: int
) -> typ.NoReturn:
    """Wait at the barrier, then set up and execute the compiler."""
    setup = Setup(error_write, spec.fd_targets, len(spec.received_fds) + 1)
    try:
        original_limits = resource.getrlimit(resource.RLIMIT_NOFILE)
        setup.limits = original_limits
        setup.stage = "signals"
        signal.set_wakeup_fd(-1)
        setup.stage = "close-inherited"
        descriptors.close_unrelated({*spec.received_fds, barrier_read, error_write})
        setup.open_count = setup.expected_count + 1
        setup.stage = "barrier"
        if os.read(barrier_read, 2) != b"go":
            os._exit(ABANDONED_STATUS)
        os.close(barrier_read)
        setup.open_count = setup.expected_count
        descriptors.apply_relocation(
            spec.received_fds, spec.fd_targets, keep=setup.kept, setup=setup
        )
        setup.stage = "signals"
        signal.set_wakeup_fd(-1)
        for number in _RESET_SIGNALS:
            signal.signal(number, signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_SETMASK, [])
        setup.stage = "cwd"
        os.chdir(spec.cwd)
        setup.apply_attributes(spec.plan)
        if "RLIMIT_NOFILE" not in spec.plan.rlimits:
            setup.stage = "rlimit:RLIMIT_NOFILE"
            resource.setrlimit(resource.RLIMIT_NOFILE, original_limits)
            setup.limits = original_limits
        setup.stage = "exec"
        os.execvpe(spec.argv[0], list(spec.argv), spec.env)  # ruff: ignore[start-process-with-no-shell] - the compiler Cargo chose.
    except BaseException as error:  # ruff: ignore[blind-except] - every setup failure must reach the supervisor.
        with contextlib.suppress(BaseException):
            setup.report(spec.argv[0], error)
    os._exit(EXEC_FAILURE_STATUS)
