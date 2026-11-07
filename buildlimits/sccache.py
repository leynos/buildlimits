"""The managed sccache server: health and live compilation count.

ADR 011 keeps sccache in the admitted compile path. The server runs as a
systemd user service on a private Unix socket. Two facts about it drive the
supervisor's decisions:

- whether it is reachable, because a client that cannot reach a server starts
  one of its own inside the compiler's scope, which must never happen;
- how many compilations it is running, because a cache-miss compilation runs
  as a direct child of the server, outside any compiler scope, and must stay
  covered by a held token.
"""

import dataclasses as dc
import socket
import typing as typ

from buildlimits import sccache_cgroup
from buildlimits.bus_properties import BusQueryError, control_unit
from buildlimits.cache_backend import (
    CacheError,
    LiveCacheWorkError,
    StartupAccountingError,
    StoppedPopulatedCacheError,
)
from buildlimits.cache_backend import CacheError as SccacheError
from buildlimits.proc_trees import count_children
from buildlimits.systemd import SystemdError, UserManager

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

# ``SccacheError`` is the same class object as ``CacheError``, not a subclass:
# the cache backend seam names the exception for what it is, so every backend
# raises one vocabulary, while this sccache-specific spelling keeps the name
# its existing callers and tests use. An ``except`` on either catches the same
# failures.

__all__ = [
    "LiveCacheWorkError",
    "ManagedSccache",
    "SccacheError",
    "StartupAccountingError",
    "StoppedPopulatedCacheError",
]


@dc.dataclass(slots=True)
class ManagedSccache:
    """The managed sccache server, as seen by the supervisor.

    This is the detached cache backend: its server runs the miss compilation
    as a direct child, outside the compiler group's scope, so a scope can
    empty while its compilation still runs. That is why it also carries the
    detached-only lifecycle in :meth:`accounted_drain`, :meth:`stop` and
    :meth:`start`.

    Parameters
    ----------
    unit : str
        The server's systemd user unit.
    socket_path : Path
        The server's private Unix socket.
    manager : UserManager
        The user manager port.
    executable : str
        The client that composes a cached group. It is the configured
        command, which may be an absolute path or a name resolved on the
        build's ``PATH``.
    """

    unit: str
    socket_path: Path
    manager: UserManager = dc.field(default_factory=UserManager)
    executable: str = "sccache"
    name: str = dc.field(init=False, default="sccache")
    in_scope_work: bool = dc.field(init=False, default=False)

    def compose(
        self, argv: tuple[str, ...], env: cabc.Mapping[str, str]
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        """Compose a compiler invocation with the managed server.

        The client is pointed at this server's socket, so a client that could
        not reach it would start a server of its own inside the compiler's
        scope; and it is told to compile in scope rather than fail if the
        server goes away mid-build.

        Parameters
        ----------
        argv : tuple[str, ...]
            The bare compiler invocation.
        env : Mapping[str, str]
            The requesting client's environment.

        Returns
        -------
        tuple[tuple[str, ...], dict[str, str]]
            The invocation prefixed with the cache client, and the
            environment with the server's address added.

        Examples
        --------
        >>> cache = ManagedSccache("s.service", "/run/s.sock")
        >>> cache.compose(("/t/rustc", "-vV"), {})[0]
        ('sccache', '/t/rustc', '-vV')
        """
        composed = dict(env)
        composed["SCCACHE_SERVER_UDS"] = str(self.socket_path)
        # Losing the server mid-build falls back to compiling in scope.
        composed["SCCACHE_IGNORE_SERVER_IO_ERROR"] = "1"
        return (self.executable, *argv), composed

    def fallback_notice(  # ruff: ignore[no-self-use] - protocol member.
        self,
    ) -> str:
        """Explain an uncached group that the configuration meant to cache.

        The line names the decision rather than diagnosing it: a group compiles
        directly both while the server cannot be reached and while a recovery
        holds the cache out of service, and calling the second case
        *unreachable* would tell the operator to look for a fault that is not
        there. What the build can act on is that this compilation is not cached.

        The caller has already excluded rustdoc and configured direct mode, so
        this line is only ever produced for a build that asked to be cached.

        Returns
        -------
        str
            The line for the build's standard error.
        """
        return (
            "the managed sccache server is not serving compilations; "
            "compiling without the cache"
        )

    def is_reachable(self, timeout: float = 0.5) -> bool:
        """Check that the server accepts connections on its socket.

        Parameters
        ----------
        timeout : float
            Seconds to wait for the connection.

        Returns
        -------
        bool
            Whether a connection succeeded; ``False`` when the socket is
            absent, refuses connections or does not answer in time.

        Raises
        ------
        CacheError
            If the probe fails for any other reason, such as a permission
            fault, so that a misconfiguration is not mistaken for a stopped
            server.
        """
        probe = None
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(timeout)
            probe.connect(str(self.socket_path))
        except FileNotFoundError, ConnectionRefusedError, TimeoutError:
            return False
        except OSError as error:
            msg = f"cannot probe {self.socket_path}: {error}"
            raise CacheError(msg) from error
        finally:
            if probe is not None:
                probe.close()
        return True

    def _verify_stopped(  # ruff: ignore[no-self-use] - delegates to sccache_cgroup.
        self, properties: cabc.Mapping[str, str]
    ) -> None:
        """Require stopped unit state and an empty cgroup before accounting.

        Raises
        ------
        ValueError
            If the stopped state cannot be verified.
        StoppedPopulatedCacheError
            If the service is stopped but its control group still holds
            processes, so surviving work may hold a shared jobserver slot.
        """  # ruff: ignore[docstring-extraneous-exception] - sccache_cgroup raises both.
        sccache_cgroup.verify_stopped_service(properties)

    def active_compiles(self, *, snapshot: cabc.Mapping[str, str] | None = None) -> int:
        """Count visible compiler trees for diagnostics, without proving drain.

        Parameters
        ----------
        snapshot : Mapping[str, str] or None
            Previously queried manager properties, or None to query the unit.

        Returns
        -------
        int
            Measured compiler-root trees in the verified server cgroup,
            including orphaned work. Stopped units require verified emptiness.

        Raises
        ------
        CacheError
            If the unit, its control group or its processes cannot be read.
            ``SccacheError`` names the same class. Callers must then keep
            holding tokens: an unknown load is not a zero load.
        """
        try:
            properties = (
                self.manager.show(self.unit, "MainPID", "ControlGroup")
                if snapshot is None
                else snapshot
            )
            return sccache_cgroup.visible_roots(properties, counter=count_children)
        except (SystemdError, OSError, ValueError) as error:
            msg = f"cannot read {self.unit}'s load: {error}"
            raise SccacheError(msg) from error

    def accounted_drain(self) -> int:
        """Verify that potentially detached server work can no longer start.

        Returns
        -------
        int
            Zero only for a verified stopped and empty service.

        Raises
        ------
        CacheError
            If the service is live, contains work, or cannot be inspected.
            ``SccacheError`` names the same class. A live server may hold
            accepted tasks awaiting a jobserver token; zero compiler processes
            therefore does not prove completion.
        """
        try:
            properties = self.manager.show(
                self.unit, "MainPID", "ControlGroup", "ActiveState"
            )
            self._verify_accounting(properties)
        except (SystemdError, OSError, ValueError) as error:
            msg = f"cannot account for {self.unit}'s detached work: {error}"
            raise SccacheError(msg) from error
        return 0

    def _verify_accounting(self, properties: cabc.Mapping[str, str]) -> None:
        """Reject live, transitioning or nonempty service snapshots.

        Raises
        ------
        ValueError
            If the complete snapshot cannot prove stopped and empty state.
        LiveCacheWorkError
            If a successfully queried live service may retain detached requests.
        """
        if not {"MainPID", "ControlGroup", "ActiveState"} <= properties.keys():
            msg = "manager omitted required drain properties"
            raise ValueError(msg)
        if int(properties["MainPID"]) != 0:
            roots = self._diagnostic_roots(properties)
            shown = "unknown" if roots is None else roots
            msg = (
                "live sccache service may retain detached requests; "
                f"visible compiler roots={shown}; operator accounting required"
            )
            raise LiveCacheWorkError(msg, roots=roots)
        self._verify_stopped(properties)

    def _diagnostic_roots(self, properties: cabc.Mapping[str, str]) -> int | None:
        """Sample visible work without authorizing token release on any outcome.

        Returns
        -------
        int or None
            Root count, or None when the count could not be established.
        """
        try:
            return self.active_compiles(snapshot=properties)
        except SccacheError:
            return None

    def stop(self) -> None:
        """Ask the manager to queue a stop for the managed server.

        Raises
        ------
        SystemdError
            If the manager refuses the job or cannot be reached. The unit may
            still be live afterwards: only :meth:`accounted_drain` proves that
            it stopped and emptied.
        """  # ruff: ignore[docstring-extraneous-exception] - _control raises it.
        self._control("stop")

    def start(self) -> None:
        """Ask the manager to queue a start for the managed server.

        Raises
        ------
        SystemdError
            If the manager refuses the job or cannot be reached.
        """  # ruff: ignore[docstring-extraneous-exception] - _control raises it.
        self._control("start")

    def _control(self, verb: str) -> None:
        """Run one control verb privately, falling back to the public bus.

        Raises
        ------
        SystemdError
            If neither transport can queue the job. Both faults are reported
            together so the operator sees the whole reason.
        """
        try:
            self.manager.control_unit(verb, self.unit)
        except SystemdError as private_error:
            try:
                control_unit(self.manager.runner, verb, self.unit)
            except (BusQueryError, SystemdError) as public_error:
                msg = (
                    f"cannot {verb} {self.unit}: "
                    f"{ascii(str(private_error)[:300])[1:-1]}; "
                    f"{ascii(str(public_error)[:300])[1:-1]}"
                )
                raise SystemdError(msg) from public_error
