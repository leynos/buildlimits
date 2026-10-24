"""Collection rules for the opt-in sccache host acceptance check."""

import os
import sys
import typing as typ

import sccache_host

if typ.TYPE_CHECKING:
    import pytest


def test_opt_in_host_check_skips_non_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit opt-in still skips where Linux process interfaces are absent."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setitem(os.environ, "BUILD_LIMITS_SCCACHE_HOST_CHECK", "1")

    marks = sccache_host.sccache_host_marks()
    skip_markers = [mark for mark in marks if mark.name.startswith("skipif")]

    assert len(skip_markers) == 1, "the host check should receive one skip marker"
    assert skip_markers[0].kwargs == {
        "condition": True,
        "reason": "the host check requires Linux /proc and SO_PEERCRED",
    }, "the opt-in must not attempt Linux-only checks on another platform"
