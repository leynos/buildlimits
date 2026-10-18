"""The restart barrier's expected fate, and the ways its file calls fail.

The restart barrier is a plain file on disk, and the managed unit's own
``ConditionPathExists=!`` reads it, so its state survives a supervisor restart
and its I/O is as failure-prone as any other file. :class:`BarrierFate` states
what a policy must leave behind, and the two injectors below make one barrier
path unreadable or unremovable so a test can drive the failure branches that a
healthy temporary directory never reaches.

Each injector replaces exactly one ``Path`` method and guards exactly one
path, so a test's other file operations keep their ordinary behaviour.
"""

import dataclasses as dc
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import os

    import pytest


@dc.dataclass(frozen=True, slots=True)
class BarrierFate:
    """What one recovery policy leaves behind after a verified accounting.

    Attributes
    ----------
    remaining : bool
        Whether the restart barrier still exists.
    starts : bool
        Whether the managed cache was asked to start.
    """

    remaining: bool
    starts: bool


def unreadable_barrier(
    monkeypatch: pytest.MonkeyPatch, marker: Path, fault: str = "Permission denied"
) -> None:
    """Make one barrier path fail inspection, as an unreadable parent does.

    An unreadable path is not an absent one: the answer is unknown rather than
    negative. A runtime directory whose permissions have been tightened, or
    which has been unmounted, is exactly that case. This stands in for the real
    error so the reclaim path can be shown to treat an unknown barrier as one
    it must not act behind, rather than as an absent barrier it may start on
    top of.

    ``stat`` is the method guarded here, not ``exists``: from Python 3.14
    ``Path.exists`` answers ``False`` for every ``OSError``, so patching it
    would only re-test the fail-open reading the reclaim path no longer uses.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to replace the path class's ``stat``.
    marker : Path
        The barrier path whose inspection fails.
    fault : str
        The operating-system error the failed inspection stands for.
    """
    stat = Path.stat

    def refuse(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        """Fail inspection of the guarded barrier and answer normally otherwise.

        Returns
        -------
        os.stat_result
            The status of an unguarded path.

        Raises
        ------
        OSError
            For the guarded barrier only, standing in for a parent directory
            the process may not read.
        """
        if self == marker:
            raise OSError(fault)
        return stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, "stat", refuse)


def unremovable_barrier(
    monkeypatch: pytest.MonkeyPatch, marker: Path, fault: str = "Stale file handle"
) -> None:
    """Make one barrier path refuse removal, as an unwritable runtime dir does.

    Removing the restart barrier needs write permission on its parent
    directory as well as the file, so a runtime directory that has become
    read-only or whose filesystem has gone away fails the unlink with
    ``OSError`` while leaving the barrier in place — and the barrier is what
    the unit's ``ConditionPathExists=!`` reads. The scripted ``Path`` answers
    every other method as before; only ``unlink`` refuses.

    Parameters
    ----------
    monkeypatch : pytest.MonkeyPatch
        Fixture used to replace the path class's ``unlink``.
    marker : Path
        The rewrite this guard refuses to remove.
    fault : str
        The operating-system error the guard raises.
    """
    unlink = Path.unlink

    def refuse(self: Path, *, missing_ok: bool = False) -> None:
        """Refuse to remove the guarded barrier and act normally otherwise.

        Raises
        ------
        OSError
            For the guarded barrier only, standing in for a runtime
            directory that has become unwritable.
        """
        if self == marker:
            raise OSError(fault)
        unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse)
