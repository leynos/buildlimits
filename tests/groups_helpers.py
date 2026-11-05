"""A scripted group registry for the supervisor's direct effect tests.

The real :class:`buildlimits.groups.Groups` owns systemd scope placement, a
selector and a backend. A test that drives one effect handler wants none of
that: it wants to say whether placement succeeds and to watch the order of the
effects around it. This module holds that double so the effect tests and the
guard tests can share one definition rather than drift apart.

``settings`` and ``backend`` are the two members the stray-process guard reads
to decide whether it has anything to read at all. Their defaults are the
detached case, so a launch that is not about the guard never touches the slice.
"""

import dataclasses as dc
import typing as typ
from pathlib import Path

from buildlimits import kache_guard
from buildlimits.config import SupervisorConfig
from buildlimits.sccache import ManagedSccache

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.cache_backend import CacheBackend
    from buildlimits.groups import Running
    from buildlimits.requesters import Pending


@dc.dataclass(slots=True)
class FakeGroups:
    """A groups double that scripts whether placement succeeds.

    Parameters
    ----------
    order : list[str]
        Effects are appended here as they happen, in call order.
    placed : object or None
        What :meth:`place` returns. ``None`` scripts a failed placement, which
        is the case that returns the token and refuses the request; any other
        value scripts a success.
    terminate : Callable[[str], None]
        Records a terminate request for a group identifier.
    settings : SupervisorConfig
        Read by the guard to decide whether this configuration can leave a
        cache process in a scope. Defaults to the detached case, which cannot.
    backend : CacheBackend
        Read by the guard for the same capability. Defaults to the detached
        adapter, matching ``settings``.
    slice_dir : Path or None
        The compilers slice the guard reads, or ``None`` before start-up has
        established it.
    guard : kache_guard.Strays
        The reading the guard keeps, so a test can assert what a launch saw.
    running : dict[str, object]
        The groups whose scopes admission holds, as the guard's coverage.
    """

    order: list[str]
    placed: object | None
    terminate: cabc.Callable[[str], None] = dc.field(default=lambda _group_id: None)
    settings: SupervisorConfig = dc.field(default_factory=SupervisorConfig)
    backend: CacheBackend = dc.field(
        default_factory=lambda: ManagedSccache("x.service", Path("/x.sock"))
    )
    slice_dir: Path | None = None
    guard: kache_guard.Strays = dc.field(default_factory=kache_guard.Strays)
    running: dict[str, object] = dc.field(default_factory=dict)

    def place(self, entry: Pending, group_id: str, *, cached: bool) -> object | None:
        """Report the scripted placement outcome, admitting ``entry`` on success.

        Parameters
        ----------
        entry : Pending
            The request being placed. It is admitted to ``group_id`` only when
            the scripted outcome is a success, so a failed placement leaves the
            request's group unset exactly as the real registry does.
        group_id : str
            The identifier the group would be placed under.
        cached : bool
            Whether the group was composed with a cache.

        Returns
        -------
        object or None
            The scripted placement result.
        """
        del cached
        if self.placed is not None:
            entry.group_id = group_id
        return self.placed

    def cache_notice(self, entry: Pending, *, cached: bool) -> str:
        """Record the cache-notice effect for direct-cache scenarios.

        Parameters
        ----------
        entry : Pending
            The request being admitted.
        cached : bool
            Whether the group was composed with a cache.

        Returns
        -------
        str
            Always empty: a scripted group has no notice to give.
        """
        del entry, cached
        self.order.append("cache_notice")
        return ""

    def abandon_held(self, running: Running) -> None:
        """Record that a placed child was abandoned before release.

        Parameters
        ----------
        running : Running
            The placed group whose requester had already gone.
        """
        running.exec_error = None
        self.order.append("abandon")

    def release(self, running: object) -> None:
        """Record that the group was released.

        The return value is ``None`` rather than ``False``, because the caller
        treats only an explicit ``False`` as a release failure and a scripted
        double has none of the real registry's failure modes.

        Parameters
        ----------
        running : object
            The placed group being released.
        """
        del running
        self.order.append("release")
