"""Build the fake ``/proc`` and cgroup trees the sampler tests read."""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_process_sampler import ProcessSampler

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


@dc.dataclass(frozen=True, slots=True)
class SamplerTree:
    """A fake server group and the sampler that reads it."""

    sampler: ProcessSampler
    proc_root: Path
    group: Path


def sampler_tree(tmp_path: Path, pids: cabc.Sequence[int]) -> SamplerTree:
    """Create a fake server group holding ``pids`` and a sampler over it.

    Parameters
    ----------
    tmp_path : Path
        Root below which the fake ``/proc`` and cgroup trees are created.
    pids : cabc.Sequence[int]
        Process identifiers listed in the server's control group.

    Returns
    -------
    SamplerTree
        The sampler, the fake ``/proc`` root and the group membership file.
    """
    proc_root, cgroup_root = tmp_path / "proc", tmp_path / "cgroup"
    write_server(proc_root, 900, "/sccache")
    group = write_group(cgroup_root, "/sccache", list(pids))
    for pid in pids:
        write_process(proc_root, pid, pid + 100, f"meta-{pid}")
    return SamplerTree(ProcessSampler(proc_root, cgroup_root), proc_root, group)


def proc_stat_text(pid: int, starttime: int) -> str:
    """Build the fixed stat prefix through Linux field 22.

    Parameters
    ----------
    pid : int
        The process identifier the fake entry describes.
    starttime : int
        The value placed in field 22, the process start time.

    Returns
    -------
    str
        One stat file body ending in a newline.
    """
    fields = ["S", *("0" for _ in range(18)), str(starttime)]
    return f"{pid} (fake compiler) {' '.join(fields)}\n"


def write_process(proc_root: Path, pid: int, starttime: int, metadata: str) -> None:
    """Create one fake proc entry with a full-emit rustc command line.

    Parameters
    ----------
    proc_root : Path
        Root of the fake ``/proc`` tree.
    pid : int
        Process identifier to create beneath that root.
    starttime : int
        Start time recorded in the fake stat file.
    metadata : str
        Value for the ``-Cmetadata=`` argument.
    """
    process = proc_root / str(pid)
    process.mkdir(parents=True, exist_ok=True)
    (process / "stat").write_text(proc_stat_text(pid, starttime), encoding="ascii")
    (process / "cmdline").write_bytes(
        f"rustc\0--emit=link\0-Cmetadata={metadata}\0".encode()
    )


def write_server(proc_root: Path, pid: int, group: str) -> None:
    """Point one fake server process at a unified control group.

    Parameters
    ----------
    proc_root : Path
        Root of the fake ``/proc`` tree.
    pid : int
        Process identifier of the server whose group is resolved.
    group : str
        Absolute cgroup v2 path written into the server's cgroup file.
    """
    server = proc_root / str(pid)
    server.mkdir(parents=True, exist_ok=True)
    (server / "cgroup").write_text(f"0::{group}\n", encoding="utf-8")


def write_group(cgroup_root: Path, group: str, pids: list[int]) -> Path:
    """Create one group membership file and return its filesystem directory.

    Parameters
    ----------
    cgroup_root : Path
        Root of the fake cgroup tree.
    group : str
        Absolute cgroup v2 path whose membership file is written.
    pids : list[int]
        Process identifiers to list as members.

    Returns
    -------
    Path
        The directory holding the written ``cgroup.procs``.
    """
    directory = cgroup_root / group.removeprefix("/")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cgroup.procs").write_text(
        "".join(f"{pid}\n" for pid in pids), encoding="ascii"
    )
    return directory
