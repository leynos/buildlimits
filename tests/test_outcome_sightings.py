"""Exact rustc metadata matching and reducer sighting contracts."""

import datetime as dt

import pytest

from buildlimits.outcome_recorder import Ended, Idle, Launched, Sampled, advance
from buildlimits.outcome_recorder_types import LaunchPayload, RecorderState
from buildlimits.outcome_sightings import full_emit_metadata, request_metadata


@pytest.mark.parametrize(
    "option",
    [
        ("-C", "metadata=abc"),
        ("-Cmetadata=abc",),
        ("--codegen", "metadata=abc"),
        ("--codegen=metadata=abc",),
    ],
)
def test_request_metadata_accepts_all_rustc_spellings(option: tuple[str, ...]) -> None:
    """Read the same metadata value from each documented option form."""
    assert request_metadata(("rustc", *option)) == "abc", (
        'request_metadata_accepts_al...: request_metadata(("rustc", *option))'
    )


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (("rustc", "-Cmetadata=m"), "m"),
        (("/tool/rustc", "--emit=link", "-Cmetadata=m"), "m"),
        (("clippy-driver", "--emit", "metadata,dep-info", "-Cmetadata=m"), "m"),
        (("rustc", "--emit=dep-info", "-Cmetadata=m"), None),
        (("rustc", "--emit=asm", "-Cmetadata=m"), None),
        (("rustc", "--emit=", "-Cmetadata=m"), None),
        (("rustc", "--emit"), None),
        (("rustdoc", "-Cmetadata=m"), None),
    ],
)
def test_full_emit_parser_excludes_non_compiler_and_dep_info_passes(
    argv: tuple[str, ...], expected: str | None
) -> None:
    """Only full rustc and clippy-driver emits contribute metadata."""
    assert full_emit_metadata(argv) == expected, (
        "full_emit_parser_excludes_n...: full_emit_metadata(argv) == expected"
    )


def _launch(state: RecorderState, group: str, metadata: str | None) -> RecorderState:
    """Add one cached request with an explicit in-memory metadata value."""
    return advance(
        state,
        Launched(
            group,
            True,
            LaunchPayload(None, "registry", "cacheable", metadata),
            0.0,
        ),
    )[0]


def test_unique_match_wins_over_ambiguous_and_metadata_less_is_unavailable() -> None:
    """A later unique sighting takes precedence and missing metadata stays private."""
    state = Idle()
    state = _launch(state, "one", "same")
    state = _launch(state, "two", "same")
    state = _launch(state, "three", None)
    state = advance(state, Sampled(frozenset({"same"})))[0]
    state = advance(state, Ended("two", True, 0.5, dt.datetime.now(dt.UTC)))[0]
    state = advance(state, Sampled(frozenset({"same"})))[0]

    sightings = {item.group_id: item.sightings for item in state.progress.active}
    assert sightings["one"].sighting() == "seen", "a unique match is decisive"
    assert sightings["two"].sighting() == "ambiguous", "shared metadata is not assigned"
    assert sightings["three"].sighting() == "unavailable", (
        "missing request metadata is unknown"
    )
    assert sightings["one"].samples == 2, "only running entries gain a sample"
    assert sightings["two"].samples == 1, "an ended entry gains no later sample"


def test_sample_without_a_match_means_not_seen_only_after_metadata_exists() -> None:
    """Successful samples distinguish absence from unavailable evidence."""
    state = _launch(Idle(), "with-metadata", "request")
    state = _launch(state, "without-metadata", None)
    state = advance(state, Sampled(frozenset()))[0]

    sightings = {
        item.group_id: item.sightings.sighting() for item in state.progress.active
    }
    assert sightings == {
        "with-metadata": "not_seen",
        "without-metadata": "unavailable",
    }, 'sample_without_a_match_mean...: sightings == { "with-metadata": "not'
