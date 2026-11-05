"""Report kache processes that no admitted compiler scope covers.

kache v2.0.0-rc.1 starts its daemon from inside the client when no daemon is
listening: the wrapper forks ``kache-engine daemon run`` and becomes its
parent. That process stays in the group's scope after the compile ends, and
the scope is the supervisor's unit of accounting -- a token returned for an
emptied scope would cover work that is still running.

ADR 006 therefore requires an already-running, separately managed daemon, and
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
from pathlib import Path

from buildlimits import cgroups
from buildlimits.cache_modes import is_cache
from buildlimits.journal import log

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.groups import Groups

#: Names that identify a kache process outside an admitted compiler scope.
#: The v2 client ``exec``s ``kache-engine`` in place, so a snapshot may see
#: either executable at the wrapper's process identifier.
STRAY_EXECUTABLES = frozenset({"kache", "kache-engine"})


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

    The whole slice is read and the members of the scopes admission still
    holds are then subtracted from that reading. Reading the named scopes
    instead would miss a kache process in a place the supervisor does not
    name: a transient service, another harness's scope, or a slice nested
    inside this one. Those units are not covered by any token either, so a
    process inside them is exactly what this guard exists to report.

    A scope admission has forgotten is not covered even though it is still in
    the slice: by then its token has been accounted for, so anything still
    running there outlived the group that owned it. The same is true of a
    process that has left its group's scope for another unit in the slice --
    it is no longer under the scope whose token would cover it.

    Coverage is read from the admitted scope directories rather than from the
    slice reading, so a process that merely happens to sit in the same
    directory is still covered only while its group is held.

    Parameters
    ----------
    slice_dir : Path
        The compilers slice's directory.
    admitted : Iterable[Path]
        The scope directories of the groups admission currently holds.

    Returns
    -------
    tuple[int, ...]
        The uncovered kache process identifiers, in path order.
    """
    covered: set[int] = set()
    for scope in admitted:
        covered.update(_members(scope))
    return stray_processes(_slice_members(slice_dir), admitted=covered)


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


def _slice_members(slice_dir: Path) -> list[int]:
    """List every process in the slice, or none when it cannot be read.

    Parameters
    ----------
    slice_dir : Path
        The compilers slice's directory.

    Returns
    -------
    list[int]
        Every process identifier under the slice, in path order. See
        :func:`_or_nothing` for why an unreadable slice is no finding.
    """
    return _or_nothing(cgroups.slice_member_pids, slice_dir)


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
