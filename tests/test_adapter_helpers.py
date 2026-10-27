"""Tests for the adapter's pure helpers and the supervisor's composition rule."""

import pytest

from buildlimits.compiler_argv import compiler_argv
from buildlimits.heartbeat import Heartbeat, crate_label, heartbeat_seconds


def test_heartbeats_follow_the_phase() -> None:
    """Waiting and compiling lines differ, and appear only when due."""
    now = [0.0]
    beat = Heartbeat("serde", 45.0, clock=lambda: now[0])
    assert beat.tick() is None, "a heartbeat is silent before it is due"
    now[0] = 45.0
    beat.ahead = 2
    assert (
        beat.tick() == "[build-limits] serde: waiting for a compile slot (2 ahead, 45s)"
    ), "a waiting heartbeat reports how many callers are ahead"
    assert beat.tick() is None, "a heartbeat is silent until it is due again"
    beat.admitted()
    now[0] = 90.0
    assert beat.tick() == "[rustc-heartbeat] serde: still compiling after 45s", (
        "an admitted heartbeat reports compiling progress"
    )


def test_waiting_for_the_supervisor_says_so() -> None:
    """A missing supervisor is not reported as a busy pool."""
    now = [45.0]
    beat = Heartbeat("serde", 45.0, clock=lambda: now[0])
    beat.next_due = 0.0
    line = beat.tick(waiting_for="the build-limits supervisor")
    assert line is not None, "a due heartbeat always produces a line"
    assert "the build-limits supervisor" in line, "the wait reason names the supervisor"
    assert "ahead" not in line, "a missing supervisor is not reported as a busy pool"


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, 45.0),
        ({"RUSTC_HEARTBEAT_SECS": "30"}, 30.0),
        ({"BUILD_LIMITS_HEARTBEAT_SECS": "5", "RUSTC_HEARTBEAT_SECS": "30"}, 5.0),
        ({"BUILD_LIMITS_HEARTBEAT_SECS": "nonsense"}, 45.0),
        ({"BUILD_LIMITS_HEARTBEAT_SECS": "0"}, 45.0),
        ({"BUILD_LIMITS_HEARTBEAT_SECS": "inf", "RUSTC_HEARTBEAT_SECS": "30"}, 30.0),
        ({"BUILD_LIMITS_HEARTBEAT_SECS": "nan"}, 45.0),
    ],
)
def test_heartbeat_interval(env: dict[str, str], expected: float) -> None:
    """The interval honours both variables and ignores invalid values."""
    assert heartbeat_seconds(env) == expected, "the interval matches the env override"


def test_crate_labels() -> None:
    """The crate name labels progress lines; probes use the compiler's name."""
    assert (
        crate_label(["rustc", "--crate-name", "itoa", "--edition", "2021"]) == "itoa"
    ), "the crate name follows --crate-name"
    assert crate_label(["/t/rustc", "-vV"]) == "rustc", "a probe is labelled by binary"
    assert crate_label(["rustc", "--crate-name"]) == "rustc", (
        "a dangling --crate-name falls back to the binary name"
    )


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (("/t/rustc", "-vV"), ("/t/rustc", "-vV")),
        (("/b/sccache", "/t/rustc", "x.rs"), ("/t/rustc", "x.rs")),
        (("/b/kache", "/t/rustc", "x.rs"), ("/t/rustc", "x.rs")),
        (("sccache", "sccache", "/t/rustc"), ("/t/rustc",)),
        (("kache", "kache", "/t/rustc"), ("/t/rustc",)),
        (("kache", "sccache", "/t/rustc"), ("/t/rustc",)),
        (("/t/clippy-driver", "/t/rustc"), ("/t/clippy-driver", "/t/rustc")),
        (("sccache",), ("sccache",)),
        (("kache",), ("kache",)),
    ],
)
def test_a_callers_own_cache_layer_is_removed(
    argv: tuple[str, ...], expected: tuple[str, ...]
) -> None:
    """The supervisor, not the caller, decides whether a cache runs."""
    assert compiler_argv(argv) == expected, "the caller's own cache layer is stripped"
