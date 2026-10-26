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
            event("dep-info-only")
            assert (
                full_emit_metadata(("rustc", "--emit=dep-info", "-Cmetadata=meta-a"))
                is None
            ), 'sample: full_emit_metadata(("rustc", "--emit=dep-info", "-Cmetadata='
        active = {
            group: model for group, model in self.models.items() if not model.ended
        }
        for model in active.values():
            model.samples += 1
        for child_value in child_metadata:
            matches = [
                model for model in active.values() if model.metadata == child_value
            ]
            if len(matches) == 1:
                matches[0].seen = True
            elif len(matches) > 1:
                for model in matches:
                    model.ambiguous = True
        self.state = advance(self.state, Sampled(child_metadata))[0]

    @invariant()
    def reducer_matches_the_generated_model(self) -> None:
        """Keep counts and exact-match precedence equal after every transition."""
        actual = {item.group_id: item for item in self.state.progress.active}
        for group, model in self.models.items():
            item = actual[group]
            assert item.sightings.samples == model.samples, (
                "reducer_matches_the_generat...: item.sightings.samples == model.samp"
            )
            if model.metadata is None:
                assert item.sightings.sighting() == "unavailable", (
                    'reducer_matches_the_generat...: item.sightings.sighting() == "unavai'
                )
                event("unavailable")
            elif model.seen:
                assert item.sightings.sighting() == "seen", (
                    'reducer_matches_the_generat...: item.sightings.sighting() == "seen"'
                )
                event("seen")
            elif model.ambiguous:
                assert item.sightings.sighting() == "ambiguous", (
                    'reducer_matches_the_generat...: item.sightings.sighting() == "ambigu'
                )
                event("ambiguous")
            elif model.samples:
                assert item.sightings.sighting() == "not_seen", (
                    'reducer_matches_the_generat...: item.sightings.sighting() == "not_se'
                )
                event("not_seen")
            else:
                assert item.sightings.sighting() == "unavailable", (
                    'reducer_matches_the_generat...: item.sightings.sighting() == "unavai'
                )
                event("unavailable")


TestSightingMachine = SightingMachine.TestCase
TestSightingMachine.settings = settings(max_examples=80, stateful_step_count=20)
