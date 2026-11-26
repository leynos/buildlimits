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

import datetime as dt
import time
import typing as typ

import pytest
from differential_trace_test_helpers import cached_entry
from outcome_shell_test_support import (
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
from sccache_outcome_extraction import round_trip
from sccache_outcome_observer_support import Toolchain, toolchain

from buildlimits.effects import Exit
from buildlimits.outcome_queries import PeerServerProcess, SccacheStatistics
from buildlimits.outcome_record_format import OutcomeRecord, StoreRecord
from buildlimits.predict.config_loader import load_config
from buildlimits.predict.events import label_moment
from buildlimits.predict.harness import walk_forward
from buildlimits.predict.timing import audit
from buildlimits.supervisor_outcome_setup import GroupEnding
from buildlimits.supervisor_placement import mark_admission

if typ.TYPE_CHECKING:
    from pathlib import Path

    from outcome_shell_test_support import OutcomeWorld

    from buildlimits.supervisor import Supervisor
    from buildlimits.supervisor_outcomes import OutcomeShell

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
    shell = typ.cast("OutcomeShell", world.shell)
    entry = cached_entry()
    launch = admitted_launch(world.admission, request_id)
    mark_admission(typ.cast("Supervisor", world.supervisor), entry)
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


def _measured_rows(world: OutcomeWorld) -> list[StoreRecord]:
    """Close the shell and return every record it wrote, summary included."""
    shell = typ.cast("OutcomeShell", world.shell)
    shell.close(timeout=0.1)
    assert world.writer.is_closed, "the shell closed its writer"
    return list(_rows(world))


def test_measured_cold_and_warm_compiles_survive_the_shell_path(
    tmp_path: Path,
) -> None:
    """The shell's own path keeps identity, timing and provenance to replay."""
    tools = toolchain(tmp_path)
    socket = tmp_path / "s.sock"

    with _private_server(
        tools.executable, tools.environment, tmp_path / "server.log"
    ) as (_server, _scoped):
        world = make_outcome_world(
            tmp_path,
            OutcomeWorldOptions(
                statistics_port=SccacheStatistics(
                    tools.executable, tools.environment, socket
                ),
                server_port=PeerServerProcess(socket),
                supervisor_generation="observer-generation",
                clock=time.monotonic,
                wall_clock=lambda: dt.datetime.now(dt.UTC),
            ),
        )
        shell = typ.cast("OutcomeShell", world.shell)
        _tick_until(shell, "readings", 1, "take the baseline")
        for request_id, metadata in ((1, "shell-cold"), (2, "shell-cold")):
            _compile_invocation(world, tools, request_id, metadata)
            _tick_until(shell, "outcomes", request_id, "label the invocation")

    records = _measured_rows(world)
    measured = [record for record in records if isinstance(record, OutcomeRecord)]
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

    snapshot = round_trip(records, tmp_path, "test-salt")
    labelled = [event for event in snapshot.events if event.label in {"hit", "miss"}]
    assert [event.label for event in labelled] == ["miss", "hit"], (
        "both measured labels survive the store and the snapshot"
    )
    assert all(event.label_observed_verified for event in labelled), (
        "each label's moment survives as proof"
    )
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
    assert snapshot.diagnostics is not None, "the accounting block is present"
    assert snapshot.diagnostics.store.by_server_version, (
        "the snapshot states the sccache version its rows were measured against"
    )
