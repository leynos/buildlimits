"""The managed-server process sampler is bounded and PID-reuse safe."""

import errno
import selectors
import typing as typ
from pathlib import Path
from unittest import mock

from buildlimits import cgroups
from buildlimits.outcome_attribution import Generation
from buildlimits.outcome_ports import GenerationFailure, SampleFailure
from buildlimits.outcome_process_sampler import MAX_PROCESSES_PER_SAMPLE, ProcessSampler
from buildlimits.outcome_queries import PeerServerProcess

if typ.TYPE_CHECKING:
    import pytest


def _proc_stat(pid: int, starttime: int) -> str:
    """Build the fixed stat prefix through Linux field 22."""
    fields = ["S", *("0" for _ in range(18)), str(starttime)]
    return f"{pid} (fake compiler) {' '.join(fields)}\n"


def _write_process(proc_root: Path, pid: int, starttime: int, metadata: str) -> None:
    """Create one fake proc entry with a full-emit rustc command line."""
    process = proc_root / str(pid)
    process.mkdir(parents=True, exist_ok=True)
    (process / "stat").write_text(_proc_stat(pid, starttime), encoding="ascii")
    (process / "cmdline").write_bytes(
        f"rustc\0--emit=link\0-Cmetadata={metadata}\0".encode()
    )


def _write_server(proc_root: Path, pid: int, group: str) -> None:
    """Point one fake server process at a unified control group."""
    server = proc_root / str(pid)
    server.mkdir(parents=True, exist_ok=True)
    (server / "cgroup").write_text(f"0::{group}\n", encoding="utf-8")


def _write_group(cgroup_root: Path, group: str, pids: list[int]) -> Path:
    """Create one group membership file and return its filesystem directory."""
    directory = cgroup_root / group.removeprefix("/")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cgroup.procs").write_text(
        "".join(f"{pid}\n" for pid in pids), encoding="ascii"
    )
    return directory


def test_peer_server_returns_metadata_from_its_cached_server_group(
    tmp_path: Path,
) -> None:
    """The generation PID selects one cgroup whose children are sampled."""
    proc_root, cgroup_root = tmp_path / "proc", tmp_path / "cgroup"
    _write_server(proc_root, 900, "/user.slice/sccache.service")
    _write_group(cgroup_root, "/user.slice/sccache.service", [101])
    _write_process(proc_root, 101, 33, "meta-a")
    server = PeerServerProcess(
        tmp_path / "socket", proc_root=proc_root, cgroup_root=cgroup_root
    )
    generation = Generation("run-a", 900, 12)

    with mock.patch.object(
        server,
        "generation",
        side_effect=(generation, generation, Generation("run-b", 900, 13)),
    ):
        assert server.full_emit_metadata() == frozenset({"meta-a"}), (
            "the cached peer group remains stable within one generation"
        )
        (proc_root / "900" / "cgroup").write_text(
            "0::/user.slice/other.service\n", encoding="utf-8"
        )
        assert server.full_emit_metadata() == frozenset({"meta-a"}), (
            "the group directory remains cached for one generation"
        )
        next_group = _write_group(cgroup_root, "/user.slice/other.service", [102])
        _write_process(proc_root, 102, 34, "meta-b")
        assert next_group.exists(), "the new peer group directory exists"
        assert server.full_emit_metadata() == frozenset({"meta-b"}), (
            "a new generation resolves a fresh group"
        )


def test_generation_does_not_wait_for_a_pending_socket_connection(
    tmp_path: Path,
) -> None:
    """An in-progress Unix connection fails closed without selector polling."""
    server = PeerServerProcess(tmp_path / "socket", proc_root=tmp_path / "proc")
    peer = mock.MagicMock()
    peer.__enter__.return_value = peer
    peer.connect_ex.return_value = errno.EINPROGRESS

    with (
        mock.patch("buildlimits.outcome_queries.socket.socket", return_value=peer),
        mock.patch.object(selectors, "DefaultSelector") as selector,
    ):
        result = server.generation()

    assert result == GenerationFailure("generation_unavailable"), (
        "an unfinished peer connection is unavailable immediately"
    )
    selector.assert_not_called()
    peer.getsockopt.assert_not_called()


def test_sample_inspects_no_more_than_sixty_four_processes(tmp_path: Path) -> None:
    """The first bounded batch is the only process list inspected per sample."""
    proc_root, cgroup_root = tmp_path / "proc", tmp_path / "cgroup"
    _write_server(proc_root, 900, "/sccache")
    pids = list(range(1, MAX_PROCESSES_PER_SAMPLE + 2))
    _write_group(cgroup_root, "/sccache", pids)
    for pid in pids:
        _write_process(proc_root, pid, pid + 100, f"meta-{pid}")
    sampler = ProcessSampler(proc_root, cgroup_root)

    observed = sampler.full_emit_metadata(Generation("run-a", 900, 12))

    assert isinstance(observed, frozenset), "the group was readable"
    assert len(observed) == MAX_PROCESSES_PER_SAMPLE, "the hard inspection bound holds"
    assert "meta-65" not in observed, "the process after the first batch was untouched"


def test_classification_cache_is_keyed_by_starttime_and_prunes_departures(
    tmp_path: Path,
) -> None:
    """A reused PID is re-read and a process that leaves is forgotten."""
    proc_root, cgroup_root = tmp_path / "proc", tmp_path / "cgroup"
    _write_server(proc_root, 900, "/sccache")
    group = _write_group(cgroup_root, "/sccache", [101])
    process = proc_root / "101"
    _write_process(proc_root, 101, 33, "old")
    sampler = ProcessSampler(proc_root, cgroup_root)
    generation = Generation("run-a", 900, 12)

    assert sampler.full_emit_metadata(generation) == frozenset({"old"}), (
        "classification_cache_is_key...: sampler.full_emit_metadata(generatio"
    )
    (process / "cmdline").write_bytes(b"rustc\0--emit=link\0-Cmetadata=ignored\0")
    assert sampler.full_emit_metadata(generation) == frozenset({"old"}), (
        "one PID/start-time pair is classified only once"
    )

    (process / "stat").write_text(_proc_stat(101, 34), encoding="ascii")
    (process / "cmdline").write_bytes(b"rustc\0--emit=link\0-Cmetadata=new\0")
    assert sampler.full_emit_metadata(generation) == frozenset({"new"}), (
        "a reused PID with a different start time gets a fresh classification"
    )

    (group / "cgroup.procs").write_text("", encoding="ascii")
    assert sampler.full_emit_metadata(generation) == frozenset(), (
        "a process that left the cgroup is removed from the classification cache"
    )


def test_cached_process_outside_the_inspected_batch_is_not_reported(
    tmp_path: Path,
) -> None:
    """A cached classification the current batch skips is not carried over."""
    proc_root, cgroup_root = tmp_path / "proc", tmp_path / "cgroup"
    _write_server(proc_root, 900, "/sccache")
    pids = list(range(100, 100 + MAX_PROCESSES_PER_SAMPLE))
    group = _write_group(cgroup_root, "/sccache", pids)
    for pid in pids:
        _write_process(proc_root, pid, pid + 100, f"meta-{pid}")
    sampler = ProcessSampler(proc_root, cgroup_root)
    generation = Generation("run-a", 900, 12)

    first = sampler.full_emit_metadata(generation)

    assert isinstance(first, frozenset), "the measured group was readable"
    assert len(first) == MAX_PROCESSES_PER_SAMPLE, "the whole first batch counts"

    last_pid = pids[-1]
    _write_process(proc_root, 99, 0, "meta-99")
    group.joinpath("cgroup.procs").write_text(
        "".join(f"{pid}\n" for pid in [99, *pids]), encoding="ascii"
    )
    second = sampler.full_emit_metadata(generation)

    assert isinstance(second, frozenset), "the measured group was still readable"
    assert "meta-99" in second, "an earlier member is inspected first"
    assert f"meta-{last_pid}" not in second, (
        "a cached classification outside this batch is a stale sighting"
    )
    assert len(second) == MAX_PROCESSES_PER_SAMPLE, "only inspected members count"


def test_unreadable_cached_process_is_not_reported_from_stale_cache(
    tmp_path: Path,
) -> None:
    """A process that stops being readable drops out of the current sample."""
    proc_root, cgroup_root = tmp_path / "proc", tmp_path / "cgroup"
    _write_server(proc_root, 900, "/sccache")
    _write_group(cgroup_root, "/sccache", [101])
    _write_process(proc_root, 101, 33, "meta-a")
    sampler = ProcessSampler(proc_root, cgroup_root)
    generation = Generation("run-a", 900, 12)

    assert sampler.full_emit_metadata(generation) == frozenset({"meta-a"}), (
        "the readable process is classified once"
    )

    stat = proc_root / "101" / "stat"
    stat.rename(stat.with_suffix(".hidden"))
    assert sampler.full_emit_metadata(generation) == frozenset(), (
        "an unreadable process is not reported from its cached classification"
    )

    stat.with_suffix(".hidden").rename(stat)
    (proc_root / "101" / "cmdline").write_bytes(
        b"rustc\0--emit=link\0-Cmetadata=meta-b\0"
    )
    assert sampler.full_emit_metadata(generation) == frozenset({"meta-b"}), (
        "readability is revalidated before the cache may be trusted again"
    )


def test_process_that_exits_during_cmdline_read_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A vanished process is not turned into a server or group failure."""
    proc_root, cgroup_root = tmp_path / "proc", tmp_path / "cgroup"
    _write_server(proc_root, 900, "/sccache")
    _write_group(cgroup_root, "/sccache", [101])
    _write_process(proc_root, 101, 33, "meta-a")
    sampler = ProcessSampler(proc_root, cgroup_root)
    starttimes = iter((33, None))
    monkeypatch.setattr(sampler, "_starttime", lambda _pid: next(starttimes))

    assert sampler.full_emit_metadata(Generation("run-a", 900, 12)) == frozenset(), (
        "process_that_exits_during_c...: sampler.full_emit_metadata(Generatio"
    )


def test_unreadable_group_and_unknown_server_have_closed_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The port returns one fixed reason for each failed sampling boundary."""
    proc_root, cgroup_root = tmp_path / "proc", tmp_path / "cgroup"
    _write_server(proc_root, 900, "/sccache")
    _write_group(cgroup_root, "/sccache", [])
    sampler = ProcessSampler(proc_root, cgroup_root)
    monkeypatch.setattr(cgroups, "member_pids", mock.Mock(side_effect=PermissionError))
    assert sampler.full_emit_metadata(Generation("run-a", 900, 12)) == SampleFailure(
        "group_unreadable"
    ), "an unreadable group returns its closed failure reason"

    (proc_root / "900" / "cgroup").write_text(
        "1:name=legacy:/sccache\n", encoding="utf-8"
    )
    peer = PeerServerProcess(
        tmp_path / "socket", proc_root=proc_root, cgroup_root=cgroup_root
    )
    with mock.patch.object(peer, "generation", return_value=None):
        assert peer.full_emit_metadata() == SampleFailure("server_unknown"), (
            "an unknown peer returns its closed failure reason"
        )
