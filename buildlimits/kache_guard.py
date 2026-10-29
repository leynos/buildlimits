"""Report kache processes that no admitted compiler scope covers.

kache v2.0.0-rc.1 starts its daemon from inside the client when no daemon is
listening: the wrapper forks ``kache-engine daemon run`` and becomes its
parent. That process stays in the group's scope after the compile ends, and
the scope is the supervisor's unit of accounting -- a token returned for an
emptied scope would cover work that is still running.

ADR 005 therefore requires an already-running, separately managed daemon, and
this module is the guard behind that requirement. It is a *report*, not a
killer. A stray process inside a group's scope is removed by the ordinary
terminate-on-leader-exit path; a stray process outside every scope is the
operator's to remove, and status names it. Nothing here ever holds or returns
a token.

The executable test is kache's, because kache is the only in-scope cache the
supervisor composes. A future in-scope backend needs its own test here, which
is why the gate is a capability rather than a mode name.
"""

import contextlib
import dataclasses as dc
import typing as typ

from buildlimits import cgroups, kache
from buildlimits.cache_modes import is_cache
from buildlimits.journal import log

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from buildlimits.groups import Groups


@dc.dataclass(slots=True)
class Strays:
    """The kache processes last found outside every admitted scope.

    The reading is taken at admission, on the cache poll and whenever status is
    asked for, and kept here so that status can report it. Only a *change* is
    journalled, so a daemon that stays behind is named once rather than on
    every poll.

    Attributes
    ----------
    pids : tuple[int, ...]
        The process identifiers of the last reading, empty when the guard
        found nothing.
    """

    pids: tuple[int, ...] = ()
    _journaled: tuple[int, ...] = ()

    def describe(self) -> list[int]:
        """Return the last reading as a JSON-compatible list.

        Returns
        -------
        list[int]
            The stray process identifiers, empty when there are none.

        Examples
        --------
        >>> Strays((4242,)).describe()
        [4242]
        >>> Strays().describe()
        []
        """
        return list(self.pids)

    def observe(
        self, slice_dir: Path | None, admitted: cabc.Iterable[Path]
    ) -> tuple[int, ...]:
        """Take a reading and keep it, without journalling anything.

        Status calls this so that an operator asking for the document gets a
        current answer. It deliberately leaves the journalled baseline alone:
        a reading taken to answer one status query must not silence the
        journal line the next poll owes for the same stray.

        Parameters
        ----------
        slice_dir : Path | None
            The compilers slice, or ``None`` before the supervisor has passed
            its restart barrier, when there is nothing to read yet.
        admitted : Iterable[Path]
            The scope directories of the groups admission currently holds.

        Returns
        -------
        tuple[int, ...]
            The uncovered kache process identifiers, in scope-name order.
        """
        found = () if slice_dir is None else find(slice_dir, admitted)
        self.pids = found
        return found

    def refresh(self, slice_dir: Path | None, admitted: cabc.Iterable[Path]) -> None:
        """Take a reading and journal a stray appearing or clearing.

        Parameters
        ----------
        slice_dir : Path | None
            The compilers slice, or ``None`` before the supervisor has passed
            its restart barrier, when there is nothing to read yet.
        admitted : Iterable[Path]
            The scope directories of the groups admission currently holds.
        """
        found = self.observe(slice_dir, admitted)
        if found == self._journaled:
            return
        previously = self._journaled
        self._journaled = found
        if found or previously:
            with contextlib.suppress(OSError):
                log(_line(found))


def guarded(groups: Groups) -> bool:
    """Report whether this configuration can start a cache process in a scope.

    Only an in-scope cache is composed from inside an admitted group, so only
    it can leave a daemon behind there. ``direct`` caches nothing and the
    detached backend's work lives in its own service, so both skip the read.

    Parameters
    ----------
    groups : Groups
        The group registry holding the settings and the composed backend.

    Returns
    -------
    bool
        ``True`` when the slice must be read for stray cache processes.
    """
    return is_cache(groups.settings.cache_mode) and groups.backend.in_scope_work


def refresh_guard(groups: Groups) -> None:
    """Read the slice again when this configuration is guarded.

    Parameters
    ----------
    groups : Groups
        The group registry holding the settings, the backend and the reading.
    """
    if guarded(groups):
        groups.guard.refresh(groups.slice_dir, admitted_scopes(groups))


def admitted_scopes(groups: Groups) -> list[Path]:
    """List the scopes admission still holds, which the guard treats as covered.

    Parameters
    ----------
    groups : Groups
        The group registry.

    Returns
    -------
    list[Path]
        The scope directory of every group whose scope has not yet emptied.
    """
    return [running.scope_dir for running in groups.running.values()]


def find(slice_dir: Path, admitted: cabc.Iterable[Path]) -> tuple[int, ...]:
    """Find kache processes in the slice that no admitted scope covers.

    Every supervisor-created scope is read, and the members of the scopes
    admission still holds are treated as covered. A scope admission has
    forgotten is not covered even though it is still in the slice: by then its
    token has been accounted for, so anything still running there outlived the
    group that owned it.

    Parameters
    ----------
    slice_dir : Path
        The compilers slice's directory.
    admitted : Iterable[Path]
        The scope directories of the groups admission currently holds.

    Returns
    -------
    tuple[int, ...]
        The uncovered kache process identifiers, in scope-name order.
    """
    covered: set[int] = set()
    for scope in admitted:
        covered.update(_members(scope))
    candidates: list[int] = []
    for scope in _child_scopes(slice_dir):
        candidates.extend(_members(scope))
    return kache.stray_processes(candidates, admitted=covered)


def _or_nothing[T](read: cabc.Callable[[Path], list[T]], path: Path) -> list[T]:
    """Read one control-group directory, or none when it cannot be read.

    The slice and its scopes can both disappear between the check that they
    exist and the read that follows, which raises ``OSError``. This guard only
    reports, so an unreadable directory is treated exactly as a missing one:
    as nothing found. Raising would turn a diagnostic into a poll fault and,
    on the status path, into a failed status query, for a reading that can
    only ever add a line to the journal.

    Parameters
    ----------
    read : Callable[[Path], list[T]]
        The control-group reading to take.
    path : Path
        The directory to read.

    Returns
    -------
    list[T]
        What the reading found, empty when the directory has gone.
    """
    try:
        return read(path)
    except OSError:
        return []


def _child_scopes(slice_dir: Path) -> list[Path]:
    """List the slice's scopes, or none when the slice cannot be read.

    Parameters
    ----------
    slice_dir : Path
        The compilers slice's directory.

    Returns
    -------
    list[Path]
        The scope directories, empty when the slice has gone. See
        :func:`_or_nothing` for why an unreadable slice is no finding.
    """
    return _or_nothing(cgroups.child_scopes, slice_dir)


def _members(scope_dir: Path) -> list[int]:
    """List a scope's processes, or none when the scope cannot be read.

    Parameters
    ----------
    scope_dir : Path
        The scope's control-group directory.

    Returns
    -------
    list[int]
        The process identifiers, empty when the scope has gone. An unreadable
        scope is one the drain path is already terminating.
    """
    return _or_nothing(cgroups.member_pids, scope_dir)


def _line(found: tuple[int, ...]) -> str:
    """Word the journal line for a reading.

    Parameters
    ----------
    found : tuple[int, ...]
        The reading; empty means a previously reported process is gone.

    Returns
    -------
    str
        The journal line.

    Examples
    --------
    >>> _line(())
    'no stray kache process remains outside the admitted scopes'
    >>> _line((4242,))
    'stray kache process outside admitted scopes: 4242; no token covers it; remove it'
    """
    if found:
        listed = ", ".join(str(pid) for pid in found)
        return (
            f"stray kache process outside admitted scopes: {listed}; "
            "no token covers it; remove it"
        )
    return "no stray kache process remains outside the admitted scopes"
