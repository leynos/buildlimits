"""One measured cold and warm compile traced from admission to replay.

The observer check drives the recorder beneath the shell. This check drives the
shell the supervisor builds: admission marks the entry, the shell's launch hook
enters the recorder window, a real sccache compile runs, the group's ending is
folded in, and the reading that follows is written through the store, re-read,
snapshotted and replayed.

It stays a controlled measurement. The server, compiles and statistics are
real; nothing here claims that a live host records such rows until an operator
upgrades and restarts the service.
"""

import contextlib
import datetime as dt
import time
import typing as typ
from pathlib import Path

import pytest
from differential_trace_test_helpers import cached_entry
from outcome_shell_test_support import (
    OutcomeWorld,
    OutcomeWorldOptions,
    admitted_launch,
    make_outcome_world,
)
from sccache_host import (
    _compile_command,
    _private_server,
    _run_compile,
    sccache_host_marks,
)
from sccache_outcome_extraction import decoded_store, snapshot_of
from sccache_outcome_observer_support import Toolchain, toolchain

from buildlimits.effects import Exit
from buildlimits.outcome_queries import PeerServerProcess, SccacheStatistics
from buildlimits.outcome_record_format import OutcomeRecord, StoreRecord
from buildlimits.outcome_store_salt import store_path
from buildlimits.predict.config_loader import load_config
from buildlimits.predict.dataset import Snapshot
from buildlimits.predict.events import label_moment
from buildlimits.predict.harness import walk_forward
from buildlimits.predict.history import released_labels
from buildlimits.predict.timing import audit
from buildlimits.supervisor_outcome_setup import GroupEnding
from buildlimits.supervisor_outcomes import OutcomeShell
from buildlimits.supervisor_placement import mark_admission

pytestmark = [
    *sccache_host_marks(),
    pytest.mark.timeout(180),
]

READ_SECONDS = 30.0
"""How long to drive the shell before calling the reading lost."""


def _tick_until(shell: OutcomeShell, name: str, count: int, what: str) -> None:
    """Advance the shell until one of its counters reaches ``count``.

    The counters are the shell's own statement that a reading happened, which
    is not the same as a row appearing: a baseline reading emits no record, and
    an attributed reading hands its rows to an asynchronous writer.
    """
    deadline = time.monotonic() + READ_SECONDS
    while time.monotonic() < deadline:
        shell.on_tick(time.monotonic())
        if _counter(shell, name) >= count:
            return
        time.sleep(0.02)
    pytest.fail(f"the shell did not {what} within {READ_SECONDS:.0f} s")


def _rows(world: OutcomeWorld) -> tuple[StoreRecord, ...]:
    """Return every record the shell's writer was handed."""
    return tuple(world.writer.records)


def _counter(shell: OutcomeShell, name: str) -> int:
    """Return one of the shell's bounded status counters.

    The counters are the shell's own statement that a reading happened, which
    is not the same as a row appearing: a baseline reading emits no record, and
    an attributed reading emits its rows through an asynchronous writer.
    """
    return typ.cast("int", shell.describe()[name])


def _compile_invocation(
    world: OutcomeWorld, tools: Toolchain, request_id: int, metadata: str
) -> None:
    """Admit, launch, compile and finish one invocation through the shell.

    ``mark_admission`` is the same call the supervisor's launch handler makes
    before placement, so the entry carries the moment the decision was applied
    rather than a time the test chose for it.
    """
    assert isinstance(world.shell, OutcomeShell), "the world built the shell"
    shell = world.shell
    entry = cached_entry()
    launch = admitted_launch(world.admission, request_id)
    mark_admission(typ.cast("typ.Any", world.supervisor), entry)
    shell.launched(entry, launch.group_id)
    result = _run_compile(
        _compile_command(tools.executable, tools.source, tools.output, metadata),
        tools.environment,
    )
    assert result.returncode == 0, result.stderr
    world.admission.leader_exited(launch.group_id, Exit(code=0))
    world.admission.group_emptied(launch.group_id)
    shell.emptied(GroupEnding(launch.group_id, is_cached=True, is_clean=True))
    world.admission.server_activity(active_compiles=0)


def _measured_store(store: Path) -> list[StoreRecord]:
    """Read the store file the shell's own writer produced.

    The shell owns a real ``StoreWriter``, so this reads the bytes its
    asynchronous append path flushed rather than a fake's list of the records
    it was handed. The caller closes the shell first, which bounds that flush.
    """
    return list(decoded_store(store.read_text(encoding="utf-8"), "shell's").records)


def _assert_measured_rows(measured: list[OutcomeRecord]) -> None:
    """Assert the shell's own rows carry identity, timing and provenance."""
    assert [record.outcome for record in measured] == ["miss", "hit"], (
        "the real server misses once and then serves the same key"
    )
    cold, warm = measured
    assert cold.key == warm.key, "both invocations name one cache identity"
    assert cold.attempt != warm.attempt, "each invocation keeps its own attempt"
    for record in measured:
        assert record.supervisor_generation == "observer-generation", (
            "each row names the clock domain the shell minted"
        )
        assert record.generation_digest is not None, (
            "each row names the sccache generation its reading observed"
        )
        assert record.label_time_provenance == "verified", (
            "the shell dates each row at the reading that accepted it"
        )
        assert record.outcome_attribution == "solo", "one launch per window"
        assert record.outcome_reason == "none", "a label has no refusal reason"
        assert record.admitted_at is not None, "admission is recorded, not inferred"
        assert record.completed_at is not None, "completion is recorded"
        assert record.admitted_at <= record.completed_at <= record.observed_at, (
            "decision, completion and label are distinct ordered moments"
        )


def _assert_each_event(measured: list[OutcomeRecord], snapshot: Snapshot) -> None:
    """Assert each stored row survives as the snapshot event it became.

    Field by field, and per invocation: the attempt, the generation that
    stamped it, the generation digest, the completion moment and the decision
    and label moments all have to be the ones the row recorded. A snapshot that
    kept only the row count, or that swapped two invocations' identities, would
    still satisfy a count-based check.
    """
    assert isinstance(snapshot, Snapshot), "the loader returned a snapshot"
    labelled = [event for event in snapshot.events if event.label in {"hit", "miss"}]
    assert len(labelled) == len(measured), "one event per measured row"
    for record, occurred in zip(measured, labelled, strict=True):
        assert occurred.label == record.outcome, "the label survives"
        assert occurred.attempt == record.attempt, "the attempt survives"
        assert occurred.supervisor_generation == record.supervisor_generation, (
            "the clock domain survives"
        )
        assert occurred.generation_digest == record.generation_digest, (
            "the generation digest survives"
        )
        assert occurred.completed_at == record.completed_at, (
            "the completion moment survives"
        )
        assert occurred.admitted_at == record.admitted_at, (
            "the decision moment survives"
        )
        assert label_moment(occurred) == record.observed_at, (
            "the label moment survives as the reading that accepted it"
        )
        assert occurred.label_observed_verified, "and survives as proof"


def _assert_replayed(snapshot: Snapshot) -> None:
    """Assert the snapshot and the replay keep both measured invocations."""
    labelled = [event for event in snapshot.events if event.label in {"hit", "miss"}]
    assert all(
        event.admitted_at is not None
        and event.admitted_at <= typ.cast("dt.datetime", label_moment(event))
        for event in labelled
    ), "each decision precedes the moment its label was accepted"

    verdict = audit(snapshot.events)
    assert verdict.uncorrelated == 0, "both rows name a complete attempt pair"
    assert verdict.conflicts == 0 and verdict.duplicates == 0, (
        "the measured rows neither repeat nor contradict one another"
    )
    walk = walk_forward(snapshot.events, load_config())
    assert sum(len(group) for group in walk.moments) == len(labelled), (
        "the replay answers both measured admissions"
    )
    released = released_labels(snapshot.events)
    assert [moment for moment, _index in released] == [
        label_moment(event) for event in labelled
    ], "each label is released at the reading that accepted it"
    assert snapshot.diagnostics is not None, "the accounting block is present"
    assert snapshot.diagnostics.store.by_server_version, (
        "the snapshot states the sccache version its rows were measured against"
    )


def test_measured_cold_and_warm_compiles_survive_the_shell_path(
    tmp_path: Path,
) -> None:
    """The shell's own path keeps identity, timing and provenance to replay."""
    tools = toolchain(tmp_path)
    socket = tmp_path / "s.sock"
    store = Path(store_path({"HOME": str(tmp_path)}))

    with contextlib.ExitStack() as stack:
        stack.enter_context(
            _private_server(
                tools.executable, tools.environment, tmp_path / "server.log"
            )
        )
        world = _measured_world(tmp_path, tools, socket)
        assert isinstance(world.shell, OutcomeShell), "the world built the shell"
        shell = world.shell
        # Registered before any compile runs, so a failed assertion, a lost
        # reading or an exception still closes the shell and flushes its writer.
        stack.callback(shell.close, 0.1)
        _tick_until(shell, "readings", 1, "take the baseline")
        for request_id, metadata in ((1, "shell-cold"), (2, "shell-cold")):
            _compile_invocation(world, tools, request_id, metadata)
            _tick_until(shell, "outcomes", request_id, "label the invocation")

    records = _measured_store(store)
    measured = [record for record in records if isinstance(record, OutcomeRecord)]
    _assert_measured_rows(measured)

    decoded = decoded_store(store.read_text(encoding="utf-8"), "shell's")
    snapshot = snapshot_of(decoded, tmp_path, "measured")
    _assert_each_event(measured, snapshot)
    _assert_replayed(snapshot)


def _measured_world(tmp_path: Path, tools: Toolchain, socket: Path) -> OutcomeWorld:
    """Build a shell backed by the real sccache ports and a real store writer.

    Returns
    -------
    OutcomeWorld
        The shell, its admission machine and the real writer it builds.
    """
    return make_outcome_world(
        tmp_path,
        OutcomeWorldOptions(
            statistics_port=SccacheStatistics(
                tools.executable, tools.environment, socket
            ),
            server_port=PeerServerProcess(socket),
            supervisor_generation="observer-generation",
            clock=time.monotonic,
            wall_clock=lambda: dt.datetime.now(dt.UTC),
            real_writer=True,
        ),
    )
