"""Descriptor-budget reporting stays conservative when sampling is unavailable."""

import pytest

from buildlimits import descriptor_budget
from buildlimits.descriptor_budget import DescriptorBudget
from buildlimits.descriptor_policy import DescriptorSample, calculate_budget


@pytest.mark.parametrize(
    ("sample", "slots", "expected"),
    [
        (DescriptorSample(64, None, 4), 3, 0),
        (DescriptorSample(64, 12, 4), 3, 34),
        (DescriptorSample(64, 12, 0), 3, 30),
        (DescriptorSample(64, 2, 4), 3, 42),
        (DescriptorSample(16, 12, 0), 3, 0),
        (DescriptorSample(-1, 12, 4), 3, 226),
    ],
)
def test_budget_policy_uses_only_the_explicit_descriptor_sample(
    sample: DescriptorSample, slots: int, expected: int
) -> None:
    """Unknown samples, live receipt and infinity preserve the control reserve."""
    assert (
        calculate_budget(
            sample,
            slots,
            reserve=16,
            unlimited_ceiling=256,
        )
        == expected
    ), "budget decisions must be reproducible from their explicit inputs"


def test_unknown_open_count_never_increases_queue_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unavailable sample reports null and cannot justify retaining handles."""
    monkeypatch.setattr(descriptor_budget, "open_count", lambda: None)
    budget = DescriptorBudget()
    budget.configure(slots=2)
    status = budget.describe(0)
    assert status["open"] is None, "failed sampling is explicit in structured status"
    assert status["budget"] == 0, "unknown headroom cannot increase queued retention"
    assert status["retained"] == status["high_water"] == 0, "sampling owns no requests"


def _pressured_limits(monkeypatch: pytest.MonkeyPatch) -> list[int | None]:
    """Fix the limits at soft 64 and hard 128 with an unknown open count.

    Returns
    -------
    list[int | None]
        The one-element open-count cell; set element zero to sample a count.
    """
    monkeypatch.setattr(
        descriptor_budget.resource, "getrlimit", lambda _kind: (64, 128)
    )
    sample: list[int | None] = [None]
    monkeypatch.setattr(descriptor_budget, "open_count", lambda: sample[0])
    return sample


def test_idle_resampling_preserves_launch_reserve_and_excludes_receiving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery can recapture the idle baseline without eroding group headroom."""
    sample = _pressured_limits(monkeypatch)
    budget = DescriptorBudget()
    budget.configure(slots=3)
    assert budget.budget == 0, "initial pressure refuses requests conservatively"
    sample[0] = 12
    budget.configure(receiving=4)
    assert budget.budget == 64 - 8 - descriptor_budget.DESCRIPTOR_RESERVE - 6, (
        "an idle recheck excludes unrecorded receipt and preserves three launch slots"
    )
    budget.observe(10)
    budget.observe(4)
    status = budget.describe(4)
    assert status["soft"] == 64 and status["hard"] == 128, "limits are process-local"
    assert status["retained"] == 4 and status["high_water"] == 10, (
        "temporary retention remains distinguishable from its historical high-water"
    )


def test_receipt_ceiling_is_a_pure_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reading a receipt allowance cannot mutate an exhausted startup budget."""
    sample = _pressured_limits(monkeypatch)
    budget = DescriptorBudget()
    before = budget.soft, budget.hard, budget.budget
    sample[0] = 12
    assert budget.receipt_ceiling(0) == 0, "only an explicit idle recheck can recover"
    assert (budget.soft, budget.hard, budget.budget) == before, (
        "receipt queries must preserve their captured limits and budget"
    )


@pytest.mark.parametrize(("retained", "budget_before"), [(0, 0), (1, 0), (0, 20)])
def test_explicit_idle_recheck_preserves_other_ownership(
    monkeypatch: pytest.MonkeyPatch, retained: int, budget_before: int
) -> None:
    """Only an exhausted idle budget recaptures the baseline and launch reserve."""
    monkeypatch.setattr(
        descriptor_budget.resource, "getrlimit", lambda _kind: (64, 128)
    )
    monkeypatch.setattr(descriptor_budget, "open_count", lambda: 12)
    budget = DescriptorBudget()
    budget.configure(slots=3)
    budget.budget = budget_before
    budget.recheck_if_idle(retained, receiving=4)
    expected = 64 - 8 - descriptor_budget.DESCRIPTOR_RESERVE - 6
    assert budget.budget == (
        expected if retained == budget_before == 0 else budget_before
    ), "rechecking must exclude receipt and preserve all other existing ownership"
