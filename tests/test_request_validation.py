"""Total policy ceilings and temporary descriptor retention remain distinct."""

import pytest

from buildlimits.attributes import AttributePlan, ProcessAttributes
from buildlimits.protocol import CompileRequest
from buildlimits.request_validation import (
    CompileContext,
    CompileRefusal,
    DescriptorHeadroom,
    prepare_compile,
)


def _request(targets: tuple[int, ...], nice: int = 0) -> CompileRequest:
    """Create a validation input without sampling the test runner's state."""
    return CompileRequest(
        ("/bin/true",),
        "/",
        {},
        ProcessAttributes(0o22, nice, {}),
        targets,
        "pressure",
        cacheable=False,
    )


@pytest.mark.parametrize(
    ("available", "budget", "reason"),
    [(3, 3, "descriptors"), (3, 4, "busy"), (4, 4, None), (0, 0, "busy")],
)
def test_total_budget_and_available_headroom_have_distinct_outcomes(
    available: int, budget: int, reason: str | None
) -> None:
    """Count the request socket at exact total and available budget boundaries."""
    result = prepare_compile(
        _request((8, 9, 10)),
        3,
        DescriptorHeadroom(available, budget),
        context=CompileContext(0, {"RLIMIT_NOFILE": (64, 128)}),
    )
    if reason is None:
        assert isinstance(result, AttributePlan), (
            "an exact-fit request remains admissible"
        )
    else:
        assert isinstance(result, CompileRefusal), (
            "insufficient headroom must refuse admission"
        )
        assert result.reason == reason, (
            "a known nonzero policy ceiling is final; unknown idle headroom can recover"
        )


@pytest.mark.parametrize(
    ("compile_request", "received", "hard", "reason"),
    [
        (_request((128,), -1), 0, 128, "bad-request"),
        (_request((128,), -1), 1, 128, "descriptors"),
        (_request((127,), -1), 1, 128, "attributes"),
        (_request((127,), 0), 1, 128, None),
        (_request((128,), 0), 1, -1, None),
    ],
)
def test_validation_uses_explicit_process_sample_and_refusal_precedence(
    compile_request: CompileRequest, received: int, hard: int, reason: str | None
) -> None:
    """Mismatch and unreachable targets precede explicitly sampled priority rules."""
    context = CompileContext(0, {"RLIMIT_NOFILE": (64, hard)})
    result = prepare_compile(
        compile_request, received, DescriptorHeadroom(4, 4), context=context
    )
    if reason is None:
        assert result == AttributePlan(0o22, compile_request.attributes.nice, {}), (
            "reachable targets and priorities use only the supplied process sample"
        )
    else:
        assert isinstance(result, CompileRefusal), "policy refusals are domain values"
        assert result.reason == reason, "refusal precedence must remain stable"


def test_attribute_limits_are_planned_from_the_explicit_sample() -> None:
    """An unreachable caller limit is refused using the supplied ceiling."""
    request = _request(())
    request.attributes.rlimits["RLIMIT_NOFILE"] = (129, 256)
    result = prepare_compile(
        request,
        0,
        DescriptorHeadroom(4, 4),
        context=CompileContext(0, {"RLIMIT_NOFILE": (64, 128)}),
    )
    assert isinstance(result, CompileRefusal), "unreachable attributes refuse admission"
    assert result.reason == "attributes", (
        "caller soft limits must not be silently capped"
    )
