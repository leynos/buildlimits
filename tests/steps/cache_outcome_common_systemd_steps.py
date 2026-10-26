"""Shared cache-result setup step for real-manager outcome scenarios."""

import typing as typ

from cache_outcome_harness import FakeCacheConfiguration
from pytest_bdd import given, parsers


class CacheHarnessPort(typ.Protocol):
    """Fake-cache configuration surface used by the shared BDD step."""

    def configure_fake_cache(self, configuration: FakeCacheConfiguration) -> None:
        """Set the deterministic result sequence for the current scenario."""


class OutcomeWorldPort(typ.Protocol):
    """World fixture surface required by the common recording steps."""

    harness: CacheHarnessPort

    def start(self) -> None:
        """Start the isolated supervisor before the scenario begins."""


@given(
    'an isolated supervisor with cache_outcomes "record" and a scripted fake sccache'
)
def recording_supervisor(outcome_world: OutcomeWorldPort) -> None:
    """Start recording only for the current isolated fixture supervisor."""
    outcome_world.start()


@given(parsers.parse('the fake sccache scripts "{first}" then "{second}"'))
def scripted_cache_results(
    outcome_world: OutcomeWorldPort, first: str, second: str
) -> None:
    """Install two deterministic fake cache results before any compile."""
    outcome_world.harness.configure_fake_cache(
        FakeCacheConfiguration(script=[first, second])
    )
