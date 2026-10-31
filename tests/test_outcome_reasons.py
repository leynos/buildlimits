"""Outcome vocabulary acceptance and non-inference contracts."""

import typing as typ

import pytest
from outcome_vocabulary_test_support import OUTCOME_ALIASES as _OUTCOME_ALIASES

from buildlimits import metrics, metrics_schema

_EVENT_BY_FIELD = {
    "cache_result": "cache_outcome",
    "outcome_attribution": "cache_outcome",
    "outcome_reason": "cache_outcome",
    "store_reason": "outcome_store",
    "evaluation_reason": "outcome_reading",
    "store_operation": "outcome_store",
    "gate_reason": "outcome_reading",
}
_OMITTED_TOKEN = ("outcome_attribution", "inferred")
_OUTCOME_TOKEN_ROWS = tuple(
    (name, token)
    for name, alias in _OUTCOME_ALIASES.items()
    for token in typ.cast("tuple[str, ...]", typ.get_args(alias.__value__))
    if (name, token) != _OMITTED_TOKEN
)
_OUTCOME_TOKEN_IDS = tuple(f"{name}-{token}" for name, token in _OUTCOME_TOKEN_ROWS)


def test_outcome_token_ids_match_schema_and_literal_aliases() -> None:
    """Keep token examples aligned with schema vocabularies and domain types."""
    literal_ids = set()
    schema_ids = set()
    for name, alias in _OUTCOME_ALIASES.items():
        literal_tokens = set(typ.get_args(alias.__value__))
        schema_tokens = set(metrics_schema.VOCABULARIES[name])
        if name == "outcome_attribution":
            assert "inferred" in literal_tokens & schema_tokens, (
                "inferred remains a reserved schema token"
            )
            literal_tokens.remove("inferred")
            schema_tokens.remove("inferred")
        literal_ids.update(f"{name}-{token}" for token in literal_tokens)
        schema_ids.update(f"{name}-{token}" for token in schema_tokens)

    assert set(_OUTCOME_TOKEN_IDS) == literal_ids, (
        "the examples cover each emitted Literal token"
    )
    assert set(_OUTCOME_TOKEN_IDS) == schema_ids, (
        "the examples cover each registered schema token"
    )
    assert _OMITTED_TOKEN not in _OUTCOME_TOKEN_ROWS, (
        "inferred is reserved but never an emitted attribution"
    )


@pytest.mark.parametrize(
    ("field", "token"), _OUTCOME_TOKEN_ROWS, ids=_OUTCOME_TOKEN_IDS
)
def test_each_outcome_token_reaches_the_metrics_sink(
    field: str, token: str, metric_sink
) -> None:
    """Emit every approved outcome token through the fixed metrics boundary."""
    metrics.Metrics(metric_sink).emit(_EVENT_BY_FIELD[field], **{field: token})

    assert metric_sink.records[-1][f"BUILD_LIMITS_{field.upper()}"] == token, (
        f"{field} token {token!r} passes schema validation"
    )
