"""Tests for capturing and planning caller process attributes."""

import os

import pytest
from hypothesis import given
from hypothesis import strategies as st

from buildlimits import attributes
from buildlimits.attributes import (
    INFINITY,
    AttributeCaptureError,
    AttributeRefusalError,
    ProcessAttributes,
    at_most,
    capture,
    current_umask,
    plan_attributes,
    plan_limit,
)

limits = st.integers(min_value=INFINITY, max_value=2**32)


@given(limits, limits, limits)
def test_at_most_is_transitive(first: int, second: int, third: int) -> None:
    """The wire-form order is transitive, with infinity greatest."""
    if at_most(first, second) and at_most(second, third):
        assert at_most(first, third), "at_most is transitive"


@given(limits, limits, limits)
def test_planned_hard_limit_is_never_raised(soft: int, hard: int, current: int) -> None:
    """The launch child never asks for a hard limit above its own."""
    try:
        _soft, planned = plan_limit((soft, hard), (0, current))
    except AttributeRefusalError:
        assert not at_most(soft, hard if at_most(hard, current) else current), (
            "a refusal only happens when the soft limit is unreachable"
        )
        return
    assert at_most(planned, current), "the planned hard limit is never raised"


def test_higher_priority_than_the_supervisor_is_refused() -> None:
    """A caller that runs at a higher priority cannot be reproduced."""
    caller = ProcessAttributes(umask=0o22, nice=-5, rlimits={})
    with pytest.raises(AttributeRefusalError, match="priority"):
        plan_attributes(caller, current_nice=0, current_limits={})


def test_unknown_limits_are_refused() -> None:
    """A limit the launch child does not know is not silently dropped."""
    caller = ProcessAttributes(umask=0o22, nice=0, rlimits={"RLIMIT_BOGUS": (1, 1)})
    with pytest.raises(AttributeRefusalError, match="unknown"):
        plan_attributes(caller, current_nice=0, current_limits={})


def test_a_restrictive_umask_is_carried() -> None:
    """The caller's umask reaches the plan unchanged."""
    caller = ProcessAttributes(umask=0o077, nice=0, rlimits={})
    assert plan_attributes(caller, 0, {}).umask == 0o077, (
        "the umask is carried unchanged"
    )


def test_capture_reads_the_real_umask_without_changing_it() -> None:
    """The umask is read from /proc, not by setting it."""
    previous = os.umask(0o027)
    try:
        assert current_umask() == 0o027, "the umask is read correctly"
        assert capture().umask == 0o027, "the captured umask matches"
        assert os.umask(0o027) == 0o027, "reading the umask does not change it"
    finally:
        os.umask(previous)


class _Status:
    """A stand-in for ``/proc/self/status`` with fixed contents."""

    def __init__(self, text: str) -> None:
        """Hold the status text."""
        self.text = text

    def read_text(self, encoding: str) -> str:
        """Return the status text."""
        del encoding
        return self.text


@pytest.mark.parametrize(
    "status",
    ["Name:\tpython3\nPid:\t1\n", "Umask:\tnonsense\n"],
    ids=["missing", "malformed"],
)
def test_an_unreadable_umask_fails_the_capture(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    """Without a readable Umask field the capture fails; the umask is untouched."""
    previous = os.umask(0o027)
    try:
        monkeypatch.setattr(attributes, "Path", lambda _path: _Status(status))
        with pytest.raises(AttributeCaptureError, match="umask"):
            current_umask()
        assert os.umask(0o027) == 0o027, "a failed capture does not change the umask"
    finally:
        os.umask(previous)


def test_captured_limits_plan_against_themselves() -> None:
    """The adapter's own attributes are always reproducible by an equal process."""
    captured = capture()
    plan = plan_attributes(captured, captured.nice, captured.rlimits)
    assert plan.rlimits == captured.rlimits, (
        "an equal process reproduces its own limits"
    )
