"""Compiler adapters: ``RUSTC_WRAPPER`` and ``RUSTDOC`` entry points.

Cargo calls rustc as ``<adapter> <compiler> <arguments...>`` and rustdoc as
``<adapter> <arguments...>``. Requests preserve caller arguments, cwd, environment,
attributes and descriptors. Compiler streams remain unchanged; heartbeats use
stderr. Nested tools reuse admission through ``/proc/self/cgroup`` membership;
only the supervisor creates scopes and kills survivors before restart admission.
Run ``python -m buildlimits.adapter <compiler> <arguments...>`` for diagnosis.
"""

import functools
import os
import signal as signal
import sys
import typing as typ
from pathlib import Path

from buildlimits import attributes, cgroups, config, descriptors
from buildlimits.adapter_connection import SYSTEM_DIALLER as SYSTEM_DIALLER
from buildlimits.adapter_connection import AdapterError as AdapterError
from buildlimits.adapter_connection import Dialler as Dialler
from buildlimits.adapter_connection import SupervisorClosedError
from buildlimits.adapter_connection import connect as connect
from buildlimits.adapter_connection import unix_socket as unix_socket
from buildlimits.adapter_invocation import AdmissionAttempt, Invocation, Progress
from buildlimits.adapter_invocation import BusyError as BusyError
from buildlimits.adapter_invocation import RestartingError as RestartingError
from buildlimits.adapter_invocation import refusal as _refusal
from buildlimits.adapter_metrics import AdapterMetrics
from buildlimits.adapter_replies import next_reply as _next_reply
from buildlimits.adapter_retry import retry_request as _retry_request
from buildlimits.adapter_signals import _reproduce as _reproduce
from buildlimits.adapter_signals import observe_signals
from buildlimits.framing import DescriptorSendPressureError
from buildlimits.groups import compiler_argv
from buildlimits.heartbeat import (
    Heartbeat,
    crate_label,
    heartbeat_seconds,
    positive_seconds,
)
from buildlimits.heartbeat import write_line as _stderr
from buildlimits.protocol import (
    Admitted,
    CompileRequest,
    Finished,
    ProtocolError,
    Queued,
    Refused,
    encode_request,
    send_frame,
)
from buildlimits.rustc_query import is_toolchain_query
from buildlimits.rustdoc import RustdocError, resolve_rustdoc
from buildlimits.timing import SYSTEM_RETRY_WAIT, RetryWait
from buildlimits.toolchain_env import toolchain_environment

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import socket

    from buildlimits.metrics import MetricsPort
    from buildlimits.protocol import Exit, Reply

DEFAULT_UNAVAILABLE_SECONDS = 300.0
DEFAULT_COMPILERS_SLICE = "buildlimits-compilers.slice"
USAGE_STATUS = 2
FAILURE_STATUS = 1


def in_admitted_scope(env: cabc.Mapping[str, str]) -> bool:
    """Decide whether this process already runs inside an admitted scope.

    Parameters
    ----------
    env : Mapping[str, str]
        The environment, which may name a non-default compilers slice.

    Returns
    -------
    bool
        Whether the process's own control group is a compiler scope.
    """
    own = cgroups.own_path()
    if own is None:
        return False
    slice_unit = env.get("BUILD_LIMITS_COMPILERS_SLICE", DEFAULT_COMPILERS_SLICE)
    return cgroups.is_admitted_scope(own, slice_unit)


def run(
    invocation: Invocation,
    *,
    retry: RetryWait = SYSTEM_RETRY_WAIT,
    metrics: MetricsPort | None = None,
) -> int:
    """Run one compiler invocation under admission.

    Parameters
    ----------
    invocation : Invocation
        The compiler's argument vector, whether it may be cached, and the
        environment to send and to read the adapter settings from.
    retry : RetryWait
        Monotonic time, sleeper and jitter for descriptor-pressure retries.
    metrics : MetricsPort | None
        Silent summary port; absent means native journal emission.

    Returns
    -------
    int
        The compiler's exit status. A compiler killed by a signal is
        reproduced by killing this process with the same signal.

    Raises
    ------
    AdapterError
        If the supervisor cannot be reached or refuses the request.
    KeyboardInterrupt
        If the interpreter interrupts the invocation.
    """  # ruff: ignore[docstring-extraneous-exception] - AdapterError propagates from admission helpers.
    env = invocation.env
    clock = retry.clock.now
    heartbeat = Heartbeat(
        crate_label(invocation.argv), heartbeat_seconds(env), clock=clock
    )
    unavailable = positive_seconds(
        env, ("BUILD_LIMITS_UNAVAILABLE_SECS",), DEFAULT_UNAVAILABLE_SECONDS
    )
    summary = AdapterMetrics(clock, heartbeat.phase_started, metrics)
    try:
        with observe_signals(summary.interrupted):
            attempt = AdmissionAttempt(heartbeat.clock() + unavailable, summary)
            request = functools.partial(_request_once, invocation, heartbeat, attempt)
            exit_ = _retry_request(request, heartbeat, attempt, retry)
            summary.finish()
            return _reproduce(exit_)
    except KeyboardInterrupt:
        summary.interrupted()
        raise
    finally:
        summary.finish()


def _request_once(
    invocation: Invocation, heartbeat: Heartbeat, attempt: AdmissionAttempt
) -> Exit:
    """Recapture caller descriptors for one bounded admission attempt.

    Returns
    -------
    Exit
        Completed compiler status, after queued progress or admission.

    Raises
    ------
    AdapterError
        If descriptor capture and encoding exhaust the admission deadline.
    """
    deadline, summary = attempt.deadline, attempt.summary
    env = dict(invocation.env)
    try:
        conn = connect(
            config.supervisor_socket(env),
            deadline,
            heartbeat,
            Dialler(attempted=summary.attempted),
        )
    except AdapterError:
        summary.outcome = "connect_timeout"
        raise
    with conn:
        _send_compile_request(conn, invocation, heartbeat, attempt)
        return _await_result(conn, heartbeat, attempt, deadline=deadline)


def _send_compile_request(
    conn: socket.socket,
    invocation: Invocation,
    heartbeat: Heartbeat,
    attempt: AdmissionAttempt,
) -> None:
    """Encode and send one admission request within its deadline.

    Raises
    ------
    AdapterError
        If the deadline expires before the request is sent.
    BusyError
        If descriptor transfer is refused before any bytes are queued.
    RestartingError
        If the supervisor connection is lost while sending the request.
    """
    fds = descriptors.inheritable_descriptors(exclude={conn.fileno()})
    request = CompileRequest(
        argv=tuple(invocation.argv),
        cwd=str(Path.cwd()),
        env=dict(invocation.env),
        attributes=attributes.capture(),
        fd_targets=tuple(fds),
        label=heartbeat.label,
        cacheable=invocation.cacheable,
    )
    frame = encode_request(request)
    remaining = attempt.deadline - heartbeat.clock()
    if remaining <= 0:
        msg = "compile admission request deadline expired"
        raise AdapterError(msg)
    conn.settimeout(remaining)
    try:
        send_frame(conn, frame, fds)
    except DescriptorSendPressureError as error:
        raise BusyError(str(error)) from error
    except ConnectionError as error:
        msg = f"the build-limits supervisor went away: {error}"
        raise RestartingError(msg) from error
    finally:
        conn.settimeout(None)


def _await_result(
    conn: socket.socket,
    heartbeat: Heartbeat,
    attempt: AdmissionAttempt,
    *,
    deadline: float | None = None,
) -> Exit:
    """Relay progress until the supervisor reports the result.

    Returns
    -------
    Exit
        How the compiler ended.

    Raises
    ------
    AdapterError
        If the supervisor refuses the request or replies out of turn.
    BusyError
        If descriptor pressure refuses a request before it is recorded.
    RestartingError
        If the supervisor restarts before the compiler was admitted.
    """  # ruff: ignore[docstring-extraneous-exception] - raised via _refusal's result.
    summary = attempt.summary
    progress = Progress()
    while True:
        reply = _reply_or_restart(conn, heartbeat, deadline, progress)
        match reply:
            case None:
                continue
            case Queued(position=position):
                deadline = None
                progress.recorded = True
                heartbeat.ahead = position
            case Admitted(notice=notice):
                deadline = None
                progress.recorded = progress.admitted = True
                attempt.admitted()
                heartbeat.admitted()
                summary.admission(heartbeat.phase_started)
                _stderr(
                    f"[build-limits] {heartbeat.label}: {notice}" if notice else None
                )
            case Finished(exit=exit_):
                summary.outcome = "completed"
                return exit_
            case Refused():
                error = _refusal(reply, progress)
                if not isinstance(error, (BusyError, RestartingError)):
                    summary.outcome = "refused"
                raise error
            case other:
                msg = f"unexpected reply {other!r}"
                raise AdapterError(msg)


def _reply_or_restart(
    conn: socket.socket,
    heartbeat: Heartbeat,
    deadline: float | None,
    progress: Progress,
) -> Reply | None:
    """Wait for one reply, treating a lost supervisor as a restart before admission.

    Before admission nothing ran, so the request is safe to resubmit; after it
    the compiler may have, and the build must fail.

    Returns
    -------
    Reply | None
        The reply, or ``None`` when a heartbeat fell due first.

    Raises
    ------
    SupervisorClosedError
        If the supervisor is lost after the compiler was admitted.
    RestartingError
        If the supervisor is lost before admission.
    """  # ruff: ignore[docstring-extraneous-exception] - Progress builds it.
    try:
        if deadline is None:
            return _next_reply(conn, heartbeat)
        return _next_reply(conn, heartbeat, deadline=deadline)
    except SupervisorClosedError as error:
        if progress.admitted:
            raise
        raise progress.restart(str(error)) from error


def _main(argv: list[str], *, cacheable: bool, exempt_queries: bool = False) -> int:
    """Run an adapter entry point.

    Returns
    -------
    int
        The compiler's exit status, or the adapter's own failure status.
    """
    if not argv:
        _stderr("build-limits: no compiler given")
        return USAGE_STATUS
    try:
        # One effective environment serves all paths, so a launch under
        # admission and either direct exec see the same compiler lookup.
        cwd = str(Path.cwd())
        env = toolchain_environment(argv, os.environ, cwd)
        if in_admitted_scope(env):
            os.execvpe(argv[0], argv, env)  # ruff: ignore[start-process-with-no-shell] - already admitted; become the compiler.
        if exempt_queries and is_toolchain_query(argv, env, cwd):
            heartbeat = Heartbeat(crate_label(argv), heartbeat_seconds(env))
            AdapterMetrics(heartbeat.clock, heartbeat.phase_started).exempted()
            compiler = compiler_argv(tuple(argv))
            os.execvpe(compiler[0], compiler, env)  # ruff: ignore[start-process-with-no-shell] - query is proven not to compile; become rustc directly.
        return run(Invocation(argv, cacheable, env))
    except (
        AdapterError,
        attributes.AttributeCaptureError,
        ProtocolError,
        OSError,
    ) as error:
        _stderr(f"build-limits: {error}")
        return FAILURE_STATUS


def rustc_main() -> int:
    """Run the ``RUSTC_WRAPPER`` adapter.

    Returns
    -------
    int
        The compiler's exit status.
    """
    return _main(sys.argv[1:], cacheable=True, exempt_queries=True)


def rustdoc_main() -> int:
    """Run the ``RUSTDOC`` adapter.

    Returns
    -------
    int
        Rustdoc's exit status.
    """
    try:
        real = resolve_rustdoc(os.environ, str(Path.cwd()))
    except RustdocError as error:
        _stderr(f"build-limits: {error}")
        return FAILURE_STATUS
    return _main([real, *sys.argv[1:]], cacheable=False, exempt_queries=False)


if __name__ == "__main__":
    sys.exit(rustc_main())
