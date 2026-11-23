"""Retention bounds are exact, oldest-first, and survive a restart.

The writer applies the policy in place; the archiver applies it to closed
history. Both take the same policy object, so these tests state the boundary
behaviour once: what is kept at the exact age and byte bounds, which rows go
first, and what happens when a deployment overrides the legacy defaults or the
wall clock moves under a running writer.
"""

import datetime as dt
import typing as typ
from pathlib import Path
from unittest import mock

import pytest
from outcome_archive_helpers import NOW, records
from outcome_retention_helpers import (
    LARGE_STORE_TIMEOUT,
    decoded,
    encoded_size,
    large_store,
    policy,
    rohga_policy,
    running_writer,
    seed,
    startup,
)
from outcome_store_helpers import (
    close_writer,
    stored_records,
    wait_for_results,
    wait_for_salt,
)
from outcome_test_helpers import attributed_record
from segment_test_helpers import store_layout

from buildlimits import config, outcome_records, outcome_store_state
from buildlimits.outcome_key import Salt
from buildlimits.outcome_ports import StoreResult
from buildlimits.outcome_record_encode import header_floor_bytes
from buildlimits.outcome_record_format import MAX_COMPACT_BYTES
from buildlimits.outcome_retention import LEGACY_MAX_RECORDS, policy_from_settings
from buildlimits.outcome_segment_inventory import read_inventory
from buildlimits.outcome_store import StoreWriter

if typ.TYPE_CHECKING:
    from buildlimits.outcome_archive_history import ActiveHistory


def test_a_row_exactly_at_the_age_cutoff_is_kept() -> None:
    """Age eviction uses ``>=`` cutoff, so one second decides the boundary."""
    cutoff = NOW - dt.timedelta(days=30)
    rows = (
        attributed_record(observed_at=cutoff - dt.timedelta(seconds=1)),
        attributed_record(observed_at=cutoff),
        attributed_record(observed_at=NOW),
    )

    compacted = outcome_records.compact(decoded(rows), now=NOW, policy=policy())

    assert compacted.records == rows[1:], "the row exactly at the cutoff survives"
    assert compacted.discarded == 1, "only the older row is evicted"
    assert compacted.store_reason == "expired", "the age bound names itself"


def test_history_below_both_bounds_is_untouched() -> None:
    """A store inside its age, byte and count bounds is left as it was."""
    rows = records(NOW - dt.timedelta(hours=1), 3)

    compacted = outcome_records.compact(
        decoded(rows), now=NOW, policy=policy(max_records=3)
    )

    assert compacted.records == rows, "no row is evicted below every bound"
    assert compacted.discarded == 0, "nothing was discarded"
    assert compacted.store_reason == "none", "no eviction cause is reported"


def test_the_byte_bound_is_exact() -> None:
    """The budget counts the header and its newlines; one byte decides."""
    rows = records(NOW - dt.timedelta(minutes=3), 3)
    exact = encoded_size(rows)
    above = outcome_records.compact(
        decoded(rows), now=NOW, policy=policy(max_bytes=exact)
    )
    below = outcome_records.compact(
        decoded(rows), now=NOW, policy=policy(max_bytes=exact - 1)
    )

    assert above.records == rows, "a store exactly at its budget is retained"
    assert above.store_reason == "none", "a store inside its budget names no bound"
    assert below.records == rows[1:], "one byte over evicts exactly one row"
    assert below.discarded == 1, "exactly the one row is discarded"
    assert below.store_reason == "bytes", "the byte bound names itself"


def test_the_oldest_rows_are_evicted_first() -> None:
    """A count cap removes leading rows and keeps the newest ones."""
    rows = records(NOW - dt.timedelta(minutes=4), 4)

    compacted = outcome_records.compact(
        decoded(rows), now=NOW, policy=policy(max_records=2)
    )

    assert compacted.records == rows[2:], "the newest rows survive"
    assert compacted.discarded == 2, "one row went for each unit over the cap"
    assert compacted.store_reason == "records", "the record cap names itself"


def test_the_default_settings_still_cap_the_active_history(tmp_path: Path) -> None:
    """Compatibility: omitted retention keys keep the 10,000-row bound."""
    path, rows = large_store(tmp_path)

    results = startup(
        path,
        policy_from_settings(
            config.load(None),
            header_floor=header_floor_bytes(),
            segment_limit=MAX_COMPACT_BYTES,
        ),
        NOW,
        timeout=LARGE_STORE_TIMEOUT,
    )

    assert StoreResult("compact", "records", LEGACY_MAX_RECORDS, 1) in results, (
        "the legacy policy reports the one row it discarded"
    )
    assert stored_records(path) == rows[1:], "the oldest row is the one evicted"


def test_the_rohga_policy_keeps_a_history_over_ten_thousand_rows(
    tmp_path: Path,
) -> None:
    """Thirty days and 15 GB rotate the store instead of truncating it."""
    path, _rows = large_store(tmp_path)

    with running_writer(
        path, rohga_policy(tmp_path), NOW, timeout=LARGE_STORE_TIMEOUT
    ) as writer:
        wait_for_results(
            writer,
            operation="load",
            reason="none",
            timeout=LARGE_STORE_TIMEOUT,
        )
        sealed = wait_for_results(
            writer,
            operation="seal",
            reason="none",
            timeout=LARGE_STORE_TIMEOUT,
        )

    assert StoreResult("seal", "none", 10_001, 0) in sealed, (
        "the oversized active file is sealed whole, not trimmed"
    )
    inventory = read_inventory(store_layout(tmp_path))
    retained = sum(entry.record_count or 0 for entry in inventory.sealed)
    assert retained == 10_001, "every row is still retained after rotation"
    assert not inventory.gaps, "rotation leaves no sequence gap"


def test_a_restart_reads_the_same_history_the_writer_wrote(tmp_path: Path) -> None:
    """Each startup applies one injected clock to the store it just loaded."""
    path = tmp_path / "cache-outcomes.jsonl"
    rows = (
        attributed_record(observed_at=NOW - dt.timedelta(hours=3)),
        attributed_record(observed_at=NOW - dt.timedelta(minutes=90)),
        attributed_record(observed_at=NOW - dt.timedelta(minutes=30)),
        attributed_record(observed_at=NOW),
    )
    seed(path, rows)
    retention = policy(max_age=dt.timedelta(hours=2))

    first = startup(path, retention, NOW)
    after_first = stored_records(path)
    second = startup(path, retention, NOW + dt.timedelta(minutes=30))
    after_second = stored_records(path)
    third = startup(path, retention, NOW + dt.timedelta(hours=2))

    assert StoreResult("compact", "expired", 3, 1) in first, (
        "the first startup names the age eviction it performed"
    )
    assert after_first == rows[1:], "the row older than two hours is evicted"
    assert after_second == after_first, "a restart inside the bound changes nothing"
    assert StoreResult("compact", "none", 3, 0) in second, "the restart discarded none"
    assert after_first[0].observed_at == NOW - dt.timedelta(minutes=90), (
        "the row exactly at the cutoff survived the first pass"
    )
    assert stored_records(path) == rows[3:], "later time evicts the older rows"
    assert StoreResult("compact", "expired", 1, len(after_second) - 1) in third, (
        "the age eviction names its cause"
    )


def test_a_reduced_budget_trims_on_the_next_startup_and_settles(
    tmp_path: Path,
) -> None:
    """Lowering a configured cap takes effect at the next load, once."""
    path = tmp_path / "cache-outcomes.jsonl"
    rows = records(NOW - dt.timedelta(hours=5), 6)
    seed(path, rows)

    generous = startup(path, policy(max_records=6), NOW)
    reduced = startup(path, policy(max_records=3), NOW)
    settled = startup(path, policy(max_records=3), NOW)

    assert StoreResult("compact", "none", 6, 0) in generous, (
        "a generous cap leaves the store alone"
    )
    assert StoreResult("compact", "records", 3, 3) in reduced, (
        "the next startup trims to the reduced cap"
    )
    assert stored_records(path) == rows[3:], "the newest rows are the survivors"
    assert StoreResult("compact", "none", 3, 0) in settled, (
        "a second start finds nothing left to trim"
    )


@pytest.mark.parametrize(
    "delta",
    [dt.timedelta(days=2), -dt.timedelta(days=2)],
    ids=["forward", "backward"],
)
def test_a_clock_jump_skips_age_eviction(tmp_path: Path, delta: dt.timedelta) -> None:
    """A wall-clock jump never decides the fate of recorded history.

    A forward jump would otherwise look like weeks of age, and a backward jump
    like a future-dated store; either pass reports ``clock_skew`` and keeps the
    rows, leaving the next undisturbed run to apply the real age bound.
    """
    path = tmp_path / "cache-outcomes.jsonl"
    row = attributed_record(observed_at=NOW - dt.timedelta(minutes=30))
    seed(path, (row,))
    jumps = [NOW]
    monotonic = [0.0]

    def advance(jump: dt.timedelta) -> None:
        """Move the wall clock while monotonic time advances one second."""
        jumps[0] = NOW + jump
        monotonic[0] += 1.0

    writer = StoreWriter(
        path,
        policy=policy(max_age=dt.timedelta(hours=1)),
        wall_clock=lambda: jumps[0],
        monotonic_clock=lambda: monotonic[0],
    )
    try:
        if not isinstance(wait_for_salt(writer), Salt):
            pytest.fail("the writer publishes its active salt")
        wait_for_results(writer, operation="compact", reason="none")
        advance(delta)
    finally:
        close_writer(writer)

    results = wait_for_results(writer, operation="compact", reason="clock_skew")
    assert StoreResult("compact", "clock_skew", 1, 0) in results, (
        "the jumped pass reports the skew"
    )
    assert stored_records(path) == (row,), "no row is evicted on a suspect clock"


def test_the_status_snapshot_never_reads_closed_history(tmp_path: Path) -> None:
    """Reporting the active count must not open a sealed segment."""
    path = tmp_path / "cache-outcomes.jsonl"
    rows = records(NOW - dt.timedelta(hours=1), 2)
    seed(path, rows)
    sealed_dir = store_layout(tmp_path).sealed_dir
    sealed_dir.mkdir(parents=True)
    (sealed_dir / "segment-000000000001.jsonl").write_bytes(b"{" * 4096)

    with running_writer(path, policy(), NOW) as writer:
        wait_for_results(writer, operation="compact", reason="none")
        snapshot = writer.retention()

    assert snapshot.has_metadata is True, (
        "an unreadable segment cannot spoil the answer"
    )
    assert snapshot.records == len(rows), "only active rows are counted"
    assert snapshot.bytes == path.stat().st_size, "the size is the file's own"


def test_a_status_snapshot_never_pairs_a_row_with_an_earlier_state(
    tmp_path: Path,
) -> None:
    """A row on disk but not yet in the writer's rows is not half-counted.

    The worker writes a row's line before it adds the row to its in-memory
    rows. A status read in that window must report the last complete state
    rather than the new file size beside the old row count.
    """
    path = tmp_path / "cache-outcomes.jsonl"
    rows = records(NOW - dt.timedelta(hours=1), 1)
    seed(path, rows)
    append_line = outcome_store_state.append_line
    seen: list[tuple[ActiveHistory, int, int]] = []

    with running_writer(path, policy(), NOW) as writer:

        def read_before_the_row_lands(target: Path, line: bytes) -> None:
            """Write the row, then read the status as the append window opens."""
            append_line(target, line)
            seen.append((writer.retention(), target.stat().st_size, len(line) + 1))

        with mock.patch.object(
            outcome_store_state, "append_line", read_before_the_row_lands
        ):
            writer.append(attributed_record(observed_at=NOW))
            wait_for_results(writer, operation="append", reason="none")

    snapshot, size_on_disk, appended = seen[0]
    assert snapshot.has_metadata is True, "the writer loaded its rows"
    assert snapshot.records == len(rows), (
        "a row still in the append window is not counted"
    )
    assert size_on_disk == snapshot.bytes + appended, (
        "the row's bytes are not paired with the earlier row count"
    )
    assert writer.retention().records == len(rows) + 1, (
        "the final snapshot counts the appended row"
    )
