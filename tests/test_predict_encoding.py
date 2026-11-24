"""Shape and range contracts for the feature model's causal input."""

import typing as typ

from hypothesis import given, settings
from hypothesis import strategies as st

from buildlimits.predict.encoding import (
    IDENTITY_BUCKETS,
    LOGISTIC_WEIGHT_COUNT,
    ORIGIN_ORDER,
    _identity_bucket,
    logistic_features,
)
from buildlimits.predict.events import EPOCH, Features

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import Origin

GIT = typ.cast("Origin", "git")


def _row(*, origin: Origin = GIT, age: float | None = None) -> Features:
    """Return one feature row with a fixed identity and load."""
    return Features(
        identity="a" * 64,
        origin=origin,
        available_at=EPOCH,
        age_seconds=age,
        load=3,
    )


def test_the_vector_has_one_entry_per_model_weight() -> None:
    """The encoder and the learner must agree on the vector's width."""
    assert len(logistic_features(_row())) == LOGISTIC_WEIGHT_COUNT, (
        "one value per weight"
    )


def test_the_origin_block_marks_only_the_rows_own_origin() -> None:
    """Each origin has its own slot, in the order the model was pinned to."""
    features = logistic_features(_row(origin=typ.cast("Origin", "path")))

    block = features[1 : 1 + len(ORIGIN_ORDER)]

    assert block == (0.0, 0.0, 1.0, 0.0), "registry, git, path, unknown"


def test_an_absent_age_is_flagged_rather_than_imputed() -> None:
    """The model is told whether an age exists, so zero is not read as none."""
    absent = logistic_features(_row(age=None))
    present = logistic_features(_row(age=0.0))

    assert absent[-2:] == (0.0, 0.0), "no flag and no age term"
    assert present[-2:] == (1.0, 0.0), "the flag separates an age of zero"


@given(st.text())
@settings(max_examples=50, derandomize=True, deadline=None)
def test_every_identity_hashes_into_a_bucket(identity: str) -> None:
    """The bucket is stable for a name and always addresses a weight."""
    first = _identity_bucket(identity)
    second = _identity_bucket(identity)

    assert first == second, "the same identity always lands in the same bucket"
    assert 0 <= first < IDENTITY_BUCKETS, "the index stays inside the model"
