"""The kache backend: an in-scope cache that never outlives its group.

kache wraps the compiler and runs it as a child of the wrapper, inside the
compiler group's own scope. That one fact drives everything here:

- no accepted work can outlive the group, so a kache group returns its token
  when its scope empties, exactly as a direct group does, and never enters
  ``SERVER_DRAIN``;
- there is no managed service for the supervisor to stop, start or account
  for, so this adapter implements composition and health only, and the
  :class:`~buildlimits.cache_backend.DetachedCacheBackend` lifecycle is not
  part of its shape at all.

The daemon policy follows from the same fact. kache v2.0.0-rc.1 offers no
setting that stops a client from starting a daemon, and a daemon started from
inside an admitted group would stay in that group's scope after the compile
finished. Every admitted group therefore points at a separately managed daemon
through ``KACHE_SOCKET_PATH``, disables the built-in scheduler, and is only
composed at all when :meth:`Kache.is_reachable` has confirmed that a daemon is
already answering on that socket. A kache or kache-engine process found outside
an admitted scope is reported by :func:`stray_processes` rather than covered by
a held token.

See ``docs/adr-004-kache-cache-backend.md``.
"""

import dataclasses as dc
import json
import os
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] - fixed argument vectors, never a shell.
import typing as typ
from pathlib import Path

from buildlimits.cache_backend import CacheError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

#: The scheduler is disabled in every admitted group. See ADR 004: a
#: whole-pool request under memory pressure would admit a second time, and the
#: 30-minute fail-open would run a compile the supervisor's pool never saw.
SCHEDULER_DISABLE = "0"

#: Restrict kache to its local store, which keeps its remote-gated start paths
#: inert; the remote backends are out of scope for this issue.
LOCAL_ONLY = "1"

#: Names that identify a kache process outside an admitted compiler scope.
#: The v2 client ``exec``s ``kache-engine`` in place, so a snapshot may see
#: either executable at the wrapper's process identifier.
STRAY_EXECUTABLES = frozenset({"kache", "kache-engine"})

#: The health probe runs on every admission cycle, so it must not hold the
#: supervisor open when a daemon accepts the connection and then stalls. It is
#: the same budget as sccache's socket probe: a slow probe reads as unavailable
#: and the build compiles directly, which is a safe degradation that the next
#: poll reverses, whereas a long timeout stalls reaping and token returns.
STATUS_TIMEOUT_SECONDS = 0.5


@dc.dataclass(frozen=True, slots=True)
class Kache:
    """The kache backend, as seen by the supervisor.

    Parameters
    ----------
    executable : str
        The client that composes a cached group. It may be an absolute path or
        a bare name, which is resolved on the supervisor's own ``PATH`` by
        both the health probe and composition, so a group runs the file
        health verified rather than a second lookup of its own.
    socket_path : Path or None
        The separately managed daemon's socket, passed to every admitted group
        and named in the health probe. ``None`` lets kache resolve its own
        default socket.
    cache_dir : Path or None
        The store kache writes, checked by :meth:`is_reachable` and passed to
        every admitted group as ``KACHE_CACHE_DIR``. ``None`` lets kache
        resolve its own store, which health then cannot check.

    Attributes
    ----------
    name : str
        Always ``kache``, for the journal and status wording.
    in_scope_work : bool
        Always ``True``: kache runs the compiler as a child of the wrapper
        inside the group's scope.
    """

    executable: str = "kache"
    socket_path: Path | None = None
    cache_dir: Path | None = None

    name: str = dc.field(init=False, default="kache")
    in_scope_work: bool = dc.field(init=False, default=True)

    def compose(
        self, argv: tuple[str, ...], env: cabc.Mapping[str, str]
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        """Compose a compiler invocation with kache.

        The caller's argument vector has already had any leading cache layer
        stripped, so a caller that wrapped the compiler itself cannot produce
        ``kache kache rustc``. A workspace wrapper stays in place: kache wraps
        the wrapper, which then runs the compiler.

        A bare name is written out as the absolute path health resolved, so the
        group runs the file that was probed. Otherwise the launch would search
        the requesting build's ``PATH``, which can differ from the supervisor's
        and would make the health answer describe a different executable.

        Parameters
        ----------
        argv : tuple[str, ...]
            The bare compiler invocation, or a wrapper followed by it.
        env : Mapping[str, str]
            The requesting client's environment.

        Returns
        -------
        tuple[tuple[str, ...], dict[str, str]]
            The invocation prefixed with the kache client, and the environment
            with kache's settings and no ``SCCACHE_*`` variables.

        Examples
        --------
        >>> cache = Kache("/b/kache")
        >>> cache.compose(("/t/rustc", "-vV"), {})[0]
        ('/b/kache', '/t/rustc', '-vV')
        >>> cache.compose(("/t/clippy-driver", "/t/rustc"), {})[0][:2]
        ('/b/kache', '/t/clippy-driver')
        >>> stored = Kache("/b/kache", cache_dir=Path("/s"))
        >>> env = stored.compose(("/t/rustc",), {})[1]
        >>> env["KACHE_CACHE_DIR"], "KACHE_SOCKET_PATH" in env
        ('/s', False)
        """
        composed = {
            key: value for key, value in env.items() if not key.startswith("SCCACHE_")
        }
        composed["KACHE_SCHEDULER"] = SCHEDULER_DISABLE
        composed["KACHE_LOCAL_ONLY"] = LOCAL_ONLY
        if self.socket_path is not None:
            composed["KACHE_SOCKET_PATH"] = str(self.socket_path)
        if self.cache_dir is not None:
            # The group must write the store health checked; otherwise the
            # probe verifies one filesystem while builds fill another.
            composed["KACHE_CACHE_DIR"] = str(self.cache_dir)
        return (self._client() or self.executable, *argv), composed

    def _client(self) -> str | None:
        """Resolve the configured client to the file the supervisor can run.

        ``shutil.which`` searches the supervisor's own ``PATH``, and for a
        value that carries a directory component it checks that exact file.
        Health, the daemon probe and composition all read this one answer, so
        they cannot disagree about which executable they mean: a bare name
        cannot resolve for the probe and then fail to resolve for the build.

        Returns
        -------
        str or None
            The resolved client, or ``None`` when nothing runnable matches.
        """
        return shutil.which(self.executable)

    def fallback_notice(self) -> str:  # ruff: ignore[no-self-use] - protocol member.
        """Explain an uncached group that the configuration meant to cache.

        kache has no managed server, so the notice names kache itself rather
        than a service the supervisor owns. It is true of every health
        failure: a missing client, an unusable store and a daemon that is not
        answering all leave the build uncached.

        Returns
        -------
        str
            The line for the build's standard error.
        """
        return "kache is not serving compilations; compiling without the cache"

    def is_reachable(self) -> bool:
        """Check that kache, its store and its daemon are usable right now.

        Health has three parts, all of which must hold before a group is
        composed: the client resolves and is executable, the configured store
        can be created or written, and ``kache daemon status --json`` reports
        a running daemon on the configured socket. The last part is what keeps
        a daemon out of the compiler's scope, because a group is only ever
        pointed at a daemon that is already listening.

        ``kache daemon status`` never starts a daemon and exits zero with
        ``daemon_running: false`` when none is running, so it is safe to call
        on every admission cycle. Every way the probe can fail -- a vanished
        binary, a timeout, output that is not the documented document -- is
        reported as unavailable rather than raised, because the build can
        still compile directly.

        Returns
        -------
        bool
            ``False`` when the client cannot be run, the store is unusable or
            no daemon answers on the configured socket.
        """
        client = self._client()
        if client is None:
            return False
        if self.cache_dir is not None and not _is_writable(self.cache_dir):
            return False
        return self._daemon_running(client)

    def _daemon_running(self, client: str) -> bool:
        """Ask kache whether a daemon answers on the configured socket.

        The probe runs with the configured socket in its environment, so kache
        resolves the same endpoint the group uses even when it is not the
        built-in default. The supervisor's own environment is inherited, so an
        operator's kache configuration reaches the probe.

        Parameters
        ----------
        client : str
            The resolved client, as :meth:`_client` returned it, so the probe
            runs the same file the group will.

        Returns
        -------
        bool
            ``True`` when the status document reports a running daemon for
            that socket; ``False`` when the probe cannot run or cannot answer.
        """
        env = dict(os.environ)
        if self.socket_path is not None:
            env["KACHE_SOCKET_PATH"] = str(self.socket_path)
        try:
            finished = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] - fixed argument vector.
                [client, "daemon", "status", "--json"],
                capture_output=True,
                text=True,
                check=False,
                timeout=STATUS_TIMEOUT_SECONDS,
                env=env,
            )
        except OSError, subprocess.TimeoutExpired:
            return False
        return _reports_running(finished.stdout, self.socket_path)


def _reports_running(stdout: str, socket_path: Path | None) -> bool:
    """Decide whether a ``daemon status --json`` document proves a daemon.

    Parameters
    ----------
    stdout : str
        The command's standard output, expected to be the documented JSON
        document.
    socket_path : Path or None
        The socket the group will be pointed at, or ``None`` when kache's own
        default discovery applies and any reported socket is acceptable.

    Returns
    -------
    bool
        ``True`` only when the document reports ``daemon_running: true`` and,
        for a configured socket, names that same endpoint.

    Examples
    --------
    >>> _reports_running('{"daemon_running": true, "socket": "/r/d.sock"}',
    ...                  Path("/r/d.sock"))
    True
    >>> _reports_running('{"daemon_running": false, "socket": "/r/d.sock"}',
    ...                  Path("/r/d.sock"))
    False
    >>> _reports_running('{"daemon_running": true, "socket": "/r/d.sock"}',
    ...                  Path("/r/other.sock"))
    False
    """
    try:
        document = json.loads(stdout)
    except json.JSONDecodeError:
        return False
    # A mapping pattern is a total test here: a document that is not a mapping
    # at all -- a list, a bare string -- falls through to the final case rather
    # than needing a separate type guard.
    match document:
        case {"daemon_running": True}:
            return socket_path is None or document.get("socket") == str(socket_path)
        case _:
            return False


def _is_writable(directory: Path) -> bool:
    """Report whether a cache directory can be used.

    A directory that does not exist yet is acceptable when its nearest
    existing parent is writable, because kache creates its store on first use.

    Returns
    -------
    bool
        ``True`` when the directory exists and is writable, or can be created.
    """
    probe = directory
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return probe.is_dir() and os.access(probe, os.W_OK | os.X_OK)
    except OSError:
        return False


def stray_processes(
    pids: cabc.Iterable[int],
    *,
    admitted: cabc.Container[int],
    is_kache: cabc.Callable[[int], bool] | None = None,
) -> tuple[int, ...]:
    """Find kache processes that no admitted scope covers.

    A kache daemon started from an admitted group stays in that group's scope
    after the compile ends, and the scope is the supervisor's unit of
    accounting: a token returned for an emptied scope would then cover work
    that is still running. Rather than guess whether such a process is
    harmless, the supervisor reports it as a distinct fault.

    Parameters
    ----------
    pids : Iterable[int]
        Candidate process identifiers read from the compilers' slice.
    admitted : Container[int]
        Processes that an admitted group's scope already covers.
    is_kache : Callable[[int], bool] or None
        The executable test, injectable so the decision can be exercised
        without a live ``/proc``. ``None`` means the real one. The module
        global is read at call time rather than bound as a default, so a test
        that substitutes it is exercising the same call the guard makes.

    Returns
    -------
    tuple[int, ...]
        The identifiers of uncovered kache processes, in the order given.

    Examples
    --------
    >>> stray_processes([10, 11, 12], admitted={11}, is_kache=lambda pid: pid != 12)
    (10,)
    """
    probe = _is_kache if is_kache is None else is_kache
    return tuple(pid for pid in pids if pid not in admitted and probe(pid))


def _is_kache(pid: int) -> bool:
    """Report whether a process's executable is a kache binary.

    The executable link is what the kernel actually ran, so it distinguishes
    kache from a process that merely carries a similar name. A v2 wrapper that
    has ``exec``ed its engine links to ``kache-engine``, which is why both
    names are recognized.

    Returns
    -------
    bool
        ``True`` when ``/proc`` names a kache executable for the process. A
        process that has already exited, or whose executable cannot be read,
        is not reported: the caller is looking for survivors, and an unreadable
        entry is not evidence of one.
    """
    try:
        target = Path(f"/proc/{pid}/exe").readlink()
    except OSError:
        return False
    # A replaced binary still has a live process holding the deleted inode,
    # which the kernel marks in the link text rather than the file name.
    return target.name.removesuffix(" (deleted)") in STRAY_EXECUTABLES


def unavailable(executable: str) -> CacheError:
    """Build the error a caller reports when kache cannot be used.

    Parameters
    ----------
    executable : str
        The configured kache command, quoted in the message so an operator can
        see what was looked for.

    Returns
    -------
    CacheError
        The backend-neutral error naming the missing command.
    """
    return CacheError(f"cannot use {executable}: kache is not available")
