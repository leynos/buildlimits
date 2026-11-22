"""Row builders for the cache-outcome H2 replay contracts.

An H2 verdict depends on exact sample sizes and on how a key's hit rows pair
with its miss rows. These builders make those counts explicit, so each test
names the history it scores instead of encoding it again in its own body.
"""

import dataclasses as dc
import typing as typ

from outcome_test_helpers import OBSERVED_AT, cache_key

from buildlimits.outcome_record_format import ChildSighting, OutcomeRecord

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_key import CacheKey
    from buildlimits.outcome_vocabulary import CacheResult


def key(index: int) -> CacheKey:
    """Create a stable, path-free key for one synthetic history.

    Returns
    -------
    CacheKey
        One key with a unique, non-path argument digest.
    """
    return dc.replace(cache_key(), argument_digest=f"{index:064x}")


def outcome(
    key: CacheKey,
    label: CacheResult,
    *,
    runtime: float,
    sighting: ChildSighting = "not_seen",
) -> OutcomeRecord:
    """Build one store row with explicit H2 inputs.

    Returns
    -------
    OutcomeRecord
        One deterministic cache history row.
    """
    return OutcomeRecord(
        key=key,
        outcome=label,
        origin="registry",
        runtime_seconds=runtime,
        observed_at=OBSERVED_AT,
        parser_class="cacheable",
        child_sighting=sighting,
        child_samples=0 if label == "failed" else 1,
    )


def rows(
    evaluable: int,
    correct: int,
    attributed: int,
    *,
    status_overrides: dict[int, ChildSighting] | None = None,
) -> tuple[OutcomeRecord, ...]:
    """Build paired key histories with a chosen number of combined matches.

    Returns
    -------
    tuple[OutcomeRecord, ...]
        Sampled paired rows plus unattributed-to-H2 supporting rows.
    """
    overrides = {} if status_overrides is None else status_overrides
    records = [
        _paired_row(index, evaluable, correct, overrides) for index in range(evaluable)
    ]
    records.extend(
        outcome(key(100_000 + index), "failed", runtime=0.25)
        for index in range(len(records), attributed)
    )
    return tuple(records)


def streamed_hits(key: CacheKey, count: int) -> cabc.Iterator[OutcomeRecord]:
    """Yield ``count`` sampled hits for one key without collecting them.

    Yields
    ------
    OutcomeRecord
        One fast hit row for the key, produced on demand.
    """
    for _ in range(count):
        yield outcome(key, "hit", runtime=0.25)


def _paired_row(
    index: int,
    evaluable: int,
    correct: int,
    overrides: dict[int, ChildSighting],
) -> OutcomeRecord:
    """Build one sampled paired row, honouring an explicit sighting override.

    Returns
    -------
    OutcomeRecord
        The paired hit or miss row for ``index``.
    """
    key_index, label = _row_identity(index, evaluable)
    is_correct = index < correct or index in overrides
    sighting = (
        overrides[index]
        if index in overrides
        else _sighting(label, is_correct=is_correct)
    )
    runtime = _row_runtime(label, is_correct=is_correct)
    if index in overrides and label == "miss":
        runtime = 2.0
    return outcome(key(key_index), label, runtime=runtime, sighting=sighting)


def _row_identity(index: int, evaluable: int) -> tuple[int, CacheResult]:
    """Choose the paired key and label for one evaluable row index.

    Rows in the paired region alternate hit and miss on shared keys; any
    remainder row is a single extra hit on the first key.

    Returns
    -------
    tuple[int, CacheResult]
        Key index and the row's hit or miss label.
    """
    if index < evaluable // 2 * 2:
        return index // 2, "hit" if index % 2 == 0 else "miss"
    return 0, "hit"


def _sighting(label: CacheResult, *, is_correct: bool) -> ChildSighting:
    """Choose the child sighting that matches the predicted correctness.

    Returns
    -------
    ChildSighting
        The sighting value implied by the label and correctness.
    """
    if label == "hit":
        return "not_seen" if is_correct else "seen"
    return "seen" if is_correct else "not_seen"


def _row_runtime(label: CacheResult, *, is_correct: bool) -> float:
    """Choose the runtime that carries the row's prediction.

    Returns
    -------
    float
        Hit rows stay fast; miss rows are slow only when predicted correctly.
    """
    if label == "hit":
        return 0.25
    return 2.0 if is_correct else 0.5
