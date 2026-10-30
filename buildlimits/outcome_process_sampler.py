"""Bounded `/proc` sampling for the managed cache server's compiler children."""

import os
import typing as typ
from pathlib import Path

from buildlimits import cgroups, outcome_sightings, proc_trees
from buildlimits.outcome_ports import SampleFailure

if typ.TYPE_CHECKING:
    from buildlimits.outcome_attribution import Generation

MAX_PROCESSES_PER_SAMPLE = 64
_MAX_CMDLINE_BYTES = 128 * 1024


class ProcessSampler:
    """Cache one server group and bounded process classifications per generation."""

    def __init__(
        self,
        proc_root: Path = Path("/proc"),
        cgroup_root: Path = cgroups.CGROUP_ROOT,
    ) -> None:
        """Retain injectable filesystem roots for deterministic tests."""
        self._proc_root = proc_root
        self._cgroup_root = cgroup_root
        self._generation: Generation | None = None
        self._group_dir: Path | None = None
        self._classified: dict[tuple[int, int], str | None] = {}

    def full_emit_metadata(
        self, generation: Generation
    ) -> frozenset[str] | SampleFailure:
        """Read up to 64 process command lines and return full-emit metadata.

        Every reported value belongs to a process whose identity this sample
        re-validated. Cached classifications are still reused, but a value is
        never carried over from an earlier sample merely because it was cached
        then: a process that has become unreadable, or that falls outside this
        sample's bounded batch, contributes nothing to the result. O12 counts a
        classification only when it was validated while the entry ran, so a
        stale cache hit must not be reported as a fresh sighting.

        Returns
        -------
        frozenset[str] | SampleFailure
            In-memory metadata values or one closed failure reason.
        """
        try:
            group_dir = self._control_group(generation)
        except OSError, ValueError:
            return SampleFailure("group_unreadable")
        if group_dir is None:
            return SampleFailure("server_unknown")
        try:
            members = set(cgroups.member_pids(group_dir))
        except OSError, ValueError:
            return SampleFailure("group_unreadable")

        self._forget_departed(members)
        metadata: set[str] = set()
        for pid in sorted(members)[:MAX_PROCESSES_PER_SAMPLE]:
            value = self._sample(pid)
            if value is not None:
                metadata.add(value)
        return frozenset(metadata)

    def _forget_departed(self, members: set[int]) -> None:
        """Drop cached classifications whose process left the group."""
        self._classified = {
            identity: metadata
            for identity, metadata in self._classified.items()
            if identity[0] in members
        }

    def _forget_pid(self, pid: int, *, keep: tuple[int, int] | None = None) -> None:
        """Drop cached classifications for one pid, except one live identity."""
        self._classified = {
            known: metadata
            for known, metadata in self._classified.items()
            if known[0] != pid or known == keep
        }

    def _sample(self, pid: int) -> str | None:
        """Classify one member and report only what this sample validated.

        The cache still saves a repeated command-line read, but it is keyed by
        the identity this sample just re-read. A PID that has become
        unreadable, or whose start time now differs, therefore contributes
        nothing rather than an earlier classification.

        Returns
        -------
        str | None
            This sample's full-emit metadata for the process, or None when the
            process is gone, unreadable, or has no full-emit argument.
        """
        starttime = self._starttime(pid)
        if starttime is None:
            self._forget_pid(pid)
            return None
        identity = (pid, starttime)
        self._forget_pid(pid, keep=identity)
        if identity in self._classified:
            return self._classified[identity]
        metadata = self._classify(pid, starttime)
        if metadata is _PROCESS_GONE:
            return None
        value = typ.cast("str | None", metadata)
        self._classified[identity] = value
        return value

    def _control_group(self, generation: Generation) -> Path | None:
        """Resolve and cache the control group for one server identity.

        Returns
        -------
        Path | None
            The resolved directory, or None when the process has no unified
            control group path.
        """
        if self._generation != generation:
            self._generation = generation
            self._group_dir = None
            self._classified.clear()
        if self._group_dir is not None:
            return self._group_dir
        text = (self._proc_root / str(generation.pid) / "cgroup").read_text(
            encoding="utf-8"
        )
        path = cgroups.parse_unified_path(text)
        if path is None:
            return None
        self._group_dir = cgroups.directory(path, self._cgroup_root)
        return self._group_dir

    def _starttime(self, pid: int) -> int | None:
        """Read field 22, treating an exiting or reused PID as a missed sample.

        Returns
        -------
        int | None
            The process start time, or None when the process is gone or unreadable.
        """
        try:
            starttime = proc_trees.read_process_starttime(pid, self._proc_root)
        except OSError, UnicodeError, ValueError:
            return None
        return starttime

    def _classify(self, pid: int, starttime: int) -> str | _ProcessGone | None:
        """Read one bounded command line and reject a PID that changed identity.

        Returns
        -------
        str | None | _ProcessGone
            Full-emit metadata, no matching metadata, or the process-gone marker.
        """
        try:
            with (self._proc_root / str(pid) / "cmdline").open("rb") as source:
                raw = source.read(_MAX_CMDLINE_BYTES + 1)
            if len(raw) > _MAX_CMDLINE_BYTES:
                return None
            after = self._starttime(pid)
            if after != starttime:
                return _PROCESS_GONE
            argv = tuple(os.fsdecode(part) for part in raw.rstrip(b"\0").split(b"\0"))
        except FileNotFoundError, ProcessLookupError:
            return _PROCESS_GONE
        except OSError, UnicodeError:
            return None
        return outcome_sightings.full_emit_metadata(argv)


class _ProcessGone:
    """Sentinel distinguishing an exiting process from a completed no-match."""


_PROCESS_GONE = _ProcessGone()


__all__ = ["MAX_PROCESSES_PER_SAMPLE", "ProcessSampler"]
