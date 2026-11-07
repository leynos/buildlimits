"""The stray-process guard's admission-time reading.

:mod:`test_kache_guard` covers the reading itself: what it finds, what counts
as covered and when a change is journalled. :mod:`test_supervisor_effects`
covers the launch handler's ordering. This module is the seam between the two
-- a launch taking the reading at all -- and sits on its own so both stay
within the repository's module-size ceiling.
"""

import typing as typ
from pathlib import Path

from groups_helpers import FakeGroups
from pool_helpers import attach_pool
from request_helpers import crate_pending
from supervisor_helpers import supervisor

from buildlimits import kache_guard
from buildlimits.admission import Group, GroupState
from buildlimits.config import SupervisorConfig
from buildlimits.kache import Kache

if typ.TYPE_CHECKING:
    import pytest

    from buildlimits.groups import Groups
    from buildlimits.supervisor import Supervisor

__all__ = ["supervisor"]  # Re-exported so pytest finds the fixture here.


def test_admission_reads_the_slice_for_strays_before_placing_the_group(
    supervisor: Supervisor,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The guard runs at admission, so a leaked daemon is reported promptly.

    The guard's only other refresh is the five-second cache poll, so a group
    admitted straight after a leak could compose against a cache whose daemon
    is no longer accounted for without anything saying so. Reading the slice
    before placement also means the reading describes the state the group is
    admitted into, not one that already includes its own processes.
    """
    slice_dir = tmp_path / "buildlimits-compilers.slice"
    slice_dir.mkdir()
    stale = slice_dir / "buildlimits-old.scope"
    stale.mkdir()
    (stale / "cgroup.procs").write_text("4242\n", encoding="ascii")
    monkeypatch.setattr(kache_guard, "_is_kache", lambda pid: pid == 4242)
    lines: list[str] = []
    monkeypatch.setattr(kache_guard, "log", lines.append)

    groups = FakeGroups(
        order=[],
        placed=None,
        settings=SupervisorConfig(cache_mode="kache", kache_socket=Path("/d.sock")),
        backend=Kache("/managed/kache"),
        slice_dir=slice_dir,
    )
    supervisor.groups = typ.cast("Groups", groups)
    supervisor.admission.groups["g1"] = Group(
        "g1", request_id=1, token=b"a", cached=True, state=GroupState.RUNNING
    )
    supervisor.requests.pending[1] = crate_pending(1)
    attach_pool(supervisor)

    supervisor._launch(1, "g1", cached=True)

    assert groups.guard.pids == (4242,), (
        "the launch itself took the reading; the poll never had to run"
    )
    assert len(lines) == 1, f"the reading reaches the journal: {lines}"
    assert "4242" in lines[0], lines[0]
