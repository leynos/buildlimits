"""The retention line names the bounds and the active snapshot's state.

The renderer is the operator's only view of the effective policy in the status
document, so each branch that changes the line is pinned to its exact text: a
disabled bound, a value that is not a number, and an incomplete active snapshot
with and without a reason.
"""

from buildlimits.status_render_retention import retention_lines

_LIMITS = {
    "max_age_days": 30,
    "max_age_seconds": 2_592_000,
    "max_bytes": 15_000_000_000,
    "max_records": None,
    "headroom_bytes": 25_165_824,
    "segment_bytes": 12_582_912,
    "segmented": True,
    "age_eviction": True,
    "byte_eviction": True,
}


def test_disabled_bounds_are_named_as_disabled() -> None:
    """A policy with both bounds off says so instead of printing zeroes."""
    limits = _LIMITS | {
        "max_age_days": 0,
        "max_age_seconds": 0,
        "max_bytes": 0,
        "age_eviction": False,
        "byte_eviction": False,
    }

    (line,) = retention_lines({"retention": {"limits": limits}})

    assert line == (
        "cache outcomes retention: no age limit, no byte limit, no record cap"
    ), "disabled bounds are named, and an unbounded cap says so"


def test_an_unparseable_bound_keeps_a_readable_fallback() -> None:
    """A byte bound that is not an integer still renders a description."""
    limits = _LIMITS | {"max_bytes": "unknown"}

    (line,) = retention_lines({"retention": {"limits": limits}})

    assert line == ("cache outcomes retention: 30 days, a byte limit, no record cap"), (
        "the fallback names the kind of bound rather than dropping it"
    )


def test_an_incomplete_snapshot_names_its_reason() -> None:
    """A snapshot that could not be measured reports why it is incomplete."""
    section = {
        "limits": _LIMITS,
        "active": {
            "record_count": 4,
            "logical_bytes": 512,
            "complete": False,
            "reason": "unreadable",
        },
    }

    (line,) = retention_lines({"retention": section})

    assert line == (
        "cache outcomes retention: 30 days, 15000000000 logical bytes, "
        "no record cap; active 4 records (512 logical bytes); "
        "active snapshot incomplete (unreadable)"
    ), "the closed reason is printed beside the incomplete snapshot"


def test_an_incomplete_snapshot_without_a_reason_says_unknown() -> None:
    """A missing reason is reported as unknown rather than left blank."""
    section = {
        "limits": _LIMITS,
        "active": {"record_count": 0, "complete": False},
    }

    (line,) = retention_lines({"retention": section})

    assert line.endswith("; active snapshot incomplete (unknown)"), (
        "an unnamed fault is still disclosed"
    )
