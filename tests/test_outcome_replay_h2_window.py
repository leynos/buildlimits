"""H2's runtime window: its threshold semantics and its fixed memory bound.

The runtime clue compares a miss with the median of the key's earlier hits. A
key can be built hundreds of thousands of times, so the fold keeps one bounded
window of recent hit runtimes rather than one value per hit in the whole
history.
"""

import tracemalloc

from outcome_replay_h2_helpers import key as _key
from outcome_replay_h2_helpers import outcome as _outcome
from outcome_replay_h2_helpers import streamed_hits as _streamed_hits

from buildlimits import outcome_replay, outcome_replay_scoring


def test_the_runtime_baseline_follows_the_recent_hit_window() -> None:
    """The median follows the key's recent hits, not every hit it ever had.

    A key built thousands of times would otherwise keep one float per hit for
    the whole replay. The key here ran fast early and slow recently, so the
    two baselines disagree: the recent median is 0.5 s and its threshold is
    1.5 s, while the whole-history median is 0.1 s and its threshold is the
    1.0 s floor.
    """
    key = _key(1)
    window = outcome_replay_scoring.H2_RUNTIME_WINDOW
    rows = (
        *(_outcome(key, "hit", runtime=0.1) for _ in range(window * 2)),
        *(_outcome(key, "hit", runtime=0.5) for _ in range(window)),
        _outcome(key, "miss", runtime=1.2, sighting="ambiguous"),
    )

    report = outcome_replay.evaluate_h2(rows)

    assert report.evaluable_records == window * 3 + 1, "every sampled row is scored"
    assert report.combined_correct == window * 3, (
        "the 1.2 s miss stays inside the recent window's 1.5 s threshold"
    )


def test_scoring_a_hot_key_holds_one_fixed_runtime_window() -> None:
    """Scoring a key built many times costs one window, not one value per hit.

    A hundred thousand hits trace to a few megabytes when the fold keeps them
    all, which is the growth this fold must not have.
    """
    key = _key(7)
    count = 100_000

    tracemalloc.start()
    try:
        counters = outcome_replay_scoring.score_h2(
            _streamed_hits(key, count), frozenset({key})
        )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert counters["evaluable"] == count, "every sampled row of a paired key is scored"
    assert peak < 1024 * 1024, (
        f"scoring {count} hits on one key peaked at {peak} traced bytes"
    )
