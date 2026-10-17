"""Native scalar coercion through the bounded public journal query."""

import json
import subprocess

import pytest

from buildlimits.metrics import MAX_COUNTER
from buildlimits.metrics_query import query


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        pytest.param("queued", "002", "2", id="integral-text-normalized"),
        pytest.param(
            "queued", str(MAX_COUNTER + 1), str(MAX_COUNTER), id="counter-cap"
        ),
        pytest.param("queued", "1.5", None, id="fractional-counter-rejected"),
        pytest.param("queued", "-1", None, id="negative-counter-rejected"),
        pytest.param("queued", 2, None, id="native-numeric-value-rejected"),
        pytest.param("queued", True, None, id="native-boolean-value-rejected"),
        pytest.param("wait_seconds", "1.2346", "1.235", id="duration-rounded"),
        pytest.param("wait_seconds", "1e30", "1000000000000.000", id="duration-cap"),
        pytest.param("wait_seconds", "nan", None, id="non-finite-duration-rejected"),
        pytest.param("wait_seconds", "inf", None, id="infinite-duration-rejected"),
        pytest.param("wait_seconds", "-0.5", None, id="negative-duration-rejected"),
        pytest.param("reachable", "0", "0", id="false-preserved"),
        pytest.param("reachable", "1", "1", id="true-preserved"),
        pytest.param("reachable", "true", None, id="boolean-text-not-a-native-flag"),
        pytest.param("reachable", "01", None, id="boolean-not-numerically-coerced"),
        pytest.param("outcome", "completed", "completed", id="known-token-preserved"),
        pytest.param("outcome", "private-label", None, id="unknown-token-rejected"),
        pytest.param("outcome", ["completed"], None, id="array-rejected"),
        pytest.param("outcome", {"value": "completed"}, None, id="object-rejected"),
        pytest.param("outcome", None, None, id="null-rejected"),
    ],
)
def test_native_field_coercion(name: str, value: object, expected: str | None) -> None:
    """Valid records retain only canonical native-text schema measurements."""
    raw = {
        "BUILD_LIMITS_SCHEMA": "1",
        "BUILD_LIMITS_SOURCE": "supervisor",
        "BUILD_LIMITS_EVENT": "request_finished",
        "_SYSTEMD_USER_UNIT": "buildlimits-supervisor.service",
        f"BUILD_LIMITS_{name.upper()}": value,
        "BUILD_LIMITS_PRIVATE_PATH": "/private/request",
        "MESSAGE": "private journal content",
    }
    window = query(
        runner=lambda argv: subprocess.CompletedProcess(argv, 0, json.dumps(raw), "")
    )
    assert len(window.records) == 1, "invalid measurements do not discard valid records"
    assert window.records[0].fields == (
        {name: expected} if expected is not None else {}
    ), "only canonical measurements survive, with unknown and absent fields omitted"
