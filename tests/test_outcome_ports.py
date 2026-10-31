"""Closed success and failure values for outcome-recording ports."""

import typing as typ

import pytest

from buildlimits.outcome_key import Salt as KeySalt
from buildlimits.outcome_ports import (
    GenerationFailure,
    QueryFailure,
    Salt,
    StoreFailure,
    StoreResult,
)

if typ.TYPE_CHECKING:
    from buildlimits.outcome_vocabulary import StoreReason


@pytest.mark.parametrize("reason", ["timeout", "read_error"])
def test_statistics_failure_reason_is_closed(
    reason: typ.Literal["timeout", "read_error"],
) -> None:
    """Represent the two statistics-port failures named by Table 2."""
    failure = QueryFailure(reason)

    assert failure.reason == reason, "the statistics reason remains explicit"


def test_generation_failure_has_its_table_reason() -> None:
    """Keep unavailable server identity distinct from a missing server."""
    failure = GenerationFailure("generation_unavailable")

    assert failure.reason == "generation_unavailable", (
        "generation failure uses the Table 2 token"
    )


@pytest.mark.parametrize(
    "reason",
    [
        "corrupt",
        "unreadable",
        "version_mismatch",
        "tag_dropped",
        "salt_unavailable",
        "save_failed",
        "expired",
        "salt_changed",
        "clock_skew",
    ],
)
def test_store_failure_reason_is_a_store_reason(reason: StoreReason) -> None:
    """Carry each failure token through the store port's typed result."""
    failure = StoreFailure(reason)

    assert failure.reason == reason, "the store reports the selected failure"


def test_ports_reexport_the_single_key_salt_type() -> None:
    """Keep salt ownership in outcome_key while exposing it to adapters."""
    assert Salt is KeySalt, "the port module does not create a second salt type"


def test_store_result_keeps_operation_and_bounded_counts() -> None:
    """Represent one asynchronous writer result without an I/O exception."""
    result = StoreResult(
        operation="append", reason="save_failed", records=0, discarded=1
    )

    assert result.operation == "append", "the originating store operation stays named"
    assert result.reason == "save_failed", "the closed failure reason is retained"
    assert (result.records, result.discarded) == (0, 1), (
        "writer counts remain available to the aggregate event"
    )
