"""Generated invariants for the admission-readiness classifier."""

import dataclasses as dc
import re

from hypothesis import given
from hypothesis import strategies as st

from buildlimits.admission_readiness import ReadinessFacts, classify
from buildlimits.admission_state import Counts

_STATES = frozenset({
    "blocked",
    "closing",
    "draining",
    "ready",
    "saturated",
    "unhealthy",
})
_READY_STATES = frozenset({"ready", "saturated", "draining"})
_ACTIVE_PHASES = ("quiescing", "stopping", "accounting", "restarting")
_FAULT_KINDS = (None, "live_work", "stopped_populated", "manager")
_IDENTIFIER_LEAK = re.compile(r"\bg\d+\b|\brequest\s+\d+\b|(?:^|\s)/\S+")


@st.composite
def _readiness_facts(
    draw: st.DrawFn, *, full_retention: bool | None = None
) -> ReadinessFacts:
    """Generate ledger counts whose group states partition held capacity.

    Examples
    --------
    >>> isinstance(_readiness_facts(), st.SearchStrategy)
    True
    """
    capacity = draw(st.integers(min_value=1))
    is_full = draw(st.booleans()) if full_retention is None else full_retention
    held = capacity if is_full else draw(st.integers(min_value=0, max_value=capacity))
    maximum_server_drain = capacity if is_full else min(held, capacity - 1)
    server_drain = (
        capacity
        if is_full
        else draw(st.integers(min_value=0, max_value=maximum_server_drain))
    )
    moving = held - server_drain
    running = draw(st.integers(min_value=0, max_value=moving))
    draining = moving - running
    cached_moving = draw(st.integers(min_value=0, max_value=moving))
    counts = Counts(
        capacity=capacity,
        held=held,
        running=running,
        draining=draining,
        server_drain=server_drain,
        queued=draw(st.integers(min_value=0)),
        cached_moving=cached_moving,
    )
    return ReadinessFacts(
        healthy=draw(st.booleans()),
        closing=draw(st.booleans()),
        counts=counts,
        drain_fault_kind=draw(st.sampled_from(_FAULT_KINDS)),
        recovery_automatic=draw(st.booleans()),
        recovery_phase=draw(st.sampled_from(("idle", *_ACTIVE_PHASES))),
        recovery_attempts=draw(st.integers(min_value=0)),
        recovery_pending=draw(st.booleans()),
    )


@given(facts=_readiness_facts())
def test_unhealthy_and_closing_precede_generated_ledger_states(
    facts: ReadinessFacts,
) -> None:
    """Health and shutdown state dominate every generated ledger combination."""
    assert (
        classify(dc.replace(facts, healthy=False, closing=True)).state == "unhealthy"
    ), "unhealthy supervisor state takes precedence"
    assert classify(dc.replace(facts, healthy=True, closing=True)).state == "closing", (
        "shutdown state takes precedence over ledger state"
    )


@given(facts=_readiness_facts())
def test_generated_results_keep_the_public_state_and_reason_contract(
    facts: ReadinessFacts,
) -> None:
    """Generated states keep readiness explicit and reasons identifier-free."""
    result = classify(facts)
    assert result.state in _STATES, "classification stays within the public states"
    assert result.ready is (result.state in _READY_STATES), (
        "only ready, saturated and draining are usable admission states"
    )
    assert not _IDENTIFIER_LEAK.search(result.reason), (
        "reasons do not expose identifiers or filesystem paths"
    )


@given(facts=_readiness_facts(full_retention=False))
def test_partial_retention_preserves_normal_capacity_readiness(
    facts: ReadinessFacts,
) -> None:
    """Partial server-drain retention is never an operator-blocked state."""
    facts = dc.replace(facts, healthy=True, closing=False)
    result = classify(facts)
    expected = "saturated" if facts.counts.held == facts.counts.capacity else "ready"
    assert result.state == expected, "partial retention keeps capacity state"
    assert result.ready, "partial retention does not require operator action"


@given(facts=_readiness_facts(full_retention=True))
def test_full_retention_without_a_drain_fault_has_accounting_progress(
    facts: ReadinessFacts,
) -> None:
    """An unfailed full drain remains ready while accounting can progress."""
    facts = dc.replace(facts, healthy=True, closing=False, drain_fault_kind=None)
    result = classify(facts)
    assert result.state == "draining", "unfailed full drain has accounting progress"
    assert result.ready, "a progressing full drain remains usable admission"


@given(
    facts=_readiness_facts(full_retention=True),
    fault_kind=st.sampled_from(("stopped_populated", "manager")),
)
def test_non_live_work_faults_block_full_retention(
    facts: ReadinessFacts, fault_kind: str
) -> None:
    """Full retention with non-live-work faults requires operator recovery."""
    facts = dc.replace(facts, healthy=True, closing=False, drain_fault_kind=fault_kind)
    result = classify(facts)
    assert result.state == "blocked", "non-live-work faults require operator action"
    assert not result.ready, "operator recovery prevents admission progress"


@given(
    facts=_readiness_facts(full_retention=True),
    phase=st.sampled_from(_ACTIVE_PHASES),
)
def test_active_recovery_keeps_a_live_work_drain_progressing(
    facts: ReadinessFacts, phase: str
) -> None:
    """A live-work fault with an active recovery phase has a progress path."""
    facts = dc.replace(
        facts,
        healthy=True,
        closing=False,
        drain_fault_kind="live_work",
        recovery_phase=phase,
    )
    result = classify(facts)
    assert result.state == "draining", "active recovery provides drain progress"
    assert result.ready, "active drain recovery keeps admission usable"


@given(
    facts=_readiness_facts(full_retention=True),
    queued=st.integers(min_value=0),
    automatic=st.booleans(),
    attempts=st.integers(min_value=0),
)
def test_idle_live_work_counts_only_real_recovery_paths(
    facts: ReadinessFacts, queued: int, automatic: bool, attempts: int
) -> None:
    """Only demand-triggered automatic recovery is progress while idle."""
    facts = dc.replace(
        facts,
        healthy=True,
        closing=False,
        counts=dc.replace(facts.counts, queued=queued),
        drain_fault_kind="live_work",
        recovery_automatic=automatic,
        recovery_phase="idle",
        recovery_attempts=attempts,
        recovery_pending=False,
    )
    result = classify(facts)
    is_waiting_for_demand = automatic and queued == 0 and attempts == 0
    expected_state = "draining" if is_waiting_for_demand else "blocked"
    assert result.state == expected_state, (
        "idle live-work recovery needs a demand trigger or operator action"
    )
    assert result.ready is is_waiting_for_demand, (
        "only automatic recovery waiting for demand remains ready"
    )
