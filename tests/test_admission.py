"""Tests for the admission state machine."""

import ast
import collections.abc as cabc
import dataclasses as dc
from pathlib import Path

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
)

import buildlimits.admission as admission_module
from buildlimits.admission import (
    Admission,
    Counts,
    GroupState,
    Launch,
    NotifyFinished,
    NotifyQueued,
    Refuse,
    RequestToken,
    ReturnToken,
    Shutdown,
    Terminate,
)
from buildlimits.effects import Exit


def test_a_request_waits_until_a_token_arrives() -> None:
    """Submitting asks the pool for a token; the token launches the request."""
    admission = Admission()
    assert admission.submit(1) == [
        NotifyQueued(1, 0),
        RequestToken(),
    ], "the first request queues and asks the pool for a token"
    assert admission.submit(2) == [NotifyQueued(2, 1)], "a second request only queues"
    effects = admission.token_arrived(b"a")
    assert effects[0] == Launch(1, "g1", cached=False), "the token launches the leader"
    assert NotifyQueued(2, 0) in effects, "the remaining request moves to the front"
    assert effects[-1] == RequestToken(), "another token is requested for the queue"


def test_a_token_returns_only_when_the_group_is_empty() -> None:
    """A leader's exit terminates leftovers; only emptiness returns the token."""
    admission = Admission()
    admission.submit(1)
    admission.token_arrived(b"a")
    assert admission.leader_exited("g1", Exit(code=3)) == [Terminate("g1")], (
        "the leader's exit terminates any leftovers"
    )
    assert admission.tokens_held() == 1, "the token is held while the group drains"
    assert admission.group_emptied("g1") == [
        ReturnToken(b"a"),
        NotifyFinished(1, Exit(code=3)),
    ], "emptiness returns the token and reports the leader's exit"
    assert admission.tokens_held() == 0, "the token is released once emptied"


def test_a_token_arriving_for_nobody_goes_straight_back() -> None:
    """A token whose request was cancelled meanwhile is returned at once."""
    admission = Admission()
    admission.submit(1)
    admission.cancel(1)
    assert admission.token_arrived(b"a") == [ReturnToken(b"a")], (
        "a token for a cancelled request goes straight back"
    )


def test_cancelling_a_running_group_terminates_it_and_keeps_the_token() -> None:
    """Cancellation holds the token until the killed group has drained."""
    admission = Admission()
    admission.submit(1)
    admission.token_arrived(b"a")
    assert admission.cancel(1) == [Terminate("g1")], (
        "cancelling a running group terminates it"
    )
    admission.leader_exited("g1", Exit(signal=15))
    assert admission.tokens_held() == 1, "the token is kept until the group drains"
    assert ReturnToken(b"a") in admission.group_emptied("g1"), (
        "the token comes back once the killed group has drained"
    )


def test_a_failed_launch_returns_its_token_and_refuses() -> None:
    """A compiler that never started gives its token back immediately."""
    admission = Admission()
    admission.submit(1)
    admission.token_arrived(b"a")
    assert admission.launch_failed("g1") == [
        ReturnToken(b"a"),
        Refuse(1, "launch-failed"),
    ], "a failed launch returns its token and refuses the request"


def test_rustdoc_is_never_cached() -> None:
    """A non-cacheable request runs directly even while caching is on."""
    admission = Admission(cached=True)
    admission.submit(1, cacheable=False)
    admission.submit(2)
    assert admission.token_arrived(b"a")[0] == Launch(1, "g1", cached=False), (
        "rustdoc launches uncached even while caching is on"
    )
    assert admission.token_arrived(b"b")[0] == Launch(2, "g2", cached=True), (
        "a cacheable request still launches cached"
    )


def test_an_uncached_launch_forgets_whether_it_was_cacheable() -> None:
    """Admission keeps nothing for a request once it has launched."""
    admission = Admission(cached=False)
    admission.submit(1)
    assert admission.token_arrived(b"a")[0] == Launch(1, "g1", cached=False), (
        "a request launches uncached while caching is off"
    )
    assert not admission.cacheable, "a launched request leaves no cacheable entry"


def test_a_killed_cached_group_holds_its_token_for_the_server() -> None:
    """A cached group ended by a signal may leave a compilation in the server."""
    admission = Admission(cached=True)
    admission.submit(1)
    admission.token_arrived(b"a")
    admission.leader_exited("g1", Exit(signal=9))
    assert admission.group_emptied("g1") == [NotifyFinished(1, Exit(signal=9))], (
        "a killed cached group reports its leader's exit"
    )
    assert admission.groups["g1"].state is GroupState.SERVER_DRAIN, (
        "the group waits for the server compilation to drain"
    )
    assert not admission.server_activity(active_compiles=1), (
        "the token stays held while the server is still active"
    )
    assert admission.server_activity(active_compiles=0) == [ReturnToken(b"a")], (
        "the token returns once the server has no active compiles"
    )


def test_a_normally_ended_cached_group_returns_its_token_at_once() -> None:
    """A client that exited normally has received the server's reply."""
    admission = Admission(cached=True)
    admission.submit(1)
    admission.token_arrived(b"a")
    admission.leader_exited("g1", Exit(code=0))
    assert ReturnToken(b"a") in admission.group_emptied("g1"), (
        "a normally ended cached group returns its token at once"
    )


def test_health_failure_refuses_the_queue_and_drains_groups() -> None:
    """An unhealthy supervisor refuses waiting work and shuts down once drained."""
    admission = Admission()
    admission.submit(1)
    admission.submit(2)
    admission.token_arrived(b"a")
    effects = admission.health_failed()
    assert Refuse(2, "unhealthy") in effects, "the waiting request is refused"
    assert Terminate("g1") in effects, "the running group is terminated"
    assert Shutdown() not in effects, "shutdown waits for the group to drain"
    assert admission.submit(3) == [Refuse(3, "unhealthy")], (
        "a new submission is refused once unhealthy"
    )
    admission.leader_exited("g1", Exit(signal=15))
    assert Shutdown() in admission.group_emptied("g1"), (
        "shutdown follows once the last group has drained"
    )


@dc.dataclass(slots=True)
class ModelGroup:
    """What the model knows about one admitted group."""

    request: int
    cached: bool
    leader_alive: bool = True
    emptied: bool = False


class AdmissionMachine(RuleBasedStateMachine):
    """Drive admission with arbitrary interleavings and check its invariants.

    The model owns a pool of distinct tokens and a model sccache server. A
    running cached group is assumed to have a compilation in the server; one
    that ends abnormally leaves that compilation running as an orphan until
    the model finishes it. This is the worst case ADR 011 must cover.
    """

    CAPACITY = 3

    def __init__(self) -> None:
        """Create an empty model with a full token pool."""
        super().__init__()
        self.admission = Admission(cached=True)
        self.pool = [bytes([ord("a") + index]) for index in range(self.CAPACITY)]
        self.token_requested = False
        self.next_request = 1
        self.live_requests: set[int] = set()
        self.groups: dict[str, ModelGroup] = {}
        self.orphans = 0
        self.returned_after_empty: set[str] = set()
        self.launched_while_unhealthy = False

    def apply(self, effects: cabc.Sequence[object]) -> None:
        """Mirror effects into the model."""
        for effect in effects:
            match effect:
                case RequestToken():
                    self.token_requested = True
                case ReturnToken(token=token):
                    assert token not in self.pool, "a token was returned twice"
                    self.pool.append(token)
                case Launch(request_id=request_id, group_id=group_id, cached=cached):
                    if not self.admission.healthy:
                        self.launched_while_unhealthy = True
                    self.groups[group_id] = ModelGroup(request_id, cached)
                case (
                    NotifyFinished(request_id=request_id)
                    | Refuse(request_id=request_id)
                ):
                    self.live_requests.discard(request_id)
                case _:
                    pass

    @rule(cacheable=st.booleans())
    def submit(self, *, cacheable: bool) -> None:
        """Submit a new request."""
        request_id = self.next_request
        self.next_request += 1
        self.live_requests.add(request_id)
        self.apply(self.admission.submit(request_id, cacheable=cacheable))

    @precondition(lambda self: self.token_requested and self.pool)
    @rule()
    def deliver_token(self) -> None:
        """Supply a requested token from the pool."""
        self.token_requested = False
        self.apply(self.admission.token_arrived(self.pool.pop(0)))

    @rule(data=st.data(), by_signal=st.booleans())
    def leader_exits(self, data: st.DataObject, *, by_signal: bool) -> None:
        """End a running group's leader, normally or by a signal."""
        alive = sorted(gid for gid, group in self.groups.items() if group.leader_alive)
        if not alive:
            return
        group_id = data.draw(st.sampled_from(alive))
        group = self.groups[group_id]
        group.leader_alive = False
        abnormal = by_signal or self.admission.groups[group_id].cancelled
        if group.cached and abnormal:
            self.orphans += 1
        exit_ = Exit(signal=9) if by_signal else Exit(code=0)
        self.apply(self.admission.leader_exited(group_id, exit_))

    @rule(data=st.data())
    def scope_empties(self, data: st.DataObject) -> None:
        """Empty a drained group's scope."""
        ready = sorted(
            gid
            for gid, group in self.groups.items()
            if not group.leader_alive and not group.emptied
        )
        if not ready:
            return
        group_id = data.draw(st.sampled_from(ready))
        self.groups[group_id].emptied = True
        effects = self.admission.group_emptied(group_id)
        if any(isinstance(effect, ReturnToken) for effect in effects):
            self.returned_after_empty.add(group_id)
        self.apply(effects)

    @rule(data=st.data())
    def requester_disconnects(self, data: st.DataObject) -> None:
        """Disconnect a requester, queued or running."""
        if not self.live_requests:
            return
        request_id = data.draw(st.sampled_from(sorted(self.live_requests)))
        self.apply(self.admission.cancel(request_id))

    @precondition(lambda self: self.orphans > 0)
    @rule()
    def orphan_finishes(self) -> None:
        """Complete an orphaned server compilation."""
        self.orphans -= 1

    @rule()
    def server_polled(self) -> None:
        """Report the server's live compilations."""
        self.apply(self.admission.server_activity(self.server_active()))

    @rule()
    def health_fails(self) -> None:
        """Fail the pool."""
        self.apply(self.admission.health_failed())

    def server_active(self) -> int:
        """Compilations the model server is running."""
        running = sum(
            1 for group in self.groups.values() if group.cached and group.leader_alive
        )
        return running + self.orphans

    @invariant()
    def tokens_are_conserved(self) -> None:
        """Every token is in the pool or held by exactly one group."""
        held = [group.token for group in self.admission.groups.values()]
        assert len(set(held)) == len(held), "no token is held by two groups"
        assert not set(held) & set(self.pool), "a held token is not also in the pool"
        assert len(held) + len(self.pool) == self.CAPACITY, (
            "every token is either held or in the pool"
        )

    @invariant()
    def queued_requests_hold_nothing(self) -> None:
        """No waiting request has a group."""
        queued = set(self.admission.queue)
        holders = {group.request_id for group in self.admission.groups.values()}
        assert not queued & holders, "a waiting request never holds a group"
        assert set(self.admission.cacheable) == queued, (
            "only waiting requests have a cacheable entry"
        )

    @invariant()
    def tokens_return_only_after_emptiness(self) -> None:
        """Check that a token comes back only after its scope empties."""
        for group_id, group in self.groups.items():
            if group_id not in self.admission.groups:
                assert group.emptied or group_id not in self.returned_after_empty, (
                    "a token returns only after its scope has emptied"
                )

    @invariant()
    def server_work_is_covered_by_tokens(self) -> None:
        """Server compilations never exceed tokens held by cached groups."""
        assert self.server_active() <= self.admission.cached_tokens_held(), (
            "server compilations never outnumber tokens held by cached groups"
        )

    @invariant()
    def nothing_launches_while_unhealthy(self) -> None:
        """Check that an unhealthy supervisor never launches."""
        assert not self.launched_while_unhealthy, (
            "an unhealthy supervisor never launches"
        )


TestAdmissionMachine = AdmissionMachine.TestCase
TestAdmissionMachine.settings = settings(
    max_examples=300, stateful_step_count=60, deadline=None
)


_WIRE = frozenset({"protocol", "replies", "framing", "wire"})


@pytest.mark.parametrize(
    ("name", "forbidden"),
    [
        ("admission.py", _WIRE | {"requesters"}),
        ("effects.py", _WIRE | {"requesters"}),
        ("attributes.py", _WIRE | {"requesters"}),
        # The shell's placement code tracks requests but speaks no protocol.
        ("groups.py", _WIRE),
    ],
)
def test_domain_modules_import_no_transport(
    name: str, forbidden: frozenset[str]
) -> None:
    """Domain policy depends on no wire, framing, reply or request module."""
    source_dir = Path(admission_module.__file__).parent
    tree = ast.parse((source_dir / name).read_text(encoding="utf-8"))
    imported = {
        node.module.removeprefix("buildlimits.")
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not imported & forbidden, f"{name} imports {imported & forbidden}"


def test_counts_cover_all_group_states() -> None:
    """Held includes running, scope-draining and server-drain groups."""
    admission = Admission(cached=True, capacity=4)
    for request_id in range(3):
        admission.submit(request_id)
        admission.token_arrived(b"a")
    admission.groups["g2"].state = GroupState.DRAINING
    admission.groups["g3"].state = GroupState.SERVER_DRAIN
    admission.submit(4)
    counts = admission.counts()
    assert counts == Counts(1, 3, 1, 1, 1, 4), "gauges measure each lifecycle state"
