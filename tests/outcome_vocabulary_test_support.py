"""Shared aliases for outcome-token schema and sink contract tests."""

from buildlimits.outcome_recorder_types import AttributionKind
from buildlimits.outcome_vocabulary import (
    CacheResult,
    EvaluationReason,
    GateReason,
    OutcomeReason,
    StoreOperation,
    StoreReason,
)

OUTCOME_ALIASES = {
    "cache_result": CacheResult,
    "outcome_attribution": AttributionKind,
    "outcome_reason": OutcomeReason,
    "store_reason": StoreReason,
    "evaluation_reason": EvaluationReason,
    "store_operation": StoreOperation,
    "gate_reason": GateReason,
}
