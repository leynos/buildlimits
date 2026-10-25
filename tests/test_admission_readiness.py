"""Finite admission-readiness cases for the pure classifier."""

import dataclasses as dc
import re
import typing as typ

import pytest

from buildlimits.admission_readiness import ReadinessFacts, classify
from buildlimits.admission_state import Counts

_IDENTIFIER_LEAK = re.compile(r"\bg\d+\b|\brequest\s+\d+\b|(?:^|\s)/\S+")

_BASE_COUNTS = Counts(
    capacity=4,
    held=0,
    running=0,
    draining=0,
    server_drain=0,
    queued=0,
    cached_moving=0,
)
_BASE_FACTS = ReadinessFacts(
    healthy=True,
    closing=False,
    counts=_BASE_COUNTS,
    drain_fault_kind=None,
    recovery_automatic=False,
    recovery_phase="idle",
    recovery_attempts=0,
    recovery_pending=False,
)


def _facts(**changes: bool | int | str | None) -> ReadinessFacts:
    """Build one immutable readiness reading with idle healthy defaults.

    Returns
    -------
    ReadinessFacts
        A frozen status reading with the requested overrides.

    Examples
    --------
    >>> _facts(held=1).counts.held
    1
    """
    count_fields = (
        "capacity",
        "held",
        "running",
        "draining",
        "server_drain",
        "queued",
    )
    count_changes = {
        field: changes[field] for field in count_fields if field in changes
    }
    counts = dc.replace(_BASE_COUNTS, **count_changes)
    facts_changes = {
        field: value for field, value in changes.items() if field not in count_fields
    }
    return dc.replace(_BASE_FACTS, counts=counts, **facts_changes)


CASES = [
    pytest.param(
        _facts(),
        "ready",
        True,
        "4 of 4 slots available",
        id="idle",
    ),
    pytest.param(
        _facts(held=4, running=4, queued=1),
        "saturated",
        True,
        "all 4 slots are occupied",
        id="ordinary-saturation",
    ),
    pytest.param(
        _facts(
            held=4,
            server_drain=4,
            queued=1,
            drain_fault_kind="live_work",
            recovery_automatic=True,
            recovery_phase="quiescing",
            recovery_pending=True,
        ),
        "draining",
        True,
        "4 server-drain slots retained; recovery is in quiescing",
        id="automatic-recovery-progressing",
    ),
    pytest.param(
        _facts(
            held=4,
            server_drain=4,
            drain_fault_kind="live_work",
            recovery_automatic=True,
        ),
        "draining",
        True,
        "4 server-drain slots retained; automatic recovery waits for demand",
        id="automatic-idle-without-demand",
    ),
    pytest.param(
        _facts(
            held=4,
            server_drain=4,
            queued=1,
            drain_fault_kind="live_work",
        ),
        "blocked",
        False,
        "4 server-drain slots retained; operator recovery required",
        id="manual-live-work-blocked",
    ),
    pytest.param(
        _facts(
            held=4,
            server_drain=4,
            queued=1,
            drain_fault_kind="stopped_populated",
            recovery_automatic=True,
            recovery_pending=True,
        ),
        "blocked",
        False,
        "4 server-drain slots retained; operator recovery required",
        id="stopped-cache-needs-operator",
    ),
    pytest.param(
        _facts(
            held=4,
            server_drain=4,
            queued=1,
            drain_fault_kind="manager",
            recovery_automatic=True,
            recovery_pending=True,
        ),
        "blocked",
        False,
        "4 server-drain slots retained; operator recovery required",
        id="manager-fault-needs-operator",
    ),
    pytest.param(
        _facts(
            held=4,
            server_drain=4,
            queued=1,
            drain_fault_kind="live_work",
            recovery_automatic=True,
            recovery_phase="stopping",
            recovery_attempts=8,
            recovery_pending=False,
        ),
        "blocked",
        False,
        "4 server-drain slots retained; recovery stopped after 8 attempts",
        id="automatic-attempts-exhausted",
    ),
    pytest.param(
        _facts(
            held=4,
            server_drain=4,
            queued=1,
            drain_fault_kind="live_work",
            recovery_attempts=1,
        ),
        "blocked",
        False,
        "4 server-drain slots retained; recovery stopped after 1 attempt",
        id="one-recovery-attempt",
    ),
    pytest.param(
        _facts(
            healthy=False,
            closing=True,
            held=4,
            server_drain=4,
            queued=1,
            drain_fault_kind="manager",
        ),
        "unhealthy",
        False,
        "supervisor is unhealthy",
        id="unhealthy-precedes-closing-and-blocking",
    ),
    pytest.param(
        _facts(
            closing=True,
            held=4,
            server_drain=4,
            queued=1,
            drain_fault_kind="manager",
        ),
        "closing",
        False,
        "supervisor is closing",
        id="closing-precedes-blocking",
    ),
    pytest.param(
        _facts(held=2, running=1, server_drain=1),
        "ready",
        True,
        "1 server-drain slot retained; 2 of 4 slots available",
        id="partial-retention-with-capacity",
    ),
    pytest.param(
        _facts(
            held=4,
            running=2,
            draining=1,
            server_drain=1,
            queued=1,
            drain_fault_kind="live_work",
        ),
        "saturated",
        True,
        "all 4 slots are occupied; 1 server-drain slot retained",
        id="partial-retention-at-capacity",
    ),
]


@pytest.mark.parametrize(("facts", "state", "ready", "reason"), CASES)
def test_readiness_uses_health_ledger_and_recovery_outlook(
    facts: ReadinessFacts, state: str, ready: bool, reason: str
) -> None:
    """Each state and boundary has a stable readiness result."""
    result = classify(facts)
    assert (result.state, result.ready, result.reason) == (state, ready, reason), (
        "classification follows the specified precedence and counts"
    )
    assert _IDENTIFIER_LEAK.search(result.reason) is None, (
        "reasons do not expose identifiers or paths"
    )


@pytest.mark.parametrize("value", ["/home/x", "g12", "request 7"])
def test_identifier_leak_pattern_detects_all_identifier_forms(value: str) -> None:
    """The count-only reason guard catches paths and request identifiers."""
    assert _IDENTIFIER_LEAK.search(value) is not None, (
        "the pattern detects leaked values"
    )


def test_readiness_inputs_are_frozen() -> None:
    """A readiness decision cannot be changed through mutable input fields."""
    facts = _facts()
    mutable_facts = typ.cast("typ.Any", facts)
    with pytest.raises(dc.FrozenInstanceError):
        mutable_facts.held = 4


def test_cache_reachability_is_not_a_readiness_input() -> None:
    """The classifier has no cache reachability or cache-mode field."""
    names = {field.name for field in dc.fields(ReadinessFacts)}
    assert "caching" not in names, "cache reachability is independent"
    assert "cache_mode" not in names, "direct fallback remains usable"
