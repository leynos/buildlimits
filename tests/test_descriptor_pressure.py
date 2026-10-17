"""Reproduce relocation failures without reducing the test runner's limits.

High targets and dense inherited tables exercise the EINVAL/EMFILE launch
mechanism. Pipe identity and contents establish correct placement after repair.
Run from the repository root with ``uv run pytest -v tests/test_descriptor_pressure.py``.
"""

import errno
import fcntl
import os
import signal

import descriptor_helpers as helper_module
import pytest
from descriptor_helpers import SOFT_LIMIT, isolated

from buildlimits.descriptors import apply_relocation


def _high_target(report: int) -> object:
    """Place a received descriptor at the last reachable soft-limit slot."""
    target = SOFT_LIMIT - 1
    source, writer = os.pipe()
    assert target not in {source, writer}, (
        "the high target is distinct from both pipe ends"
    )
    expected = os.fstat(source)
    os.write(writer, b"high")
    os.dup2(writer, target)
    moved = apply_relocation([source], [target], keep=[report])
    actual = os.fstat(target)
    return {
        "identity": (actual.st_dev, actual.st_ino)
        == (expected.st_dev, expected.st_ino),
        "data": os.read(target, 4).decode(),
        "writer": moved[0] != target,
    }


def test_high_target_does_not_require_parking_above_soft_limit() -> None:
    """A valid target at soft minus one must not cause F_DUPFD EINVAL."""
    result = isolated(_high_target, soft=SOFT_LIMIT)
    assert isinstance(result, dict), "the child reports structured evidence"
    assert result.get("errno") is None, result
    assert result["writer"], "the error writer survives outside the targets"
    assert result["identity"] and result["data"] == "high", (
        "the target contains the original readable pipe"
    )


def _dense(report: int) -> object:
    """Fill the descriptor table before relocation has reclaimed anything."""
    source, writer = os.pipe()
    target = 1
    assert source != target, "the dense-table target is distinct from its source"
    expected = os.fstat(source)
    os.write(writer, b"dense")
    while True:
        try:
            os.dup(source)
        except OSError as error:
            if error.errno != errno.EMFILE:
                raise
            break
    moved = apply_relocation([source], [target], keep=[report])
    actual = os.fstat(target)
    return {
        "writer": moved[0] != target,
        "identity": (actual.st_dev, actual.st_ino)
        == (expected.st_dev, expected.st_ino),
        "data": os.read(target, 5).decode(),
    }


def test_dense_table_reclaims_before_allocating() -> None:
    """Unrelated inherited descriptors must not prevent compiler setup."""
    result = isolated(_dense)
    assert isinstance(result, dict), "the child reports structured evidence"
    assert result.get("errno") is None, result
    assert result["writer"], "the error writer survives outside the dense target"
    assert result["identity"] and result["data"] == "dense", (
        "the dense-table target contains the original readable pipe"
    )


def _dup_boundary(report: int) -> object:
    """Record the exact syscall errno at the soft-limit boundary."""
    try:
        fcntl.fcntl(report, fcntl.F_DUPFD_CLOEXEC, SOFT_LIMIT)
    except OSError as error:
        return {"errno": error.errno}
    return {}


def test_duplication_floor_at_soft_limit_reports_einval() -> None:
    """F_DUPFD_CLOEXEC rejects a floor equal to the effective soft limit."""
    assert isolated(_dup_boundary, soft=SOFT_LIMIT) == {"errno": errno.EINVAL}, (
        "the floor fails with EINVAL"
    )


@pytest.mark.parametrize(
    "failure",
    [
        SystemExit(9),
        RuntimeError("unexpected"),
        AssertionError("invalid experiment"),
        ValueError("invalid experiment"),
    ],
)
def test_child_failures_retain_their_exception_type(failure: BaseException) -> None:
    """Unexpected child failures must be reported rather than hidden by _exit."""

    def fail(report: int) -> object:
        """Fail before producing the child result."""
        del report
        raise failure

    with pytest.raises(RuntimeError, match=type(failure).__name__):
        isolated(fail)


def test_failed_reporting_is_a_child_failure() -> None:
    """A closed report channel must produce a nonzero child exit status."""

    def close_report(report: int) -> object:
        """Remove the reporting channel before returning the result."""
        os.close(report)
        return {}

    with pytest.raises(RuntimeError, match="disposable child failed"):
        isolated(close_report)


def test_child_reports_are_read_through_eof() -> None:
    """A multi-read report must not block the child or truncate its result."""
    expected = "x" * 16384
    assert isolated(lambda _report: expected) == expected, (
        "every report chunk is read before waiting for the child"
    )


@pytest.mark.parametrize("cyclic", [False, True])
def test_unserializable_reports_fail_the_child(cyclic: bool) -> None:
    """Serialization failures report their type and a failing child status."""
    cycle: list[object] = []
    cycle.append(cycle)
    result = cycle if cyclic else object()
    kind = "ValueError" if cyclic else "TypeError"
    with pytest.raises(RuntimeError, match=kind):
        isolated(lambda _report: result)


def test_fork_failure_closes_both_reporting_descriptors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused fork must not leave its preallocated reporting pipe open."""
    baseline = helper_module.open_fds()

    def fail() -> int:
        """Refuse the test-owned fork without creating a child."""
        raise OSError(errno.EAGAIN, "fork unavailable")

    monkeypatch.setattr(os, "fork", fail)
    with pytest.raises(OSError, match="fork unavailable"):
        isolated(lambda _report: 42)
    assert helper_module.open_fds() == baseline, (
        "both parent pipe ends close when no child exists"
    )


def test_parent_isolation_failure_preserves_the_child_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Independent child and parent failures must remain separately visible."""
    baseline = helper_module.open_fds()
    collect = helper_module._collect_report
    extra = []

    def corrupt_parent(read: int, child: helper_module._ReportChild) -> object:
        """Add one test-owned parent descriptor after collecting the child."""
        try:
            return collect(read, child)
        finally:
            extra.append(os.open("/dev/null", os.O_RDWR))

    monkeypatch.setattr(helper_module, "_collect_report", corrupt_parent)
    try:
        with pytest.raises(BaseExceptionGroup) as failure:
            isolated(lambda _report: object())
        kinds = {type(error) for error in failure.value.exceptions}
        assert kinds == {RuntimeError, AssertionError}, (
            "report failure and isolation failure are both retained"
        )
    finally:
        for fd in extra:
            os.close(fd)
    assert helper_module.open_fds() == baseline, (
        "the deliberate parent fault is fully cleaned up"
    )


@pytest.mark.parametrize("is_failure", [False, True])
def test_collection_reaps_the_child_only_once(
    monkeypatch: pytest.MonkeyPatch, is_failure: bool
) -> None:
    """Successful and failed reports both end ownership of the child PID."""
    calls = []
    original = os.waitpid

    def wait(pid: int, flags: int) -> tuple[int, int]:
        """Record each parent-side wait for the experiment's own child."""
        calls.append((pid, flags))
        return original(pid, flags)

    monkeypatch.setattr(os, "waitpid", wait)
    if is_failure:
        with pytest.raises(RuntimeError):
            isolated(lambda _report: object())
    else:
        assert isolated(lambda _report: 42) == 42, (
            "successful collection remains unchanged"
        )
    assert len(calls) == 1 and calls[0][1] == 0, (
        "cleanup does not wait again for a reaped PID"
    )


def test_interrupted_collection_terminates_before_waiting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Interrupted collection must not wait forever for a continuing child action."""
    original_wait = os.waitpid
    original_kill = os.kill
    children = []
    killed = set()

    def interrupt(_read: int, child: helper_module._ReportChild) -> object:
        """Stop report collection while retaining the experiment's child identity."""
        children.append(child.pid)
        raise InterruptedError(errno.EINTR, "injected collection interruption")

    def kill(pid: int, number: int) -> None:
        """Record termination of the experiment's own unreaped child."""
        assert pid in children, "the cleanup may signal only its owned child"
        killed.add(pid)
        original_kill(pid, number)

    def wait(pid: int, flags: int) -> tuple[int, int]:
        """Fail fast before an unsafe blocking wait instead of hanging the runner."""
        if flags == 0:
            assert pid in killed, (
                "the continuing child must be terminated before blocking wait"
            )
        return original_wait(pid, flags)

    monkeypatch.setattr(helper_module, "_collect_report", interrupt)
    monkeypatch.setattr(os, "kill", kill)
    monkeypatch.setattr(os, "waitpid", wait)
    try:
        with pytest.raises(InterruptedError, match="collection interruption"):
            isolated(lambda _report: signal.pause())
    finally:
        for pid in children:
            try:
                ended, _status = original_wait(pid, os.WNOHANG)
            except ChildProcessError:
                continue
            if ended == 0:
                original_kill(pid, signal.SIGKILL)
                original_wait(pid, 0)
    assert killed == set(children), "interrupted collection reaps every owned child"


def test_interruption_after_reaping_never_signals_the_old_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupted state update must not turn a reaped PID into a kill target."""
    original_wait = os.waitpid
    signals = []

    def interrupt_after_reap(_read: int, child: helper_module._ReportChild) -> object:
        """Model interruption after waitpid succeeds but before reaped is stored."""
        original_wait(child.pid, 0)
        raise InterruptedError(errno.EINTR, "interrupted reaped-state update")

    def record_signal(pid: int, number: int) -> None:
        """Record unsafe signalling without sending a signal to any process."""
        signals.append((pid, number))

    monkeypatch.setattr(helper_module, "_collect_report", interrupt_after_reap)
    monkeypatch.setattr(os, "kill", record_signal)
    with pytest.raises(InterruptedError, match="reaped-state update"):
        isolated(lambda _report: 42)
    assert not signals, "a reaped PID is never signalled even with stale state"
