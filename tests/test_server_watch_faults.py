"""Fault-kind transitions and the zero-root proof on the server watch.

:mod:`test_server_watch` covers what each fault kind reports in isolation.
This module is the seam between kinds — one watch seeing a live-work fault
and then a manager fault — and the structured root count the automatic
recovery trigger corroborates its starvation proof with. A sibling module
keeps both under the repository's module-size ceiling.
"""

import functools
import typing as typ
from pathlib import Path

from test_server_watch import FakeServer, _draining, _scripted, _watch

from buildlimits import sccache as sccache_module
from buildlimits.proc_trees import count_children
from buildlimits.sccache import LiveCacheWorkError, ManagedSccache, SccacheError
from buildlimits.server_watch import ServerWatch

if typ.TYPE_CHECKING:
    import pytest

# A fault whose root count was never established, so nothing may act on it.
_UNKNOWN_ROOTS = LiveCacheWorkError(
    "live sccache service may retain detached requests; "
    "visible compiler roots=unknown; operator accounting required",
    roots=None,
)
_ZERO_ROOTS = LiveCacheWorkError(
    "live sccache service may retain detached requests; "
    "visible compiler roots=0; operator accounting required",
    roots=0,
)
_POSITIVE_ROOTS = LiveCacheWorkError(
    "live sccache service may retain detached requests; "
    "visible compiler roots=2; operator accounting required",
    roots=2,
)


def _spend_the_fault_budget(watch: ServerWatch) -> None:
    """Exhaust the retry limit, which is when a recovery action is installed."""
    for _ in range(8):
        watch.drained_tokens()


def test_a_changed_fault_kind_replaces_the_recovery_action(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Guidance tracks the fault, in both directions and without piling up.

    A stale action is worse than none: it tells the operator to run a command
    that will refuse, while the fault that would actually be cured goes
    unmentioned. Repeated observations of one kind must still log only once.
    """
    now = [0.0]
    server = FakeServer(load=_ZERO_ROOTS)
    watch = _watch(server, _draining(b"a"))
    watch.clock = lambda: now[0]
    _spend_the_fault_budget(watch)
    live_action = watch.recovery_action
    assert live_action is not None, "the live-work fault installs its guidance"
    capsys.readouterr()

    # One more observation of the same kind neither logs nor rewrites.
    now[0] = 30.0
    watch.drained_tokens()
    assert watch.recovery_action == live_action, "the same kind keeps its action"
    assert not capsys.readouterr().err, "a repeated observation is not re-logged"

    # The cause changes to a manager transport fault.
    now[0] = 60.0
    server.load = SccacheError("injected transport refusal")
    watch.drained_tokens()
    manager_action = watch.recovery_action
    assert manager_action is not None, "the manager fault installs its own guidance"
    assert manager_action != live_action, "stale live-work guidance is replaced"
    assert "build-limits-recover" not in manager_action, (
        "manager guidance names the transport, not the recovery command"
    )
    assert "drain requires operator action" in capsys.readouterr().err, (
        "the replacement guidance is announced"
    )

    # And back again: the kind that returns must replace the manager guidance.
    now[0] = 90.0
    server.load = _ZERO_ROOTS
    watch.drained_tokens()
    assert watch.recovery_action == live_action, (
        "returning to live work restores live-work guidance"
    )
    assert "drain requires operator action" in capsys.readouterr().err, (
        "the restored guidance is announced as a change"
    )


def test_only_a_proven_zero_root_count_answers_the_trigger() -> None:
    """The zero-root proof is true for an established zero and nothing else."""
    for error, expected in [
        (_ZERO_ROOTS, True),
        (_POSITIVE_ROOTS, False),
        (_UNKNOWN_ROOTS, False),
        (SccacheError("injected transport refusal"), False),
    ]:
        watch = _watch(FakeServer(load=error), _draining(b"a"))
        watch.drained_tokens()
        assert watch.zero_visible_roots is expected, f"{error!r} must answer {expected}"


def test_only_an_established_positive_count_shows_visible_roots() -> None:
    """Visible roots are true for an established positive count and nothing else.

    An unknown count is neither the zero proof nor visible roots, so the
    recovery deadline can never be started by a reading that saw nothing.
    """
    for error, expected in [
        (_ZERO_ROOTS, False),
        (_POSITIVE_ROOTS, True),
        (_UNKNOWN_ROOTS, False),
        (SccacheError("injected transport refusal"), False),
    ]:
        watch = _watch(FakeServer(load=error), _draining(b"a"))
        watch.drained_tokens()
        assert watch.roots_are_visible is expected, f"{error!r} must answer {expected}"


def test_a_cleared_fault_clears_the_root_proof() -> None:
    """The proof cannot outlive the fault whose reading produced it."""
    server = FakeServer(load=_ZERO_ROOTS, stopped=True)
    watch = _watch(server, _draining(b"a"))
    watch.drained_tokens()
    assert watch.zero_visible_roots is True, "the fault established the proof"

    server.load = 0
    watch.drained_tokens()
    assert watch.recovery_action is None, "the success cleared the fault"
    assert watch.zero_visible_roots is False, (
        "a cleared fault leaves no root proof behind"
    )


def _procfs(root: Path, stat_texts: dict[int, str]) -> None:
    """Stage the ``/proc/<pid>/stat`` files a fixture process tree needs."""
    for pid, stat_text in stat_texts.items():
        entry = root / str(pid)
        entry.mkdir(parents=True, exist_ok=True)
        (entry / "stat").write_text(stat_text, encoding="utf-8")


def _scripted_live_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    procs: str,
    stats: dict[int, str],
) -> ManagedSccache:
    """Script a live snapshot whose cgroup and process tree are fixtures.

    The diagnostic's real counter still runs — the cgroup read, the parentage
    walk and the count — but both of its roots move into the fixture, so the
    test never reads the host's own process table.
    """
    (tmp_path / "cgroup.procs").write_text(procs, encoding="ascii")
    proc = tmp_path / "proc"
    _procfs(proc, stats)
    monkeypatch.setattr(sccache_module.cgroups, "directory", lambda _path: tmp_path)
    wired = functools.partial(count_children, proc=proc)
    monkeypatch.setattr(sccache_module, "count_children", wired, raising=True)
    return _scripted({
        "MainPID": "4242",
        "ControlGroup": "/fixture.service",
        "ActiveState": "active",
    })


def test_the_real_cgroup_reading_reaches_the_root_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live idle fixture cache proves zero roots through the real reader.

    The scripted faults above pin the classification; here the diagnostic
    itself runs, so a change that stopped counting roots or stopped carrying
    the count into the proof fails here rather than only in the fake.
    """
    # The server is the only member, so it has no children to count.
    server = _scripted_live_cache(
        tmp_path,
        monkeypatch,
        procs="4242\n",
        stats={4242: "4242 (sccache) S 1 4242 4242 0 -1"},
    )
    watch = ServerWatch(server, _draining(b"a"), "sccache")

    watch.drained_tokens()

    assert watch.drain_fault_kind == "live_work", "the live cache is a live-work fault"
    assert watch.zero_visible_roots is True, (
        "the diagnostic's own reading reaches the proof"
    )


def test_the_real_reader_withdraws_the_proof_for_a_visible_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same path refuses the proof when the server has a compiler child.

    A guard against the proof passing because the reader always answers zero:
    with one compiler child in the fixture tree, the count must be one and the
    proof must be withheld. The child's parent field is what places it in the
    server's tree.
    """
    server = _scripted_live_cache(
        tmp_path,
        monkeypatch,
        procs="4242\n4243\n",
        stats={
            4242: "4242 (sccache) S 1 4242 4242 0 -1",
            4243: "4243 (rustc) S 4242 42 42 0 -1",
        },
    )
    watch = ServerWatch(server, _draining(b"a"), "sccache")

    watch.drained_tokens()

    assert watch.drain_fault_kind == "live_work", "the fault kind is still live work"
    assert watch.zero_visible_roots is False, (
        "a visible compiler child withdraws the zero-root proof"
    )


def test_a_pending_automatic_recovery_makes_the_command_a_fallback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The guidance follows whether the supervisor will recover by itself."""
    pending = [True]
    now = [0.0]
    watch = _watch(FakeServer(load=_ZERO_ROOTS), _draining(b"a"))
    watch.clock = lambda: now[0]
    watch.automatic_recovery_pending = lambda: pending[0]
    _spend_the_fault_budget(watch)
    assert watch.recovery_action_is_fallback, "recovery is pending"
    assert "pending" in (watch.recovery_action or ""), "the text says so"
    journal = capsys.readouterr().err
    assert "drain requires operator action" not in journal, journal
    assert "only a fallback" in journal, journal

    pending[0] = False
    assert not watch.recovery_action_is_fallback, "recovery can no longer happen"
    assert "pending" not in (watch.recovery_action or ""), "the order is back"
    now[0] = 30.0
    watch.drained_tokens()
    assert "drain requires operator action" in capsys.readouterr().err, (
        "the journal gives the mandatory wording once the fallback ends"
    )


def test_a_manager_fault_is_never_a_fallback() -> None:
    """Only a live-work fault can be cured by the automatic recovery."""
    watch = _watch(FakeServer(load=SccacheError("manager down")), _draining(b"a"))
    watch.automatic_recovery_pending = lambda: True
    _spend_the_fault_budget(watch)
    assert not watch.recovery_action_is_fallback, "the manager must be restored"
    assert "do not stop" in (watch.recovery_action or ""), "mandatory guidance"
