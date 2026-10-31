"""Generated launch, end and child-sample overlap invariants."""

import dataclasses as dc
import datetime as dt

from hypothesis import event, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
from outcome_test_helpers import cache_key

from buildlimits.outcome_recorder import Ended, Idle, Launched, Sampled, advance
from buildlimits.outcome_recorder_types import LaunchPayload
from buildlimits.outcome_sightings import full_emit_metadata


@dc.dataclass(slots=True)
class SightingModel:
    """Independent generated expectation for one running compiler group."""

    metadata: str | None
    samples: int = 0
    seen: bool = False
    ambiguous: bool = False
    ended: bool = False


def _assert_dep_info_is_ignored() -> None:
    """Assert a dep-info-only command line yields no full-emit metadata."""
    argument = ("rustc", "--emit=dep-info", "-Cmetadata=meta-a")
    assert full_emit_metadata(argument) is None, (
        "a dep-info-only compile has no full-emit metadata"
    )


def _apply_metadata(
    active: dict[str, SightingModel], child_metadata: frozenset[str]
) -> None:
    """Mark running models that matched one sample uniquely or ambiguously."""
    for child_value in child_metadata:
        _mark_matches(active, child_value)


def _mark_matches(active: dict[str, SightingModel], child_value: str) -> None:
    """Record how many running models carry one sample's metadata value."""
    matches = [model for model in active.values() if model.metadata == child_value]
    if len(matches) == 1:
        matches[0].seen = True
    elif len(matches) > 1:
        for model in matches:
            model.ambiguous = True


class SightingMachine(RuleBasedStateMachine):
    """Compare reducer samples with an independent metadata match model."""

    def __init__(self) -> None:
        """Start an empty recorder and generated model."""
        super().__init__()
        self.state = Idle()
        self.models: dict[str, SightingModel] = {}
        self.serial = 0
        self.now = 0.0

    @rule(metadata=st.sampled_from([None, "meta-a", "meta-b"]))
    def launch(self, metadata: str | None) -> None:
        """Overlap requests for the same crate with independently chosen metadata."""
        self.serial += 1
        group = f"g{self.serial}"
        self.now += 0.1
        payload = LaunchPayload(cache_key(), "registry", "cacheable", metadata)
        self.state = advance(self.state, Launched(group, True, payload, self.now))[0]
        self.models[group] = SightingModel(metadata)

    @rule()
    def end_running_group(self) -> None:
        """End one still-running launch without dropping its model history."""
        running = [
            (group, model) for group, model in self.models.items() if not model.ended
        ]
        if not running:
            return
        group, model = running[0]
        self.now += 0.1
        self.state = advance(
            self.state,
            Ended(group, True, self.now, dt.datetime(2026, 10, 26, tzinfo=dt.UTC)),
        )[0]
        model.ended = True

    @rule(
        metadata=st.sets(st.sampled_from(["meta-a", "meta-b"]), max_size=2),
        dep_info_only=st.booleans(),
    )
    def sample(self, metadata: set[str], dep_info_only: bool) -> None:
        """Apply one successful full-emit sample to all still-running entries."""
        child_metadata = frozenset() if dep_info_only else frozenset(metadata)
        if dep_info_only:
            _assert_dep_info_is_ignored()
        active = self._active_models()
        for model in active.values():
            model.samples += 1
        _apply_metadata(active, child_metadata)
        self.state = advance(self.state, Sampled(child_metadata))[0]

    def _active_models(self) -> dict[str, SightingModel]:
        """Return the still-running model entries by group id.

        Returns
        -------
        dict[str, SightingModel]
            Models whose group has not ended.
        """
        return {group: model for group, model in self.models.items() if not model.ended}

    @invariant()
    def reducer_matches_the_generated_model(self) -> None:
        """Keep counts and exact-match precedence equal after every transition."""
        actual = {item.group_id: item for item in self.state.progress.active}
        for group, model in self.models.items():
            item = actual[group]
            assert item.sightings.samples == model.samples, (
                "the reducer preserves generated sample counts"
            )
            if model.metadata is None:
                assert item.sightings.sighting() == "unavailable", (
                    "missing metadata is unavailable"
                )
                event("unavailable")
            elif model.seen:
                assert item.sightings.sighting() == "seen", (
                    "observed process metadata is seen"
                )
                event("seen")
            elif model.ambiguous:
                assert item.sightings.sighting() == "ambiguous", (
                    "multiple process matches are ambiguous"
                )
                event("ambiguous")
            elif model.samples:
                assert item.sightings.sighting() == "not_seen", (
                    "sampled absence is classified as not seen"
                )
                event("not_seen")
            else:
                assert item.sightings.sighting() == "unavailable", (
                    "zero samples leave the sighting unavailable"
                )
                event("unavailable")


TestSightingMachine = SightingMachine.TestCase
TestSightingMachine.settings = settings(max_examples=80, stateful_step_count=20)
