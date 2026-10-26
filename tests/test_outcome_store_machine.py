"""Generated append, compaction, reload, clock and salt store contracts."""

import datetime as dt
import typing as typ
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    rule,
    run_state_machine_as_test,
)
from outcome_store_fixture_helpers import append_torn_utf8_tail
from outcome_store_helpers import (
    salt_path,
    stored_records,
    wait_for_results,
    wait_for_salt,
    window_summary,
)
from outcome_test_helpers import record_generated_case

from buildlimits import outcome_records
from buildlimits.outcome_key import Salt
from buildlimits.outcome_ports import StoreResult
from buildlimits.outcome_store import StoreWriter

_BASE_TIME = dt.datetime(2026, 10, 25, tzinfo=dt.UTC)
_DAY_SECONDS = 24 * 60 * 60


class OutcomeStoreMachine(RuleBasedStateMachine):
    """Exercise real store operations against an ordered reference model."""

    reached: typ.ClassVar[set[str]] = set()
    root: typ.ClassVar[Path | None] = None
    next_example: typ.ClassVar[int] = 0

    def __init__(self) -> None:
        """Create an isolated store and force its required non-vacuous paths."""
        super().__init__()
        root = self.root
        if root is None:
            msg = "the generated store root is configured by its pytest fixture"
            raise RuntimeError(msg)
        self.path = root / f"cache-outcomes-{self.next_example}.jsonl"
        type(self).next_example += 1
        self.wall_time = _BASE_TIME
        self.monotonic_time = 100.0
        self.last_wall_time = self.wall_time
        self.last_monotonic_time = self.monotonic_time
        self.clock_skew_pending = False
        self.expected = []
        self._startup_results: tuple[StoreResult, ...] = ()
        self.writer, self.salt = self._start_writer()

        self._append("expiring")
        self._advance_in_sync(31)
        self._restart()
        assert not self.expected, "the generated prefix reaches record expiry"
        self._mark("expiry")

        self._append("skew-survivor")
        self._advance_with_skew(3)
        self._restart()
        assert len(self.expected) == 1, "clock skew preserves old records"
        self._mark("clock-skew")

        self._append("backward-survivor")
        self._advance_with_skew(3, backwards=True)
        self._restart()
        assert len(self.expected) == 2, "a backward jump preserves recent records"
        self._mark("backward-clock-jump")

        self._append("tail-prefix")
        self._restart(torn_tail=True)
        self._mark("torn-tail")

        self._append("old-salt")
        self._restart(change_salt=True)
        assert not self.expected, "a changed salt clears prior history"
        self._mark("salt-change")

    def _mark(self, name: str) -> None:
        """Record both an event and an assertion-visible non-vacuity marker."""
        self.reached.add(name)
        record_generated_case(name)

    def _start_writer(self) -> tuple[StoreWriter, Salt]:
        """Start the real writer and wait for its asynchronous startup result."""
        writer = StoreWriter(
            self.path,
            queue_capacity=32,
            wall_clock=lambda: self.wall_time,
            monotonic_clock=lambda: self.monotonic_time,
        )
        salt = wait_for_salt(writer)
        if not isinstance(salt, Salt):
            writer.close(1.0)
            raise TypeError("the generated store has a readable salt")
        load_results = wait_for_results(writer, operation="load")
        compact_results = wait_for_results(writer, operation="compact")
        self._startup_results = (*load_results, *compact_results)
        return writer, salt

    def _append(self, version: str) -> None:
        """Append one generated row and update the model after writer success."""
        record = window_summary(server_version=version, observed_at=self.wall_time)
        self.writer.append(record)
        results = wait_for_results(self.writer, operation="append", reason="none")
        assert StoreResult("append", "none", 1, 0) in results, (
            "the generated append is acknowledged"
        )
        self.expected.append(record)

    def _advance_in_sync(self, days: int) -> None:
        """Move wall and monotonic clocks together by the generated interval."""
        self.wall_time += dt.timedelta(days=days)
        self.monotonic_time += days * _DAY_SECONDS

    def _advance_with_skew(self, days: int, *, backwards: bool = False) -> None:
        """Move wall time in either direction against monotonic time."""
        direction = -1 if backwards else 1
        self.wall_time += dt.timedelta(days=direction * days)
        self.monotonic_time += 1.0

    def _compact_model(self) -> bool:
        """Apply the writer's retention, insertion-order and clock rules."""
        wall_elapsed = (self.wall_time - self.last_wall_time).total_seconds()
        monotonic_elapsed = self.monotonic_time - self.last_monotonic_time
        if abs(wall_elapsed - monotonic_elapsed) > _DAY_SECONDS:
            self.clock_skew_pending = True
        skipped_ageing = self.clock_skew_pending
        if self.clock_skew_pending:
            self._mark("clock-skew")
        self._apply_model_retention(should_age=not skipped_ageing)
        self.clock_skew_pending = False
        self.last_wall_time = self.wall_time
        self.last_monotonic_time = self.monotonic_time
        return skipped_ageing

    def _apply_model_retention(self, *, should_age: bool) -> None:
        """Apply the shared age and count limits to the expected row sequence."""
        if should_age:
            cutoff = self.wall_time - dt.timedelta(days=outcome_records.RETENTION_DAYS)
            before = len(self.expected)
            self.expected = [
                record for record in self.expected if record.observed_at >= cutoff
            ]
            if len(self.expected) < before:
                self._mark("expiry")
        if len(self.expected) > outcome_records.MAX_COMPACT_RECORDS:
            self.expected = self.expected[-outcome_records.MAX_COMPACT_RECORDS :]
            self._mark("retention-bound")

    def _model_startup_compaction(self) -> None:
        """Age rows after reload, whose new monotonic baseline has no jump."""
        self._apply_model_retention(should_age=True)

    def _close_writer(self) -> tuple[StoreResult, ...]:
        """Stop the worker and return its final queued operation results."""
        self.writer.close(1.0)
        assert not self.writer._thread.is_alive(), "close completes within its bound"
        return self.writer.results()

    def _restart(self, *, torn_tail: bool = False, change_salt: bool = False) -> None:
        """Compact, optionally damage or rotate the store, then reload it."""
        skipped_ageing = self._compact_model()
        previous_salt = self.salt
        close_results = self._close_writer()
        assert any(result.operation == "compact" for result in close_results), (
            "close runs final compaction"
        )
        if skipped_ageing:
            assert any(
                result.operation == "compact" and result.reason == "clock_skew"
                for result in close_results
            ), "a clock jump skips ageing and reports its reason"
        assert stored_records(self.path) == tuple(self.expected), (
            "the close compaction matches the ordered model"
        )

        if torn_tail:
            self._append_torn_utf8_tail()
            self._mark("torn-tail")
        if change_salt:
            self._replace_salt(previous_salt)

        self.writer, self.salt = self._start_writer()
        if self.salt.salt_id != previous_salt.salt_id:
            self.expected = []
            self._mark("salt-change")
            assert any(
                result.operation == "load" and result.reason == "salt_changed"
                for result in self._startup_results
            ), "reload reports a salt change"
        else:
            assert any(
                result.operation == "load" and result.reason == "none"
                for result in self._startup_results
            ), "reload keeps a valid same-salt store"
            self._model_startup_compaction()
        assert stored_records(self.path) == tuple(self.expected), (
            "reload matches the ordered reference model"
        )

    def _append_torn_utf8_tail(self) -> None:
        """Append a final record cut inside its multibyte value."""
        row = window_summary(server_version="torn-é", observed_at=self.wall_time)
        append_torn_utf8_tail(self.path, row)

    def _replace_salt(self, previous_salt: Salt) -> None:
        """Install different valid key material without replacing the store."""
        replacement = b"n" * 32
        if replacement == previous_salt.key:
            replacement = b"m" * 32
        path = salt_path(self.path)
        path.write_bytes(replacement)
        path.chmod(0o600)

    @rule(version=st.integers(min_value=0, max_value=10_000))
    def append_generated_row(self, version: int) -> None:
        """Add a generated row while preserving insertion order."""
        self._append(f"generated-{version}")

    @rule(days=st.integers(min_value=0, max_value=35))
    def advance_wall_and_monotonic_clock(self, days: int) -> None:
        """Generate a normal clock advance before a future compaction."""
        self._advance_in_sync(days)

    @rule(
        days=st.integers(min_value=2, max_value=40),
        backwards=st.booleans(),
    )
    def jump_wall_clock(self, days: int, backwards: bool) -> None:
        """Generate forward and backward jumps against monotonic time."""
        self._advance_with_skew(days, backwards=backwards)

    @rule()
    def compact_and_reload(self) -> None:
        """Exercise the writer's close compaction and a subsequent file load."""
        self._restart()

    @rule()
    def reload_after_torn_utf8_tail(self) -> None:
        """Recover validated rows after a generated incomplete UTF-8 row."""
        self._restart(torn_tail=True)

    @rule()
    def reload_after_salt_change(self) -> None:
        """Exercise privacy isolation when a valid replacement salt appears."""
        self._restart(change_salt=True)

    @invariant()
    def persisted_rows_match_the_model(self) -> None:
        """Every completed action leaves only the modelled complete records."""
        assert stored_records(self.path) == tuple(self.expected), (
            "persisted rows match the ordered reference model"
        )

    def teardown(self) -> None:
        """Close the writer before pytest removes this example's private files."""
        close_results = self._close_writer()
        assert any(result.operation == "compact" for result in close_results), (
            "teardown runs the writer's final compaction"
        )


def test_generated_store_operations_reach_required_branches(tmp_path: Path) -> None:
    """Generated operation sequences exercise expiry, skew, tails and salts."""
    OutcomeStoreMachine.root = tmp_path
    OutcomeStoreMachine.next_example = 0
    OutcomeStoreMachine.reached = set()
    run_state_machine_as_test(
        OutcomeStoreMachine,
        settings=settings(max_examples=5, stateful_step_count=12, deadline=None),
    )

    required = {
        "expiry",
        "clock-skew",
        "backward-clock-jump",
        "torn-tail",
        "salt-change",
    }
    assert required <= OutcomeStoreMachine.reached, (
        "every generated sequence reaches the non-vacuous startup contract"
    )


def test_generated_compaction_reaches_the_retention_bound() -> None:
    """Generated compact input evicts only the oldest row at the real bound."""
    reached: set[str] = set()

    @settings(max_examples=1, deadline=None)
    @given(seed=st.integers(min_value=0, max_value=10_000))
    def verify(seed: int) -> None:
        """Generate a valid store just beyond its real retention bound."""
        now = _BASE_TIME
        header = outcome_records.StoreHeader("generated-salt", now)
        records = tuple(
            window_summary(server_version=f"{seed}-{index}", observed_at=now)
            for index in range(outcome_records.MAX_COMPACT_RECORDS + 1)
        )

        compacted = outcome_records.compact(
            outcome_records.DecodedStore(header, records), now=now
        )

        assert compacted.records == records[1:], (
            "the count bound retains the newest rows in insertion order"
        )
        assert compacted.discarded == 1, "only the oldest row is counted as evicted"
        reached.add("retention-bound")
        record_generated_case("retention-bound")

    verify()

    assert reached == {"retention-bound"}, (
        "generation crossed the actual 10,000-row cap"
    )
