"""Behavioural tests for the approved outcome-attribution examples."""

import dataclasses as dc

import pytest
from pytest_bdd import given, parsers, scenarios, then, when

from buildlimits.outcome_attribution import (
    Attribution,
    Generation,
    Reading,
    WindowEntry,
    attribute,
)
from buildlimits.outcome_stats import StatsReading

scenarios("features/outcome_rules.feature")


@dc.dataclass(slots=True)
class OutcomeWorld:
    """Mutable scenario values shared by the steps of one BDD example."""

    before: Reading | None = None
    after: Reading | None = None
    window: tuple[WindowEntry, ...] = ()
    result: Attribution | None = None


@pytest.fixture
def world() -> OutcomeWorld:
    """Give each BDD scenario fresh readings and a fresh launch window."""
    return OutcomeWorld()


def _stats(**changes: int) -> StatsReading:
    """Build one cumulative statistics reading for a scenario.

    Examples
    --------
    >>> _stats(compile_requests=3).compile_requests
    3
    """
    values = {
        "compile_requests": 0,
        "requests_unsupported_compiler": 0,
        "requests_not_compile": 0,
        "requests_not_cacheable": 0,
        "requests_executed": 0,
        "cache_hits": 0,
        "cache_misses": 0,
        "cache_errors": 0,
        "non_cacheable_compilations": 0,
        "compilations": 0,
        "compile_fails": 0,
    }
    values.update(changes)
    return StatsReading(**values, version="0.14.0")


def _generation(name: str) -> Generation:
    """Build a distinct server identity for a feature generation name.

    Examples
    --------
    >>> _generation("a").invocation
    'a'
    """
    return Generation(invocation=name, pid=1, starttime=1)


def _finish_reading(world: OutcomeWorld, generation: str, **changes: int) -> None:
    """Store the post-reading and evaluate the scenario's whole window.

    Examples
    --------
    >>> world = OutcomeWorld(before=Reading(_generation("a"), _stats()))
    >>> _finish_reading(world, "a", compile_requests=1)
    >>> world.after.stats.compile_requests
    1
    """
    assert world.before is not None, "a post-reading requires its baseline"
    world.after = Reading(_generation(generation), _stats(**changes))
    world.result = attribute(world.before, world.after, world.window)


@given(parsers.parse('a baseline reading in generation "{generation}"'))
def baseline_reading(world: OutcomeWorld, generation: str) -> None:
    """Establish a zero-counter baseline for one server generation."""
    world.before = Reading(_generation(generation), _stats())


@given(
    parsers.parse(
        'a baseline reading in generation "{generation}" with compile_requests {count:d}'
    )
)
def baseline_reading_with_compile_requests(
    world: OutcomeWorld, generation: str, count: int
) -> None:
    """Establish a baseline with an already-populated request counter."""
    world.before = Reading(_generation(generation), _stats(compile_requests=count))


@given("one cached group that ended cleanly")
def one_clean_cached_group(world: OutcomeWorld) -> None:
    """Record one clean launch ending with a stable group identifier."""
    world.window = (WindowEntry(group_id="g0", is_clean=True),)


@given("three cached groups that ended cleanly")
def three_clean_cached_groups(world: OutcomeWorld) -> None:
    """Record three clean launch endings with stable group identifiers."""
    world.window = tuple(
        WindowEntry(group_id=f"g{index}", is_clean=True) for index in range(3)
    )


@given("one cached group that was signalled")
def signalled_cached_group(world: OutcomeWorld) -> None:
    """Record the suspect ending that makes the whole window ineligible."""
    world.window = (WindowEntry(group_id="g0", is_clean=False),)


@when(
    parsers.parse(
        'a reading in generation "{generation}" shows cache_hits up by 1 and compile_requests up by 1'
    )
)
def cache_hit_reading(world: OutcomeWorld, generation: str) -> None:
    """Record a solo cache-hit footprint."""
    _finish_reading(
        world,
        generation,
        cache_hits=1,
        requests_executed=1,
        compile_requests=1,
    )


@when(
    parsers.parse(
        'a reading in generation "{generation}" shows compilations up by 1 and compile_requests up by 1'
    )
)
def compilations_only_reading(world: OutcomeWorld, generation: str) -> None:
    """Record a solo compilation without a cache-hit counter movement."""
    _finish_reading(
        world, generation, compilations=1, requests_executed=1, compile_requests=1
    )


@when("a reading shows one hit, one miss and one declined request")
def mixed_reading(world: OutcomeWorld) -> None:
    """Record three distinct outcomes without splitting their shared window."""
    _finish_reading(
        world,
        "a",
        cache_hits=1,
        compilations=1,
        cache_misses=1,
        requests_executed=2,
        requests_not_cacheable=1,
        compile_requests=3,
    )


@when(
    parsers.parse(
        'a reading in generation "{generation}" shows compile_requests {count:d}'
    )
)
def restarted_reading(world: OutcomeWorld, generation: str, count: int) -> None:
    """Record counters above the old total in a different server generation."""
    _finish_reading(world, generation, compile_requests=count)


@when(parsers.parse('a reading in generation "{generation}" shows a miss'))
def miss_reading(world: OutcomeWorld, generation: str) -> None:
    """Record a single miss footprint for the requested generation."""
    _finish_reading(
        world,
        generation,
        compilations=1,
        cache_misses=1,
        requests_executed=1,
        compile_requests=1,
    )


@when(
    parsers.parse(
        "a reading shows requests_not_cacheable up by 1 and compile_requests up by 1"
    )
)
def declined_reading(world: OutcomeWorld) -> None:
    """Record the server-declined request footprint without a compilation."""
    _finish_reading(world, "a", requests_not_cacheable=1, compile_requests=1)


@then(parsers.parse('the group is labelled "{label}" with attribution "{kind}"'))
def group_has_label(world: OutcomeWorld, label: str, kind: str) -> None:
    """Assert the solo result label and attribution kind."""
    assert world.result is not None, "the scenario must have a classification result"
    assert world.result.labels == (("g0", label),), (
        "the solo group receives its expected label"
    )
    assert world.result.kind == kind, "the solo result keeps its attribution kind"


@then(parsers.parse('every group is "{label}" with attribution "{kind}"'))
def all_groups_have_label(world: OutcomeWorld, label: str, kind: str) -> None:
    """Assert that a mixed window remains whole and unattributed."""
    assert world.result is not None, "the scenario must have a classification result"
    assert all(value == label for _, value in world.result.labels), (
        "every group in the window receives the expected label"
    )
    assert world.result.kind == kind, "the whole-window attribution kind is preserved"


@then(parsers.parse('the group is "{label}" with outcome reason "{reason}"'))
def group_has_reason(world: OutcomeWorld, label: str, reason: str) -> None:
    """Assert the group fallback label and its generation or eligibility reason."""
    assert world.result is not None, "the scenario must have a classification result"
    assert world.result.labels == (("g0", label),), (
        "the fallback group label is preserved"
    )
    assert world.result.reason == reason, (
        "the expected generation or eligibility reason is kept"
    )


@then(parsers.parse('the outcome reason is "{reason}"'))
def outcome_has_reason(world: OutcomeWorld, reason: str) -> None:
    """Assert the reason for the entire unattributed window."""
    assert world.result is not None, "the scenario must have a classification result"
    assert world.result.reason == reason, "the whole-window failure reason is preserved"
