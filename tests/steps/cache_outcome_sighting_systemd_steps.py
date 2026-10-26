"""BDD steps for the real-manager child-sighting scenarios."""

import typing as typ

from cache_outcome_harness import FakeCacheConfiguration
from cache_outcome_systemd_support import replay_report
from pytest_bdd import parsers, then, when

from buildlimits.outcome_record_format import OutcomeRecord

if typ.TYPE_CHECKING:
    import subprocess

    from systemd_harness import Harness

    from buildlimits.outcome_record_format import StoreRecord


class OutcomeWorldPort(typ.Protocol):
    """Systemd scenario surface needed by the sighting steps."""

    harness: Harness
    processes: list[subprocess.Popen[bytes]]

    def start(self) -> None:
        """Start the isolated supervisor before the scenario begins."""

    def compile(self, *, metadata: str, env: dict[str, str]) -> float:
        """Run one compiler through the owned adapter.

        Returns
        -------
        float
            Monotonic elapsed time for the completed request.
        """

    def wait_for_outcomes(self, count: int) -> list[dict[str, str]]:
        """Wait for the corresponding journal and store outcomes.

        Returns
        -------
        list[dict[str, str]]
            The journal's bounded outcome events.
        """

    def _records(self) -> list[StoreRecord]:
        """Read currently stored records for scenario assertions.

        Returns
        -------
        list[StoreRecord]
            The decoded outcome and window rows.
        """


@when("two cached compiles with distinct -C metadata run one after the other")
def _distinct_metadata_compiles(outcome_world: OutcomeWorldPort) -> None:
    """Keep a miss child visible, then delay a hit while it has no child."""
    metadata = "child-sighting-miss"
    outcome_world.harness.configure_fake_cache(
        FakeCacheConfiguration(
            script=["miss", "hit"],
            hit_delay_seconds=0.6,
            rustc_sleep_emit_metadata=metadata,
            rustc_emit_seconds=1.0,
        )
    )
    outcome_world.compile(
        metadata=metadata,
        env={},
    )
    outcome_world.wait_for_outcomes(1)
    outcome_world.compile(metadata="child-sighting-hit", env={})
    outcome_world.wait_for_outcomes(2)


@then(
    parsers.parse(
        'the "{cache_result}" cache_outcome reports child_sighting "{sighting}"'
    )
)
def _sighting_for_result(
    outcome_world: OutcomeWorldPort, cache_result: str, sighting: str
) -> None:
    """Find the requested scripted outcome without relying on event order."""
    events = outcome_world.harness.read_metric_events("cache_outcome")
    matching = [
        event
        for event in events
        if event.get("BUILD_LIMITS_CACHE_RESULT") == cache_result
    ]
    assert len(matching) == 1, "the scripted result produced one outcome"
    assert matching[0].get("BUILD_LIMITS_CHILD_SIGHTING") == sighting, (
        '_sighting_for_result: matching[0].get("BUILD_LIMITS_CHILD_SIGHTING")'
    )
    records = [
        record
        for record in outcome_world._records()
        if isinstance(record, OutcomeRecord) and record.outcome == cache_result
    ]
    assert len(records) == 1, "_sighting_for_result: len(records) == 1"
    assert records[0].child_sighting == sighting, (
        "_sighting_for_result: records[0].child_sighting == sighting"
    )
    assert records[0].child_samples >= 1, "the result contains a live sample"


@then("the outcome store holds no raw arguments or metadata values")
def _store_has_no_arguments_or_metadata(outcome_world: OutcomeWorldPort) -> None:
    """Search persisted bytes and journal fields for request-only values."""
    path = outcome_world.harness.state_home / "buildlimits" / "cache-outcomes.jsonl"
    stored = path.read_bytes()
    events = repr(outcome_world.harness.read_metric_events("cache_outcome")).encode()
    private_values = (
        b"child-sighting-miss",
        b"child-sighting-hit",
        str(outcome_world.harness.work).encode(),
        b"-Cmetadata",
        b"rustc",
    )
    for private_value in private_values:
        assert private_value not in stored, "the store contains no raw request value"
        assert private_value not in events, (
            "journal fields contain no raw request value"
        )


@when("two active requests share metadata while only one has a full-emit child")
def _overlapping_shared_metadata(outcome_world: OutcomeWorldPort) -> None:
    """Overlap delayed requests so their shared metadata stays ambiguous."""
    metadata = "shared-child-metadata"
    outcome_world.harness.configure_fake_cache(
        FakeCacheConfiguration(
            script=["miss", "hit", "miss", "hit"],
            miss_delay_seconds=0.4,
            hit_delay_seconds=1.6,
            rustc_sleep_emit_metadata=metadata,
            rustc_emit_seconds=1.2,
        )
    )
    outcome_world.compile(metadata=metadata, env={})
    outcome_world.wait_for_outcomes(1)
    outcome_world.compile(metadata=metadata, env={})
    outcome_world.wait_for_outcomes(2)
    processes = [
        outcome_world.harness.compile_rustc(metadata=metadata, env={}),
        outcome_world.harness.compile_rustc(metadata=metadata, env={}),
    ]
    outcome_world.processes.extend(processes)
    for process in processes:
        _stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 0, stderr.decode(errors="replace")
    outcome_world.wait_for_outcomes(4)


@then('both cache_outcome records report child_sighting "ambiguous"')
def _both_sightings_are_ambiguous(outcome_world: OutcomeWorldPort) -> None:
    """Require both overlapped requests to carry samples and ambiguity."""
    outcomes = [
        record
        for record in outcome_world._records()
        if isinstance(record, OutcomeRecord)
    ]
    assert len(outcomes) == 4, "_both_sightings_are_ambiguous: len(outcomes) == 4"
    assert [record.outcome for record in outcomes[:2]] == ["miss", "hit"], (
        "_both_sightings_are_ambiguous: [record.outcome for record in outcome"
    )
    assert outcomes[0].key is not None, (
        "_both_sightings_are_ambiguous: outcomes[0].key is not None"
    )
    assert outcomes[0].key == outcomes[1].key, (
        "_both_sightings_are_ambiguous: outcomes[0].key == outcomes[1].key"
    )
    assert [record.child_sighting for record in outcomes[:2]] == ["seen", "not_seen"], (
        "_both_sightings_are_ambiguous: [record.child_sighting for record in "
    )
    assert all(record.outcome == "unattributed" for record in outcomes[2:]), (
        '_both_sightings_are_ambiguous: all(record.outcome == "unattributed" '
    )
    assert all(record.child_sighting == "ambiguous" for record in outcomes[2:]), (
        '_both_sightings_are_ambiguous: all(record.child_sighting == "ambiguo'
    )
    assert all(record.child_samples >= 1 for record in outcomes), (
        "_both_sightings_are_ambiguous: all(record.child_samples >= 1 for rec"
    )


@then("H2 replay excludes those child sightings from child-only agreement")
def _ambiguous_h2_excluded(outcome_world: OutcomeWorldPort) -> None:
    """Keep mixed, unattributed samples out of H2 evidence."""
    store_path = (
        outcome_world.harness.state_home / "buildlimits" / "cache-outcomes.jsonl"
    )
    report = replay_report("h2", store_path)
    child_sightings = report["child_sightings"]
    assert isinstance(child_sightings, dict), "H2 reports child-sighting counts"
    assert child_sightings.get("ambiguous") == 0, (
        '_ambiguous_h2_excluded: report["child_sightings"]["ambiguous"] == 0'
    )
    assert report["evaluable_records"] == 2, (
        '_ambiguous_h2_excluded: report["evaluable_records"] == 2'
    )
    child_only = report["child_only"]
    assert isinstance(child_only, dict), "H2 reports child-only agreement"
    assert child_only.get("evaluable_records") == 2, (
        '_ambiguous_h2_excluded: report["child_only"]["evaluable_records"] =='
    )
