"""Hand-computed answers for the benchmark's published scores.

The four-event history here is small enough that every expected value can be
worked out from the predictor definitions, rather than read back from a run.
"""

import dataclasses as dc
import datetime as dt
import typing as typ
from pathlib import Path

import pytest
from predict_test_helpers import benchmark_argv, read_results, write_json
from predict_timing_helpers import at

from buildlimits.predict import benchmark_cli
from buildlimits.predict.config_loader import load_config
from buildlimits.predict.dataset import Snapshot, Source
from buildlimits.predict.events import EPOCH, Event, Label
from buildlimits.predict.journal_provenance import JournalUnavailable
from buildlimits.predict.report_codec import config_to_dict
from buildlimits.predict.snapshot_store import save_snapshot

IDENTITY_A = "a" * 64
IDENTITY_B = "b" * 64


def _hand_events() -> tuple[Event, ...]:
    """Return four events whose released history is small enough to compute.

    Each event is admitted ten seconds before its own label, so a label is
    released strictly before the next admission. Identity A repeats, giving the
    last-outcome predictor a previous label to repeat, while identity B is seen
    once and makes it abstain.

    Returns
    -------
    tuple[Event, ...]
        The four events, in admission order.
    """

    def event(
        identity: str, moments: tuple[int, int], label: str, attempt: str
    ) -> Event:
        """Return one verified event at its admission and label moments."""
        admitted, observed = moments
        return Event(
            identity,
            "git",
            at(admitted),
            at(observed),
            typ.cast("Label", label),
            1.0,
            label_observed_verified=True,
            completed_at=at(observed),
            attempt=attempt,
            supervisor_generation="hand-generation",
        )

    return (
        event(IDENTITY_A, (0, 10), "hit", "g1"),
        event(IDENTITY_A, (20, 30), "miss", "g2"),
        event(IDENTITY_B, (40, 50), "hit", "g3"),
        event(IDENTITY_A, (60, 70), "miss", "g4"),
    )


def _write_hand_snapshot(root: Path) -> Path:
    """Write the four-event history as a snapshot the command can read.

    Returns
    -------
    Path
        The snapshot's path.
    """
    path = root / "hand.jsonl"
    save_snapshot(
        path,
        Snapshot(
            created_at=EPOCH + dt.timedelta(days=7),
            source=Source("hand", EPOCH + dt.timedelta(days=7), 4, 4, 0),
            journal=JournalUnavailable("journal query skipped"),
            events=_hand_events(),
            gaps=(),
        ),
    )
    return path


def _write_hand_config(root: Path) -> Path:
    """Write parameters that make every row a held-out prediction.

    Returns
    -------
    Path
        The parameter file's path.
    """
    config = dc.replace(
        load_config(),
        train_fraction=0.0,
        cold_start=0.5,
        calibration_bins=10,
        block_size=2,
        bootstrap_resamples=200,
        minimum_labelled_events=1,
        minimum_scored_events=1,
    )
    return write_json(root / "hand-config.json", config_to_dict(config))


@pytest.fixture(scope="module")
def hand_computed_run(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[dict[str, typ.Any], str]:
    """Run the benchmark over the four-event history once.

    A train fraction of zero puts every row in the held-out period, so the
    released history at each admission, and therefore every expected value,
    stays small enough to work out without the harness.

    Returns
    -------
    tuple[dict[str, object], str]
        The parsed results document and the rendered Markdown.
    """
    root = tmp_path_factory.mktemp("hand")
    output = root / "out"
    status = benchmark_cli.main(
        benchmark_argv(_write_hand_snapshot(root), output, _write_hand_config(root))
    )
    assert status == 0, "the hand-built run completes"
    return read_results(output)


def test_a_held_out_brier_matches_the_hand_computed_value(
    hand_computed_run: tuple[dict[str, typ.Any], str],
) -> None:
    """The base rate's held-out Brier is the mean of four known squared errors.

    Its answer is 0.5 before any label, then 1.0, 0.5 and two thirds as the
    released labels accumulate; the outcomes are hit, miss, hit, miss.
    """
    document, _ = hand_computed_run
    heldout = document["predictors"]["base-rate"]["held-out"]

    expected = ((0.5 - 1) ** 2 + (1.0 - 0) ** 2 + (0.5 - 1) ** 2 + (2 / 3 - 0) ** 2) / 4
    assert heldout["scored"] == 4, "all four rows were answered"
    assert heldout["brier"] == pytest.approx(expected), "the hand-computed mean"


def test_a_calibration_bin_matches_the_hand_computed_counts(
    hand_computed_run: tuple[dict[str, typ.Any], str],
) -> None:
    """Ten bins hold the four answers exactly where their probabilities fall."""
    document, _ = hand_computed_run
    heldout = document["predictors"]["base-rate"]["held-out"]
    bins = heldout["calibration"]

    assert len(bins) == 10, "the configured bin count is reported"
    assert bins[5]["count"] == 2, "both answers at 0.5 share the bin"
    assert bins[5]["observed_rate"] == pytest.approx(1.0), "and both were hits"
    assert bins[6]["count"] == 1, "two thirds lands in the next bin"
    assert bins[6]["observed_rate"] == pytest.approx(0.0), "and was a miss"
    assert bins[9]["count"] == 1, "a certain answer lands in the last bin"
    assert bins[9]["observed_rate"] == pytest.approx(0.0), "and was wrong"
    assert sum(bin_["count"] for bin_ in bins) == heldout["scored"], (
        "the bins partition every answered row"
    )


def test_a_comparison_interval_matches_the_hand_computed_pairing(
    hand_computed_run: tuple[dict[str, typ.Any], str],
) -> None:
    """Last-outcome abstains on first sightings, leaving two paired rows.

    It answers one at the first paired row, matching the base rate, and zero
    at the second, where the base rate answered two thirds. The mean
    difference is therefore -2/9 over two answered positions in two blocks of
    two, across four stream positions.
    """
    document, _ = hand_computed_run
    interval = document["predictors"]["last-outcome"]["against_base_rate"]

    assert interval is not None, "the abstentions still leave paired rows"
    assert interval["difference"] == pytest.approx(-2 / 9), "the hand-computed mean"
    assert interval["rows"] == 2, "two positions were paired"
    assert interval["positions"] == 4, "the span keeps every stream position"
    assert interval["block_size"] == 2, "the configured block size"
    assert interval["blocks"] == 2, "each block holds one answered position"
    assert interval["low"] <= interval["difference"] <= interval["high"], (
        "the interval brackets the observed difference"
    )
