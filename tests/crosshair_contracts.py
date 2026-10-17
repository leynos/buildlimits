"""Contracts that CrossHair checks symbolically over the pure decision functions.

Each function follows CrossHair's ``asserts`` convention: leading assertions
are preconditions and later assertions are postconditions. ``test_crosshair``
runs ``crosshair check`` over this module.
"""

from buildlimits.attributes import INFINITY, AttributeRefusalError, at_most, plan_limit


def limit_order_is_total(left: int, right: int) -> None:
    """Check that wire-form limits are totally ordered with infinity greatest."""
    assert left >= INFINITY, "wire-form limits never fall below infinity"
    assert right >= INFINITY, "wire-form limits never fall below infinity"
    assert at_most(left, right) or at_most(right, left), "any two limits are comparable"
    assert at_most(left, INFINITY), "infinity is the greatest limit"
    assert at_most(left, left), "a limit is at most itself"


def planned_limits_are_reachable(
    caller_soft: int, caller_hard: int, current_soft: int, current_hard: int
) -> None:
    """Check that a plan keeps the soft limit and never raises the hard one."""
    assert min(caller_soft, caller_hard, current_soft, current_hard) >= INFINITY, (
        "wire-form limits never fall below infinity"
    )
    assert at_most(caller_soft, caller_hard), "the caller's own limits are ordered"
    assert at_most(current_soft, current_hard), "the current limits are ordered"
    try:
        soft, hard = plan_limit(
            (caller_soft, caller_hard), (current_soft, current_hard)
        )
    except AttributeRefusalError:
        assert not at_most(caller_soft, current_hard), (
            "a refusal only happens when the caller's soft limit is unreachable"
        )
        return
    assert soft == caller_soft, "the plan keeps the caller's soft limit"
    assert at_most(hard, current_hard), "the plan never raises the hard limit"
    assert at_most(soft, hard), "the planned limits stay ordered"
    assert hard == caller_hard or not at_most(caller_hard, current_hard), (
        "the plan reaches the caller's hard limit whenever it is reachable"
    )
