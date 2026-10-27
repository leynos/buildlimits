"""The cache backends an admitted compiler group can be composed with.

ADR 004 splits the supported caches by one capability: whether composed work
can outlive the compiler group's own scope. sccache accepts a request and runs
the miss compilation as a direct child of its server, outside the group, so a
scope can empty while its compilation still runs. kache runs the compiler as a
child of the wrapper inside the group, so the scope empties only once the work
is over.

:class:`CacheBackend` names what admission needs to compose a group: the name
its wording uses, that capability, a health answer, and the argv and
environment changes. :class:`DetachedCacheBackend` adds the lifecycle that only
a detached backend has anything to account for -- accounting, stopping and
starting the cache. It is a separate protocol because an in-scope backend has
nothing to account for, and so that the drain and recovery paths can name the
smaller thing they actually need.

The backend-neutral exceptions are declared here too, so a caller can catch a
composition or health failure without naming one product. The admission core
catches :class:`CacheError` and never asks which backend raised it.

What a *mode string* alone can say -- its name, whether it caches, and whether
its work is detached -- lives in :mod:`buildlimits.cache_modes`, because those
readers hold no backend object and so must not depend on this module's ports.

Examples
--------
>>> direct = DirectCache()
>>> direct.name, direct.in_scope_work
('direct', True)
>>> direct.compose(("/t/rustc", "-vV"), {"PATH": "/t"})
(('/t/rustc', '-vV'), {'PATH': '/t'})
>>> direct.fallback_notice()
''
"""

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class CacheError(Exception):
    """A cache backend cannot be used, or its state cannot be determined."""


class LiveCacheWorkError(CacheError):
    """A successful manager query observes a live cache with unaccounted work.

    Parameters
    ----------
    roots : int or None
        Visible compiler-root trees at the moment of the query, or None when
        the count could not be established. The count corroborates starvation
        in the automatic recovery trigger; it never authorizes token release,
        because the dangerous wait sits before any compiler exists.
    """

    def __init__(self, message: str, *, roots: int | None = None) -> None:
        """Record the fault message and the visible root count it observed.

        Parameters
        ----------
        message : str
            The operator-facing description of the observed work.
        roots : int or None
            Visible compiler-root trees, or None when the count could not be
            established.
        """
        super().__init__(message)
        self.roots = roots


class StoppedPopulatedCacheError(CacheError):
    """A detached cache is stopped but its control group still holds work.

    The cgroup need not name the process that holds the retained work; a
    member may simply be slow to exit. Either way the stopped state cannot
    account for it, so the diagnosis is separate from both a live cache and a
    manager transport fault.
    """


class StartupAccountingError(CacheError):
    """Previous cache work requires operator accounting before admission starts."""


class CacheBackend(typ.Protocol):
    """What admission needs from a cache backend to compose a group.

    ``name`` and ``in_scope_work`` are declared as read-only properties rather
    than as attributes. A writable attribute member would require every
    implementation to accept writes, which a frozen dataclass does not; every
    backend answers these two questions from configuration it was built with,
    so nothing ever assigns them.

    Attributes
    ----------
    name : str
        The backend's name, for the journal and status wording.
    in_scope_work : bool
        Whether every compilation the backend accepts runs inside the
        compiler group's own scope. ``False`` means accepted work may outlive
        the scope, which is what requires token retention and the detached
        lifecycle.
    """

    @property
    def name(self) -> str:
        """The backend's name, for the journal and status wording."""
        ...

    @property
    def in_scope_work(self) -> bool:
        """Whether every compilation the backend accepts runs in the scope."""
        ...

    def is_reachable(self) -> bool:
        """Report whether the backend can accept a compilation right now.

        Returns
        -------
        bool
            Whether a new group may be composed with it.

        Raises
        ------
        CacheError
            If the probe fails for a reason other than the backend simply not
            serving, so a misconfiguration is not mistaken for a stopped
            cache.
        """
        ...

    def compose(
        self, argv: tuple[str, ...], env: cabc.Mapping[str, str]
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        """Compose a compiler invocation with this backend.

        Parameters
        ----------
        argv : tuple[str, ...]
            The bare compiler invocation, with any caller-supplied cache layer
            already stripped.
        env : Mapping[str, str]
            The requesting client's environment, forwarded apart from what
            the backend adds.

        Returns
        -------
        tuple[tuple[str, ...], dict[str, str]]
            The argument vector and environment the group runs.
        """
        ...

    def fallback_notice(self) -> str:
        """Explain an uncached group that the configuration meant to cache.

        The caller has already established that the configuration asked for a
        cache and that this request may use one, so a backend only has to say
        what its own absence costs the build. Rustdoc and configured direct
        mode are therefore answered upstream, once, rather than by every
        backend repeating the same gate.

        Returns
        -------
        str
            A line for the build's standard error, or an empty string for a
            backend that has nothing to explain.
        """
        ...


class DetachedCacheBackend(CacheBackend, typ.Protocol):
    """A backend whose accepted work may outlive the compiler group's scope.

    Only a backend with work outside the group has anything to account for, so
    only a detached backend has these members. Admission never calls them; the
    drain, quiesce and recovery paths do, and they hold the one adapter the
    composition root built.

    Their presence says nothing about whether the backend is healthy. A
    stopped, empty cache accounts for zero work, which is what
    :meth:`accounted_drain` reports.
    """

    def accounted_drain(self) -> int:
        """Report how much accepted work can no longer start outside a scope.

        Returns
        -------
        int
            Zero only when the backend has provably no detached work left.
        """
        ...

    def stop(self) -> None:
        """Ask the backend to stop accepting work."""
        ...

    def start(self) -> None:
        """Ask the backend to start accepting work again."""
        ...


class DirectCache:
    """The absence of a cache: the compiler runs as it was invoked.

    This is a backend so that every composition site has one object to ask,
    and so that "no cache" needs no separate branch downstream.

    Its three protocol methods take no ``self`` facts, which is the point: a
    composition site asks the same question of any backend and a direct group
    is the answer that changes nothing. They stay instance methods so the
    protocol is one interface rather than a special case at each call site.

    Attributes
    ----------
    name : str
        Always ``direct``.
    in_scope_work : bool
        Always ``True``: an unwrapped compiler runs inside the group's scope.
    """

    name = "direct"
    in_scope_work = True

    def is_reachable(self) -> bool:  # ruff: ignore[no-self-use] - protocol member.
        """Answer ``True``, because there is no endpoint that could refuse.

        Returns
        -------
        bool
            Always ``True``.
        """
        return True

    def compose(  # ruff: ignore[no-self-use] - a CacheBackend protocol member.
        self, argv: tuple[str, ...], env: cabc.Mapping[str, str]
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        """Return the compiler invocation and environment unchanged.

        Returns
        -------
        tuple[tuple[str, ...], dict[str, str]]
            The argument vector and an unmodified copy of the environment.
        """
        return argv, dict(env)

    def fallback_notice(self) -> str:  # ruff: ignore[no-self-use] - protocol member.
        """Explain nothing, because no cache was configured to fall back from.

        Returns
        -------
        str
            Always an empty string.
        """
        return ""
