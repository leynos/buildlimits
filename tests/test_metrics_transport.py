"""Byte budgets, pipe draining and child cleanup for journal transport."""

import errno
import io
import math
import os
import selectors
import subprocess
import sys
import time
import unittest.mock as mock  # ruff: ignore[manual-from-import] - module alias required by .rules/python-typing.md.

import pytest

from buildlimits import metrics_transport
from buildlimits.metrics_transport import JournalOutputError, run


def test_transport_drains_both_pipes_within_budget() -> None:
    """Both streams count towards the same limit, including at exact equality."""
    result = run(
        [sys.executable, "-c", "import os; os.write(1, b'ab'); os.write(2, b'cd')"],
        max_bytes=4,
    )
    assert result.returncode == 0, "owned query child exits normally"
    assert result.stdout == "ab" and result.stderr == "cd", "both pipes drained"


def test_transport_zero_budget_accepts_empty_output() -> None:
    """EOF on both streams consumes no bytes, including with a zero budget."""
    result = run([sys.executable, "-c", "pass"], max_bytes=0)
    assert result.returncode == 0, "the silent query child exits normally"
    assert result.stdout == result.stderr == "", "empty streams fit a zero byte budget"


@pytest.mark.parametrize("descriptor", [1, 2])
def test_transport_zero_budget_rejects_first_output_byte(descriptor: int) -> None:
    """The one-byte probe detects the first byte on either stream at zero budget."""
    script = f"import os; os.write({descriptor}, b'x')"
    with pytest.raises(JournalOutputError, match="output byte limit") as caught:
        run([sys.executable, "-c", script], max_bytes=0)
    assert str(caught.value) == "journal query exceeded its output byte limit", (
        "the first byte retains the exact shared-budget error"
    )


@pytest.mark.parametrize("descriptor", [1, 2])
def test_transport_rejects_oversized_output(descriptor: int) -> None:
    """Neither stdout nor stderr can grow beyond the shared byte budget."""
    script = f"import os; os.write({descriptor}, b'x' * 1000000)"
    with pytest.raises(JournalOutputError, match="output byte limit"):
        run([sys.executable, "-c", script], max_bytes=16)


def _assert_reaped(child: subprocess.Popen[bytes]) -> None:
    """Check an owned child has terminated and no zombie is left behind."""
    assert child.returncode is not None, "the owned child terminated before return"
    with pytest.raises(ChildProcessError):
        os.waitpid(child.pid, os.WNOHANG)
    with pytest.raises(ProcessLookupError):
        os.kill(child.pid, 0)


def _frozen_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[mock.MagicMock, mock.Mock]:
    """Provide a clock frozen at 1.0 and a selector double.

    Returns
    -------
    tuple[mock.MagicMock, mock.Mock]
        The selector double, which reports one live pipe, and the clock.
    """
    selector = mock.MagicMock(spec=selectors.BaseSelector)
    selector.__enter__.return_value = selector
    selector.get_map.return_value = {1: object()}
    clock = mock.Mock(return_value=1.0)
    monkeypatch.setattr(
        metrics_transport.selectors, "DefaultSelector", lambda: selector
    )
    return selector, clock


def test_transport_timeout_reaps_owned_child(monkeypatch: pytest.MonkeyPatch) -> None:
    """A timeout kills and reaps the owned child before returning the error."""
    argv = [sys.executable, "-c", "import time; time.sleep(10)"]
    with subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as child:
        launcher = mock.Mock(return_value=child)
        monkeypatch.setattr(metrics_transport.subprocess, "Popen", launcher)
        try:
            with pytest.raises(subprocess.TimeoutExpired):
                run(argv, timeout=0.05)
            launcher.assert_called_once_with(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            _assert_reaped(child)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()


def test_transport_registration_failure_reaps_owned_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Selector allocation pressure closes its context and reaps the owned child."""
    argv = [sys.executable, "-c", "import time; time.sleep(10)"]
    error = OSError(errno.EMFILE, "controlled selector registration failure")
    selector = mock.MagicMock(spec=selectors.BaseSelector)
    selector.__enter__.return_value = selector
    selector.register.side_effect = error

    def exit_selector(_kind: object, _error: object, _traceback: object) -> None:
        """Close the controlled selector without suppressing its failure."""
        selector.close()

    selector.__exit__.side_effect = exit_selector
    monkeypatch.setattr(
        metrics_transport.selectors, "DefaultSelector", lambda: selector
    )
    with subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as child:
        monkeypatch.setattr(
            metrics_transport.subprocess, "Popen", mock.Mock(return_value=child)
        )
        try:
            with pytest.raises(
                OSError, match="controlled selector registration failure"
            ) as caught:
                run(argv)
            assert caught.value is error, "the original allocation error is propagated"
            selector.close.assert_called_once_with()
            _assert_reaped(child)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()


def test_transport_invalid_utf8_is_a_query_failure() -> None:
    """Invalid byte output is rejected after bounded reading, before JSON parsing."""
    with pytest.raises(JournalOutputError, match="not UTF-8"):
        run([sys.executable, "-c", "import os; os.write(1, bytes([255]))"])


@pytest.mark.parametrize("missing", ["stdout", "stderr"])
def test_capture_requires_both_output_pipes(
    missing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing pipes fail during registration, before deadline or streaming work."""
    selector = mock.MagicMock(spec=selectors.BaseSelector)
    selector.__enter__.return_value = selector
    monkeypatch.setattr(
        metrics_transport.selectors, "DefaultSelector", lambda: selector
    )
    with io.BytesIO() as stream:
        child = mock.Mock(
            spec=subprocess.Popen,
            stdout=None if missing == "stdout" else stream,
            stderr=None if missing == "stderr" else stream,
        )
        with pytest.raises(
            RuntimeError, match="journal query requires output pipes"
        ) as caught:
            metrics_transport._capture(
                child,
                max_bytes=1,
                deadline=metrics_transport._Deadline(0, 0, time.monotonic),
            )
        assert str(caught.value) == "journal query requires output pipes", (
            "exact missing-pipe error"
        )
        expected = (
            [] if missing == "stdout" else [mock.call(stream, selectors.EVENT_READ, 0)]
        )
        assert selector.register.call_args_list == expected, (
            "stdout is registered first with buffer zero"
        )
    selector.get_map.assert_not_called()
    selector.__exit__.assert_called_once()


def test_transport_rejects_combined_output_above_budget() -> None:
    """Each pipe can fit individually while their total exceeds the shared cap."""
    script = "import os; os.write(1, b'abcd'); os.write(2, b'efgh')"
    with pytest.raises(JournalOutputError, match="output byte limit") as caught:
        run([sys.executable, "-c", script], max_bytes=6)
    assert str(caught.value) == "journal query exceeded its output byte limit", (
        "combined overflow rejected"
    )


@pytest.mark.parametrize("deadline", [0.5, 1.0], ids=["past", "exact"])
def test_capture_expired_deadline_never_selects(
    monkeypatch: pytest.MonkeyPatch, deadline: float
) -> None:
    """Live registered pipes expire before selecting at or after the deadline."""
    selector, clock = _frozen_capture(monkeypatch)
    with io.BytesIO() as stdout, io.BytesIO() as stderr:
        child = mock.Mock(
            spec=subprocess.Popen, args=["journalctl"], stdout=stdout, stderr=stderr
        )
        with pytest.raises(subprocess.TimeoutExpired) as caught:
            metrics_transport._capture(
                child,
                max_bytes=4,
                deadline=metrics_transport._Deadline(deadline, 3, clock),
            )
        assert caught.value.cmd == child.args, "timeout retains the owned command"
        assert caught.value.timeout == 3, "timeout retains the configured duration"
        selector.register.assert_has_calls([
            mock.call(stdout, selectors.EVENT_READ, 0),
            mock.call(stderr, selectors.EVENT_READ, 1),
        ])
    selector.select.assert_not_called()
    clock.assert_called_once_with()


def test_capture_nan_deadline_preserves_selector_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NaN deadline reaches the selector instead of becoming an expired timeout."""
    selector, clock = _frozen_capture(monkeypatch)
    selector.select.side_effect = ValueError("non-finite timeout")
    with io.BytesIO() as stdout, io.BytesIO() as stderr:
        child = mock.Mock(spec=subprocess.Popen, stdout=stdout, stderr=stderr)
        with pytest.raises(ValueError, match="non-finite timeout"):
            metrics_transport._capture(
                child,
                max_bytes=4,
                deadline=metrics_transport._Deadline(math.nan, 3, clock),
            )
    assert selector.select.call_count == 1, "NaN is forwarded to the selector once"
    assert math.isnan(selector.select.call_args.args[0]), "the timeout remains NaN"
    clock.assert_called_once_with()
