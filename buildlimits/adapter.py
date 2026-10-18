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
import select
import signal as signal
import sys
import typing as typ
from pathlib import Path

from buildlimits import attributes, cgroups, config, descriptors
from buildlimits.adapter_connection import SYSTEM_DIALLER as SYSTEM_DIALLER
from buildlimits.adapter_connection import AdapterError as AdapterError
from buildlimits.adapter_connection import Dialler as Dialler
from buildlimits.adapter_connection import connect as connect
from buildlimits.adapter_connection import unix_socket as unix_socket
from buildlimits.adapter_invocation import AdmissionAttempt
from buildlimits.adapter_invocation import BusyError as BusyError
from buildlimits.adapter_invocation import retry_request as _retry_request
from buildlimits.adapter_metrics import AdapterMetrics
from buildlimits.adapter_signals import _reproduce as _reproduce
from buildlimits.adapter_signals import observe_signals
from buildlimits.framing import DescriptorSendPressureError
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
    decode_reply,
    encode_request,
    recv_frame,
    send_frame,
)
from buildlimits.rustdoc import RustdocError, resolve_rustdoc
from buildlimits.timing import SYSTEM_RETRY_WAIT, RetryWait

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
    argv: cabc.Sequence[str],
    *,
    cacheable: bool,
    retry: RetryWait = SYSTEM_RETRY_WAIT,
    metrics: MetricsPort | None = None,
) -> int:
    """Run one compiler invocation under admission.

    Parameters
    ----------
    argv : Sequence[str]
        The real compiler's argument vector.
    cacheable : bool
        Whether the supervisor may compose the compiler with sccache.
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
    env = dict(os.environ)
    clock = retry.clock.now
    heartbeat = Heartbeat(crate_label(argv), heartbeat_seconds(env), clock=clock)
    unavailable = positive_seconds(
        env, ("BUILD_LIMITS_UNAVAILABLE_SECS",), DEFAULT_UNAVAILABLE_SECONDS
    )
    summary = AdapterMetrics(clock, heartbeat.phase_started, metrics)
    try:
        with observe_signals(summary.interrupted):
            attempt = AdmissionAttempt(heartbeat.clock() + unavailable, summary)
            request = functools.partial(
                _request_once, argv, heartbeat, attempt, cacheable=cacheable
            )
            exit_ = _retry_request(request, heartbeat, attempt, retry)
            summary.finish()
            return _reproduce(exit_)
    except KeyboardInterrupt:
        summary.interrupted()
        raise
    finally:
        summary.finish()


def _request_once(
    argv: cabc.Sequence[str],
    heartbeat: Heartbeat,
    attempt: AdmissionAttempt,
    *,
    cacheable: bool,
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
    BusyError
        If the initial rights transfer is refused before any bytes are queued.
    """
    deadline, summary = attempt.deadline, attempt.summary
    env = dict(os.environ)
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
        fds = descriptors.inheritable_descriptors(exclude={conn.fileno()})
        request = CompileRequest(
            argv=tuple(argv),
            cwd=str(Path.cwd()),
            env=env,
            attributes=attributes.capture(),
            fd_targets=tuple(fds),
            label=heartbeat.label,
            cacheable=cacheable,
        )
        frame = encode_request(request)
        remaining = deadline - heartbeat.clock()
        if remaining <= 0:
            msg = "compile admission request deadline expired"
            raise AdapterError(msg)
        conn.settimeout(remaining)
        try:
            send_frame(conn, frame, fds)
        except DescriptorSendPressureError as error:
            raise BusyError(str(error)) from error
        finally:
            conn.settimeout(None)
        return _await_result(conn, heartbeat, summary, deadline=deadline)


def _next_reply(
    conn: socket.socket, heartbeat: Heartbeat, *, deadline: float | None = None
) -> Reply | None:
    """Wait for one reply, writing a heartbeat line if one falls due.

    Returns
    -------
    Reply | None
        The reply, or ``None`` when the heartbeat interval passed first.

    Raises
    ------
    AdapterError
        If the supervisor closes the connection.
    """
    timeout = heartbeat.timeout()
    if deadline is not None:
        remaining = deadline - heartbeat.clock()
        if remaining <= 0:
            msg = (
                "compile admission did not acknowledge the request before its deadline"
            )
            raise AdapterError(msg)
        timeout = min(timeout, remaining)
    ready, _writable, _errored = select.select([conn], [], [], timeout)
    if not ready:
        _stderr(heartbeat.tick())
        return None
    return _receive_reply(conn, heartbeat, deadline)


def _receive_reply(
    conn: socket.socket, heartbeat: Heartbeat, deadline: float | None
) -> Reply:
    """Decode a ready reply, reclaim stray fds and always restore its timeout.

    Returns
    -------
    Reply
        Decoded reply after reclaiming unsolicited descriptors.

    Raises
    ------
    AdapterError
        If the reply deadline expires or the supervisor closes the connection.
    """
    original = conn.gettimeout()
    try:
        if deadline is not None:
            remaining = deadline - heartbeat.clock()
            if remaining <= 0:
                msg = "compile admission reply deadline expired"
                raise AdapterError(msg)
            conn.settimeout(remaining)
        frame = recv_frame(conn)
    finally:
        conn.settimeout(original)
    if frame is None:
        msg = "the build-limits supervisor closed the connection"
        raise AdapterError(msg)
    body, stray = frame
    for fd in stray:
        os.close(fd)
    return decode_reply(body)


def _await_result(
    conn: socket.socket,
    heartbeat: Heartbeat,
    summary: AdapterMetrics,
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
    """
    recorded = False
    while True:
        reply = (
            _next_reply(conn, heartbeat)
            if deadline is None
            else _next_reply(conn, heartbeat, deadline=deadline)
        )
        match reply:
            case None:
                continue
            case Queued(position=position):
                deadline = None
                recorded = True
                heartbeat.ahead = position
            case Admitted(notice=notice):
                deadline = None
                recorded = True
                heartbeat.admitted()
                summary.admission(heartbeat.phase_started)
                _stderr(
                    f"[build-limits] {heartbeat.label}: {notice}" if notice else None
                )
            case Finished(exit=exit_):
                summary.outcome = "completed"
                return exit_
            case Refused(reason="busy", message=message) if not recorded:
                raise BusyError(message)
            case Refused(message=message):
                summary.outcome = "refused"
                raise AdapterError(message)
            case other:
                msg = f"unexpected reply {other!r}"
                raise AdapterError(msg)


def _main(argv: list[str], *, cacheable: bool) -> int:
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
        if in_admitted_scope(os.environ):
            os.execvp(argv[0], argv)  # ruff: ignore[start-process-with-no-shell] - already admitted; become the compiler.
        return run(argv, cacheable=cacheable)
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
    return _main(sys.argv[1:], cacheable=True)


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
    return _main([real, *sys.argv[1:]], cacheable=False)


if __name__ == "__main__":
    sys.exit(rustc_main())
