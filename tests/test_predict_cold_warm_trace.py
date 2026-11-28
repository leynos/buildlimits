"""One cold then warm compilation traced from admission to replay.

The controlled sccache check proves the observer reads a real server; the shell
suites prove the recorder writes rows. This test joins the two ends: two cached
invocations are admitted through the real admission state machine, recorded by
the real shell and reducer, written through the store's own writer, read back,
snapshotted, loaded and replayed. Every assertion names a value that would be
lost if one link stopped carrying it.
"""

import datetime as dt
import typing as typ

from differential_trace_test_helpers import CompletingStatistics, cached_entry
from outcome_shell_group_helpers import run_cached_group
from outcome_shell_test_support import (
    OutcomeWorldOptions,
    make_outcome_world,
)
from outcome_test_helpers import STORE_EPOCH, stats_text
from predict_test_helpers import predict_config

from buildlimits import outcome_store_io
from buildlimits.outcome_ports import StatisticsText
from buildlimits.outcome_record_codec import decode_lines
from buildlimits.outcome_record_format import (
    FORMAT_VERSION,
    DecodedStore,
    OutcomeRecord,
    StoreHeader,
)
from buildlimits.predict.accounting import HistoryLoss
from buildlimits.predict.dataset import build_snapshot
from buildlimits.predict.events import label_moment
from buildlimits.predict.harness import walk_forward
from buildlimits.predict.history import released_labels
from buildlimits.predict.records import decode_history
from buildlimits.predict.snapshot_store import load_snapshot, save_snapshot
from buildlimits.predict.timing import audit

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.pending_request import Pending
    from buildlimits.supervisor_outcomes import OutcomeShell

COLD_AT = dt.datetime(2026, 11, 26, 8, 0, tzinfo=dt.UTC)
"""The cold invocation's admission moment."""

WARM_AT = COLD_AT + dt.timedelta(minutes=1)
"""The warm invocation's admission moment."""

READING_AT = COLD_AT + dt.timedelta(minutes=2)
"""The wall-clock moment every reading in this trace reports."""

# sccache reports cumulative counters, so each reading is the running total.
# The first two establish the baseline, the third is the cold compilation's
# miss, and the fourth is the same compilation served from the cache.
_READINGS: tuple[dict[str, int], ...] = (
    {},
    {},
    {
        "compile_requests": 1,
        "compilations": 1,
        "cache_misses": 1,
        "requests_executed": 1,
    },
    {
        "compile_requests": 2,
        "compilations": 1,
        "cache_misses": 1,
        "cache_hits": 1,
        "requests_executed": 2,
    },
)


class _ColdThenWarm(CompletingStatistics):
    """Report a cold miss for one labelled window, then a warm hit.

    ``CompletingStatistics`` repeats one footprint, which cannot express a
    compilation that misses and is then served from the cache. The variant
    answers the same port interface, so the recorder cannot tell them apart.
    """

    def poll(self, ticket: object, now: float) -> StatisticsText:
        """Return the baseline, then the cold miss, then the warm hit.

        Returns
        -------
        StatisticsText
            The scripted reading for this poll.
        """
        super().poll(typ.cast("typ.Any", ticket), now)
        index = min(self.polls, len(_READINGS)) - 1
        return StatisticsText(stats_text(**_READINGS[index]))


def _run_two_invocations(tmp_path: Path) -> tuple[OutcomeRecord, ...]:
    """Drive a cold and a warm cached invocation through the shell's own path.

    Returns
    -------
    tuple[OutcomeRecord, ...]
        The rows the shell's writer was handed, in reading order.
    """
    world = make_outcome_world(
        tmp_path,
        OutcomeWorldOptions(
            statistics_port=_ColdThenWarm(footprint="miss"),
            wall_clock=lambda: READING_AT,
        ),
    )
    shell = typ.cast("OutcomeShell", world.shell)
    for tick in (0.0, 0.1):
        shell.on_tick(tick)
    for admitted, request_id, ticks in (
        (None, 1, (1.1, 2.1, 2.2)),
        (COLD_AT, 2, (3.3, 4.1, 4.2)),
        (WARM_AT, 3, (5.5, 6.3, 6.4)),
    ):
        entry: Pending = cached_entry()
        if admitted is not None:
            entry.admitted_at = admitted
        run_cached_group(world, shell, entry, request_id)
        for tick in ticks:
            shell.on_tick(tick)
    shell.close(timeout=0.1)
    return tuple(
        record
        for record in world.writer.records
        if isinstance(record, OutcomeRecord) and record.outcome != "unattributed"
    )


def test_cold_and_warm_invocations_survive_the_whole_path(tmp_path: Path) -> None:
    """The trace keeps identity, times and provenance from admission to replay."""
    rows = _run_two_invocations(tmp_path)
    assert len(rows) == 2, "each labelled window emits one row"
    cold, warm = rows

    assert (cold.outcome, warm.outcome) == ("miss", "hit"), (
        "the scripted cold miss and warm hit reach the store rows"
    )
    for row in rows:
        assert row.attempt is not None, "each row names its invocation"
        assert row.supervisor_generation is not None, "and the clock domain"
        assert row.label_time_provenance == "verified", (
            "the shell dates each row at its own reading"
        )
        assert row.completed_at is not None, "completion is recorded, not inferred"
        assert row.admitted_at is not None, "admission is recorded"
        assert row.admitted_at <= row.completed_at <= row.observed_at, (
            "decision, completion and label are distinct ordered moments"
        )
        assert row.generation_digest is not None, (
            "the reading's sccache generation is retained"
        )
    assert cold.attempt != warm.attempt, "two invocations never share an attempt"

    store = tmp_path / "cache-outcomes.jsonl"
    outcome_store_io.replace_store(
        store,
        StoreHeader("fixed-salt", STORE_EPOCH, version=FORMAT_VERSION),
        typ.cast("typ.Any", rows),
    )
    decoded = decode_lines(store.read_text(encoding="utf-8"))
    assert isinstance(decoded, DecodedStore), "the store's own reader accepts the rows"
    assert decoded.records == rows, "every recorded field survives the store"

    snapshot = build_snapshot(
        decode_history(decoded),
        now=STORE_EPOCH,
        history_loss=HistoryLoss.active_file_only(),
    )
    path = tmp_path / "history.jsonl"
    save_snapshot(path, snapshot)
    events = load_snapshot(path).events
    assert len(events) == 2, "both invocations reach the snapshot"

    verdict = audit(events)
    assert verdict.reasons == (None, None), "neither invocation is excluded"
    assert verdict.uncorrelated == 0, "both name a complete attempt pair"
    walk = walk_forward(events, predict_config())
    assert sum(len(group) for group in walk.moments) == 2, (
        "the replay answers both admissions"
    )
    released = released_labels(events)
    assert [moment for moment, _index in released] == [
        label_moment(event) for event in events
    ], "each label is released at its own reading moment"
    assert all(event.generation_digest is not None for event in events), (
        "the snapshot carries the generation the store recorded"
    )
