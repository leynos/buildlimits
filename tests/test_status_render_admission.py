"""Readiness rendering for malformed admission state values."""

import pytest

from buildlimits.status_render import render


@pytest.mark.parametrize("admission_state", [None, "future"])
def test_invalid_admission_state_reports_readiness_unavailable(
    admission_state: object,
) -> None:
    """Use one diagnostic for malformed and future admission states."""
    rendered = render({
        "capacity": 1,
        "held": 0,
        "healthy": True,
        "caching": True,
        "admission": {"state": admission_state},
    })
    assert rendered.splitlines()[0] == (
        "admission readiness unavailable: readiness is not reported"
    ), "malformed admission states use the shared unavailable diagnostic"
