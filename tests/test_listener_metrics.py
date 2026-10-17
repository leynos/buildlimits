"""Listener pressure metrics preserve backlog and due retry behavior."""

import errno
import typing as typ

import pytest
from listener_helpers import watched_listener

from buildlimits.descriptor_budget import DescriptorBudget

if typ.TYPE_CHECKING:
    from conftest import RecordingSink

    from buildlimits.metrics import Metrics


def test_listener_metrics_preserve_the_original_pause_episode(
    metrics: Metrics, metric_sink: RecordingSink
) -> None:
    """Repeated pauses do not reset elapsed time or emit repeated transitions."""
    now = [0.0]
    with watched_listener(lambda: now[0], metrics) as (selector, listener, pressure):
        limits = DescriptorBudget()
        pressure.pause(listener, errno.EMFILE, limits, 0)
        now[0] = 0.05
        pressure.pause(listener, errno.ENFILE, limits, 0)
        assert len(metric_sink.records) == 1, "repeated pressure is one episode"
        assert metric_sink.records[0]["BUILD_LIMITS_ACCEPT_PAUSE_SECONDS"] == "0.000", (
            "the initial pause starts at zero elapsed time"
        )
        pressure.resume(listener)
        assert pressure.paused, "the retry deadline remains authoritative"
        assert len(metric_sink.records) == 1, "before-deadline resume emits nothing"
        now[0] = 0.2
        pressure.resume(listener)
        assert not pressure.paused, "successful restoration resumes acceptance"
        assert selector.get_key(listener).data == "listener", "watch ownership returns"
        assert metric_sink.records[-1]["BUILD_LIMITS_ACCEPT_STATE"] == "resumed", (
            "the final observation records successful restoration"
        )
        assert (
            metric_sink.records[-1]["BUILD_LIMITS_ACCEPT_PAUSE_SECONDS"] == "0.200"
        ), "duration includes the repeated pause without resetting the episode"
        assert metric_sink.records[-1]["BUILD_LIMITS_ACCEPT_PAUSED"] == "0", (
            "the snapshot reflects restored acceptance"
        )


@pytest.mark.parametrize(
    "error",
    [OSError(errno.EMFILE, "secret"), OSError(errno.ENFILE, "secret")],
)
def test_resume_failure_reports_partial_time_and_preserves_backlog(
    monkeypatch: pytest.MonkeyPatch,
    metrics: Metrics,
    metric_sink: RecordingSink,
    error: OSError,
) -> None:
    """A due pressure failure remains paused until a later successful attempt."""
    now = [1.0]
    with watched_listener(lambda: now[0], metrics) as (selector, listener, pressure):
        pressure.pause(listener, errno.EMFILE, DescriptorBudget(), 0)
        register = selector.register

        def fail(*_args: object) -> None:
            """Refuse registration once without changing listener ownership."""
            raise error

        monkeypatch.setattr(selector, "register", fail)
        now[0] = 2.0
        pressure.resume(listener)
        assert pressure.paused, "failed restoration retains the backlog"
        partial = metric_sink.records[-1]
        assert partial["BUILD_LIMITS_ACCEPT_STATE"] == "resume_failed", (
            "partial observation cannot claim successful restoration"
        )
        assert partial["BUILD_LIMITS_ACCEPT_PAUSE_SECONDS"] == "1.000", (
            "failed retry reports elapsed time from the first pause"
        )
        assert partial["BUILD_LIMITS_ACCEPT_ERRNO"] in {"emfile", "enfile"}, (
            "the errno category is fixed"
        )
        assert "secret" not in str(partial), "raw errors stay outside telemetry"
        pressure.resume(listener)
        assert len(metric_sink.records) == 2, "same-time retries wait for the deadline"
        monkeypatch.setattr(selector, "register", register)
        now[0] = 3.0
        pressure.resume(listener)
        assert not pressure.paused, "a later retry still restores acceptance"
        assert (
            metric_sink.records[-1]["BUILD_LIMITS_ACCEPT_PAUSE_SECONDS"] == "2.000"
        ), "only successful restoration completes the whole duration"


@pytest.mark.parametrize(
    "error",
    [OSError(errno.EIO, "secret"), RuntimeError("secret"), ValueError("secret")],
)
def test_resume_failure_preserves_unexpected_registration_errors(
    monkeypatch: pytest.MonkeyPatch,
    metrics: Metrics,
    metric_sink: RecordingSink,
    error: Exception,
) -> None:
    """Unexpected registration errors retain their identity after observation."""
    now = [0.0]
    with watched_listener(lambda: now[0], metrics) as (selector, listener, pressure):
        pressure.pause(listener, errno.EMFILE, DescriptorBudget(), 0)

        def fail(*_args: object) -> None:
            """Fail the real registration boundary with the supplied exception."""
            raise error

        monkeypatch.setattr(selector, "register", fail)
        now[0] = 1.0
        with pytest.raises(type(error)) as caught:
            pressure.resume(listener)
        assert caught.value is error, "observation must preserve exception identity"
        assert pressure.paused, "an unexpected failure cannot restore acceptance"
        assert pressure.next_resume == pytest.approx(1.1), (
            "unexpected failures retain the advanced retry deadline"
        )
        assert metric_sink.records[-1]["BUILD_LIMITS_ACCEPT_ERRNO"] == "none", (
            "unlisted and absent errnos use the fixed none category"
        )


class BrokenMetrics:
    """A caller-supplied port that refuses every observation."""

    def emit(self, event: str, **_fields: object) -> None:
        """Inject a telemetry failure without exposing field content."""
        del event
        msg = "unavailable metrics"
        raise OSError(msg)


def test_listener_metric_failures_do_not_change_acceptance() -> None:
    """A failing injected metric port cannot hold a restored listener paused."""
    now = [0.0]
    with watched_listener(lambda: now[0], BrokenMetrics()) as (
        selector,
        listener,
        pressure,
    ):
        pressure.pause(listener, errno.EMFILE, DescriptorBudget(), 0)
        now[0] = 1.0
        pressure.resume(listener)
        assert not pressure.paused, "metric failure cannot affect successful restore"
        assert selector.get_key(listener).data == "listener", "the backlog stays owned"
