"""Build the cache backend a configured mode names.

This is the composition root for cache adapters, kept out of
:mod:`buildlimits.supervisor` so that adding a backend is one table entry and
one branch here rather than another layer in the event loop's own module.

Two objects come out, because two callers need different things:

- ``composition`` is what admission asks for argv, environment and capability.
  It is the backend the mode names, or :class:`DirectCache` for ``direct``.
- ``managed`` is the sccache adapter, always built. Only a detached mode acts
  on it -- the drain, quiesce and recovery paths -- and those paths are
  unreachable for an in-scope backend, so an unselected adapter is inert.

See ``docs/adr-005-kache-cache-backend.md``.
"""

import dataclasses as dc
import typing as typ

from buildlimits.cache_backend import CacheBackend, DirectCache
from buildlimits.cache_modes import is_cache
from buildlimits.kache import Kache
from buildlimits.sccache import ManagedSccache

if typ.TYPE_CHECKING:
    from buildlimits.config import SupervisorConfig
    from buildlimits.systemd import UserManager


@dc.dataclass(frozen=True, slots=True)
class Backends:
    """The adapters one configuration selects.

    Attributes
    ----------
    composition : CacheBackend
        The backend that composes admitted compiler groups and answers health.
    managed : ManagedSccache
        The managed sccache adapter. It is built in every mode, because the
        recovery machine is wired to it unconditionally, but it is only acted
        on when the configured mode is detached.
    """

    composition: CacheBackend
    managed: ManagedSccache


def select(settings: SupervisorConfig, manager: UserManager) -> Backends:
    """Build the adapters for a configuration's ``cache_mode``.

    Parameters
    ----------
    settings : SupervisorConfig
        The supervisor's settings.
    manager : UserManager
        The user-manager port the sccache adapter's control verbs use.

    Returns
    -------
    Backends
        The composition backend and the managed sccache adapter.

    Examples
    --------
    >>> from buildlimits.config import SupervisorConfig
    >>> settings = SupervisorConfig(cache_mode="direct")
    >>> built = select(settings, typ.cast("UserManager", object()))
    >>> built.composition.name, built.managed.name
    ('direct', 'sccache')
    """
    managed = ManagedSccache(
        settings.sccache_unit,
        settings.sccache_socket,
        manager,
        executable=settings.sccache_executable,
    )
    if not is_cache(settings.cache_mode):
        return Backends(DirectCache(), managed)
    if settings.cache_mode == "kache":
        kache = Kache(
            settings.kache_executable,
            socket_path=settings.kache_socket,
            cache_dir=settings.kache_cache_dir,
        )
        return Backends(kache, managed)
    return Backends(managed, managed)
