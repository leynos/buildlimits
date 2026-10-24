"""Stateful adversarial sequences for the cache-outcome recorder."""

import typing as typ

import pytest
from hypothesis import event, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    rule,
)
from outcome_recorder_machine_support import (
    REQUIRED_CASES,
    TRUTHS,
    RecorderReadings,
    _Outcome,
    _Output,
)
from outcome_recorder_test_helpers import (
    GENERATION,
    OBSERVED_AT,
    baseline_state,
    cache_key,
    outcome_module,
    recorder_progress,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

pytestmark = pytest.mark.xfail(
    strict=True, reason="EP-M4 red contracts precede the outcome recorder"
)


class RecorderMachine(RuleBasedStateMachine):
    """Compare reducer output with request truth across adversarial orderings."""

    def __init__(self) -> None:
        """Create empty model-server counters and recorder state."""
        super().__init__()
        self.recorder = outcome_module("outcome_recorder")
        self.ports = outcome_module("outcome_ports")
        self.gate = outcome_module("outcome_gate")
        self.attribution = outcome_module("outcome_attribution")
        self.state = self.recorder.Idle()
        self.now = 0.0
        self.generation = GENERATION
        self.server_counters: dict[str, int] = {}
        self.server_drain_count = 0
        self.next_group = 0
        self.ticket_serial = 0
        self.payload = self.recorder.LaunchPayload(
            key=cache_key(), origin="registry", parser_class="cacheable"
        )
        self.active: dict[str, str | None] = {}
        self.cached_group_ids: set[str] = set()
        self.truth_by_group: dict[str, str] = {}
        self.pending_groups: list[str] = []
        self.outcomes: dict[str, _Outcome] = {}
        self.case_labels: set[str] = set()
        self.readings = RecorderReadings(self)
        self.exercise_required_outcomes()

    def exercise_required_outcomes(self) -> None:
        """Reach each required classification and rejection before exploration."""
        self.readings.exercise_balanced_windows()
        self._exercise_rejections_and_straddling()
        self._exercise_restart_and_overflow()

    def _exercise_rejections_and_straddling(self) -> None:
        """Reach an ineligible window, timeout and straddled reading."""
        rejected = self._launch("hit")
        self.readings.finish(rejected, is_clean=False)
        self.readings.finish_read()
        self._expect(rejected, "unattributed", "ineligible_window")
        self._mark("rejected")

        abandoned = self._launch("hit")
        self.readings.finish(abandoned, is_clean=True)
        self.readings.start_query()
        abandoned_output = self.readings.finish_read(failure="timeout")
        assert abandoned_output is not None, "the timed-out ticket is accounted"
        self._expect(abandoned, "unattributed", "timeout")
        self._mark("abandonment")

        old_baseline = recorder_progress(self.state).baseline
        straddled_case = self._launch_during_query("hit")
        assert straddled_case is not None, "the straddled reading started"
        straddled, straddled_output = straddled_case
        assert straddled_output.reading_reasons == ("straddled_reading",), (
            "a launch inside the query interval straddles the reading"
        )
        assert recorder_progress(self.state).baseline == old_baseline, (
            "a straddling read leaves the prior baseline unchanged"
        )
        assert straddled not in self.outcomes, "the straddled launch remains buffered"
        self._mark("straddle")
        self.readings.finish_read()
        self._expect(straddled, "hit", "none", "solo")

    def _exercise_restart_and_overflow(self) -> None:
        """Reach a high-counter restart and overflow the bounded window."""
        assert self.cached_group_ids == set(self.outcomes), (
            "the restart model accounts for every earlier cached launch"
        )
        self.state = baseline_state(self.recorder)
        self.server_counters = {}
        self.generation = GENERATION
        previous_counters = self.server_counters.copy()
        restarted = self._launch("hit")
        self.readings.finish(restarted, is_clean=True)
        self.readings.start_query()
        next_generation = self.attribution.Generation("run-b", 110, 210)
        self.generation = next_generation
        restart_output = self.readings.finish_read()
        assert restart_output is not None, "the restarted server was read"
        self._expect(restarted, "unattributed", "generation_changed")
        baseline = recorder_progress(self.state).baseline
        assert baseline is not None and baseline.generation == next_generation, (
            "the first new-generation reading becomes its baseline"
        )
        assert all(
            self.server_counters[field] > previous_counters.get(field, 0)
            for field in ("compile_requests", "requests_executed", "cache_hits")
        ), "the restarted generation counters climb above the old reading"
        self._mark("restart")

        after_restart, _ = self.readings.window(
            self._launch, self.readings.finish, ("hit",)
        )
        self._expect(after_restart[0], "hit", "none", "solo")

        overflowed, _ = self.readings.window(
            self._launch, self.readings.finish, ("hit",) * 65
        )
        for group_id in overflowed:
            self._expect(group_id, "unattributed", "window_overflow")
        self._mark("overflow")

    def _apply(self, recorder_event: object) -> _Output:
        """Apply one pure reducer event and check each emitted row in order."""
        self.state, output = self.recorder.advance(self.state, recorder_event)
        for outcome in output.events:
            self._consume(outcome)
        return output

    def _consume(self, outcome: _Outcome) -> None:
        """Match an output row to the oldest cached request and check its truth."""
        assert self.pending_groups, "the recorder emitted an unowned outcome"
        group_id = self.pending_groups.pop(0)
        assert group_id not in self.outcomes, "a cached launch was emitted twice"
        self.outcomes[group_id] = outcome
        truth = self.truth_by_group[group_id]
        if outcome.cache_result == "unattributed":
            assert outcome.outcome_reason != "none", (
                "an unattributed request names why it could not be labelled"
            )
            return
        assert outcome.cache_result == truth, (
            "every emitted label matches the model server's request truth"
        )
        assert outcome.outcome_attribution in {"solo", "unanimous"}, (
            "only a sound solo or unanimous model window emits a label"
        )
        assert outcome.outcome_reason == "none", "sound labels have no refusal reason"

    def _expect(
        self,
        group_id: str,
        cache_result: str,
        reason: str,
        attribution: str | None = None,
    ) -> None:
        """Assert the output for one model request has the expected disposition."""
        outcome = self.outcomes[group_id]
        assert outcome.cache_result == cache_result, "cache result differs from model"
        assert outcome.outcome_reason == reason, "outcome reason differs from model"
        if attribution is not None:
            assert outcome.outcome_attribution == attribution, (
                "outcome attribution differs from model"
            )

    def _mark(self, name: str) -> None:
        """Report a reached branch to Hypothesis and the teardown contract."""
        event(name)
        self.case_labels.add(name)

    def _launch(self, truth: str | None = None, *, cached: bool = True) -> str:
        """Record one cached request or a direct launch in both models."""
        self.next_group += 1
        group_id = f"group-{self.next_group}"
        is_cached = cached and truth is not None
        if is_cached:
            assert truth is not None, "cached model requests need server truth"
            self.cached_group_ids.add(group_id)
            self.truth_by_group[group_id] = truth
            self.pending_groups.append(group_id)
        self.active[group_id] = truth if is_cached else None
        self._apply(
            self.recorder.Launched(
                group_id=group_id,
                is_cached=is_cached,
                payload=self.payload,
                now=self.now,
            )
        )
        return group_id

    def _launch_during_query(self, truth: str) -> tuple[str, _Output] | None:
        """Start a query, then finish one request inside its reading interval."""
        if not self.readings.start_query():
            return None
        group_id = self._launch(truth)
        self.readings.finish(group_id, is_clean=True)
        output = self.readings.finish_read()
        return None if output is None else (group_id, output)

    def _finish_random_group(self, index: int, *, is_clean: bool) -> bool:
        """Finish one selected live group and report whether it was live."""
        groups = tuple(self.active)
        if groups and not isinstance(self.state, self.recorder.Disabled):
            self.readings.finish(groups[index % len(groups)], is_clean=is_clean)
            return True
        return False

    def _stop_recorder(self, *, mark_explicit_stop: bool = False) -> None:
        """Stop an enabled recorder and label an explicit stop rule."""
        if not isinstance(self.state, self.recorder.Disabled):
            self.now += 0.1
            self._apply(self.recorder.Stopping(now=self.now, observed_at=OBSERVED_AT))
            if mark_explicit_stop:
                self._mark("stop")

    def _run_special_read(self, scenario: cabc.Callable[[], None]) -> None:
        """Run a generated server scenario only at a quiet reading point."""
        if self.readings.can_run_special_read():
            scenario()

    @rule(truth=st.sampled_from(TRUTHS))
    def cached_launch(self, truth: str) -> None:
        """Launch one cached request whose server truth is held by the model."""
        if not isinstance(self.state, self.recorder.Disabled):
            self._launch(truth)

    @rule()
    def direct_launch(self) -> None:
        """Launch and end a direct request without adding a cached outcome."""
        if isinstance(self.state, self.recorder.Disabled):
            return
        group_id = self._launch(cached=False)
        self.readings.finish(group_id, is_clean=True)

    @rule()
    def launch_failure(self) -> None:
        """Record a process failure that never entered the cached window."""
        if not isinstance(self.state, self.recorder.Disabled):
            self.next_group += 1
            self._apply(
                self.recorder.LaunchFailed(group_id=f"failed-{self.next_group}")
            )

    @rule(
        index=st.integers(min_value=0, max_value=100),
        ending=st.sampled_from(("clean_exit", "signalled_exit", "cancel")),
    )
    def group_exit(
        self, index: int, ending: typ.Literal["clean_exit", "signalled_exit", "cancel"]
    ) -> None:
        """End a live group cleanly, by signal, or through cancellation."""
        if self._finish_random_group(index, is_clean=ending == "clean_exit"):
            self._mark(ending)

    @rule()
    def open_reading(self) -> None:
        """Open a query ticket while the model has a quiet point."""
        self.readings.start_query()

    @rule()
    def prompt_reading(self) -> None:
        """Complete a statistics query promptly with current model counters."""
        self.readings.finish_read()

    @rule()
    def slow_reading(self) -> None:
        """Complete a slow query before its ticket deadline."""
        self.readings.finish_read(delay=1.5)

    @rule(failure=st.sampled_from(("timeout", "read_error")))
    def failed_reading(self, failure: typ.Literal["timeout", "read_error"]) -> None:
        """Abandon a query or report a statistics-port read failure."""
        output = self.readings.finish_read(failure=failure)
        if output is not None:
            case = "abandonment" if failure == "timeout" else "failing_reading"
            self._mark(case)

    @rule()
    def server_drain(self) -> None:
        """Block query start while a managed-server token awaits accounting."""
        is_recording = not isinstance(self.state, self.recorder.Disabled)
        is_not_querying = not isinstance(self.state, self.recorder.Querying)
        can_check_drain = is_recording and is_not_querying
        is_quiet = not self.readings.active_cached()
        if can_check_drain and is_quiet:
            output = self.readings.enter_server_drain()
            assert not output.commands, "server drain prevents a statistics query"

    @rule()
    def release_server_drain(self) -> None:
        """Release the model drain and let a fresh query proceed."""
        if self.server_drain_count:
            self.server_drain_count = 0
            self.readings.finish_read()

    @rule()
    def straddling_launch(self) -> None:
        """Keep the first reading around an in-flight launch as neither edge."""
        self._run_special_read(self._run_straddling_launch)

    def _run_straddling_launch(self) -> None:
        """Exercise a launch that begins and ends during an open query."""
        straddled_case = self._launch_during_query("hit")
        if straddled_case is None:
            return
        _, output = straddled_case
        if output.reading_reasons == ("straddled_reading",):
            self.readings.finish_read()

    @rule()
    def restart_server(self) -> None:
        """Restart during a query with counters above the old generation."""
        self._run_special_read(self._run_restart_server)

    def _run_restart_server(self) -> None:
        """Read the model server across a generation change."""
        if not self.pending_groups:
            group_id = self._launch("hit")
            self.readings.finish(group_id, is_clean=True)
        if not self.readings.start_query():
            return
        previous = self.server_counters.copy()
        self.generation = self.attribution.Generation(
            f"run-{self.next_group + 1}", self.next_group + 110, self.next_group + 210
        )
        self.server_counters = {name: value + 100 for name, value in previous.items()}
        self.readings.finish_read()

    @rule()
    def overflow_window(self) -> None:
        """Drive more than 64 cached requests through one bounded window."""
        self._run_special_read(self._run_overflow_window)

    def _run_overflow_window(self) -> None:
        """Send 65 cached requests through the bounded model window."""
        before = len(self.cached_group_ids)
        groups, _ = self.readings.window(
            self._launch, self.readings.finish, ("hit",) * 65
        )
        if all(
            self.outcomes.get(group_id) is not None
            and self.outcomes[group_id].outcome_reason == "window_overflow"
            for group_id in groups
        ):
            self._mark("overflow")
        assert len(self.cached_group_ids) - before == 65, (
            "overflow sequence launched all 65 model requests"
        )

    @rule()
    def stop(self) -> None:
        """Close any live query and account for every cached launch."""
        self._stop_recorder(mark_explicit_stop=True)

    def teardown(self) -> None:
        """Stop the machine and enforce exact-once and branch non-vacuity."""
        self._stop_recorder()
        assert set(self.outcomes) == self.cached_group_ids, (
            "stop or an earlier close accounts for every cached launch exactly once"
        )
        assert not self.pending_groups, "no cached launch remains unaccounted"
        assert self.case_labels >= REQUIRED_CASES, (
            f"machine cases were not reached: {REQUIRED_CASES - self.case_labels}"
        )


TestRecorderMachine = RecorderMachine.TestCase
TestRecorderMachine.settings = settings(
    max_examples=100,
    stateful_step_count=60,
    deadline=None,
    database=None,
)
