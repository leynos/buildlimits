"""Check live descriptor gauges and explicit sampling failures in status."""

import errno
import resource
import typing as typ

import pytest

from buildlimits import descriptors, status
from buildlimits.config import SupervisorConfig
from buildlimits.supervisor import Supervisor

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path

    from buildlimits.systemd import UserManager


@pytest.fixture
def supervisor() -> cabc.Iterator[Supervisor]:
    """Describe local process resources without starting a supervisor service."""
    instance = Supervisor(SupervisorConfig(), typ.cast("UserManager", object()))
    with instance.selector:
        yield instance


def test_descriptor_gauges_follow_live_process_state(
    supervisor: Supervisor, tmp_path: Path
) -> None:
    """An opened descriptor changes the next sample; limits match getrlimit."""
    before = typ.cast(
        "dict[str, int | None]", status.describe(supervisor)["descriptors"]
    )
    with (tmp_path / "held-descriptor").open("wb"):
        during = typ.cast(
            "dict[str, int | None]", status.describe(supervisor)["descriptors"]
        )
    after = typ.cast(
        "dict[str, int | None]", status.describe(supervisor)["descriptors"]
    )
    assert isinstance(before["open"], int), "live usage is numeric"
    assert during["open"] == before["open"] + 1, "opening adds one descriptor"
    assert after["open"] == before["open"], "closing restores the count"
    assert (during["soft_limit"], during["hard_limit"]) == resource.getrlimit(
        resource.RLIMIT_NOFILE
    ), "limits match the live process"
    assert during["sampling_errno"] is None, "successful sampling has no errno"
    assert (during["soft"], during["hard"]) == (
        during["soft_limit"],
        during["hard_limit"],
    ), "compatibility aliases use the same live limits"
    assert set(before) == set(during) == set(after), (
        "opening and closing a descriptor preserves the status schema"
    )


@pytest.mark.parametrize("error_number", [errno.EMFILE, errno.EACCES])
def test_unreadable_descriptor_usage_is_reported_without_failing_status(
    supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch, error_number: int
) -> None:
    """Failed sampling remains unknown rather than reporting zero or crashing."""

    def refuse(wanted: object) -> list[int]:
        """Fail only the descriptor enumeration boundary."""
        raise OSError(error_number, "cannot enumerate descriptors")

    monkeypatch.setattr(descriptors, "open_descriptors_except", refuse)
    document = status.describe(supervisor)
    gauges = typ.cast("dict[str, int | None]", document["descriptors"])
    assert gauges["open"] is None, "failed sampling leaves usage unknown"
    assert gauges["sampling_errno"] == error_number, "the sampling errno is reported"
    assert (gauges["soft_limit"], gauges["hard_limit"]) == resource.getrlimit(
        resource.RLIMIT_NOFILE
    ), "limits match the live process"
    assert document["healthy"] is True, "sampling failure does not fail admission"


def test_combined_descriptor_status_samples_once(
    supervisor: Supervisor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queue gauges and compatibility aliases share one descriptor sample."""
    samples: list[object] = []

    def sample(wanted: object) -> list[int]:
        """Record each live enumeration without opening another descriptor."""
        samples.append(wanted)
        return [0, 1, 2]

    monkeypatch.setattr(descriptors, "open_descriptors_except", sample)
    supervisor.requests.descriptors.high_water = 7
    gauges = typ.cast(
        "dict[str, int | None]", status.describe(supervisor)["descriptors"]
    )
    assert len(samples) == 1, "one query cannot combine different live samples"
    assert gauges["open"] == 3, "the single sample supplies process usage"
    assert gauges["high_water"] == 7, "queue history survives live sampling"
    assert gauges["budget"] == supervisor.requests.descriptors.budget, (
        "queue reserve survives"
    )
