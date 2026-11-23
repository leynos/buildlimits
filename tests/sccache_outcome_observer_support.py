"""Helpers for the opt-in real-sccache outcome observer check.

The check drives the recorder through the real statistics and generation ports.
Keeping the port wiring, the controlled compile steps and the extraction
assertions here leaves the test module to state the sequence and its expected
labels.
"""

import contextlib
import dataclasses as dc
import datetime as dt
import hashlib
import shutil
import subprocess
import time
import typing as typ
from pathlib import Path

import pytest
from sccache_host import _base_environment, _compile_command, _run_compile

from buildlimits.admission_state import Counts
from buildlimits.outcome_gate import QuietInputs
from buildlimits.outcome_key import CacheKey, Salt, toolchain_identity
from buildlimits.outcome_queries import PeerServerProcess
from buildlimits.outcome_record_format import (
    DecodedStore,
    OutcomeRecord,
    StoreHeader,
    WindowSummary,
)
from buildlimits.outcome_recorder_types import (
    Ended,
    GateOpen,
    Launched,
    LaunchPayload,
    Outputs,
    Querying,
)
from buildlimits.predict.dataset import build_snapshot
from buildlimits.predict.records import decode_history

if typ.TYPE_CHECKING:
    from buildlimits.outcome_attribution import Generation
    from buildlimits.outcome_recorder_driver import OutcomeRecorder

SALT = Salt(b"o" * 32, "observer-test")
READ_TIMEOUT_SECONDS = 10.0


@dc.dataclass(frozen=True, slots=True)
class Toolchain:
    """The private binary, environment, source and output of one run."""

    executable: str
    environment: dict[str, str]
    source: Path
    output: Path


class Reading(typ.NamedTuple):
    """One completed reading and every effect it produced."""

    events: tuple[object, ...]
    records: tuple[object, ...]
    reason: str


class SwitchableServer(PeerServerProcess):
    """A generation port whose answer can be changed during a query."""

    def __init__(self, socket_path: Path) -> None:
        """Retain the current generation after the real port resolves it."""
        super().__init__(socket_path)
        self.current: object = super().generation()

    def generation(self) -> object:
        """Return the generation the next boundary will observe."""
        return self.current


def inputs() -> QuietInputs:
    """Return a quiet-point snapshot with an established baseline."""
    return QuietInputs(
        counts=Counts(0, 0, 0, 0, 0, 4, 0),
        is_reachable=True,
        is_cache_allowed=True,
        is_recovery_idle=True,
        has_barrier=False,
        has_query_in_flight=False,
        has_baseline=True,
    )


def key(tools: Toolchain, metadata: str) -> CacheKey:
    """Build one privacy-safe key for a controlled compilation."""
    return CacheKey(
        crate_name="fixture",
        crate_origin="registry:fixture@0.0.1",
        target="host",
        argument_digest=hashlib.sha256(metadata.encode()).hexdigest(),
        toolchain_identity=toolchain_identity(
            tools.executable, tools.environment, "0.14.0", SALT
        ),
    )


def payload(
    tools: Toolchain, metadata: str, *, parser_class: str = "cacheable"
) -> LaunchPayload:
    """Return the bounded launch metadata for one request."""
    return LaunchPayload(
        key=key(tools, metadata),
        origin="registry",
        parser_class=typ.cast("typ.Any", parser_class),
    )


def _collect(outputs: Outputs, events: list[object], records: list[object]) -> str:
    """Fold one output into the collected effects and return its reason."""
    events.extend(outputs.events)
    records.extend(outputs.records)
    return "" if not outputs.reading_reasons else outputs.reading_reasons[-1]


def poll(recorder: OutcomeRecorder) -> Reading:
    """Poll an already-started query to its terminal result."""
    events: list[object] = []
    records: list[object] = []
    deadline = time.monotonic() + READ_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        reason = _collect(recorder.poll(time.monotonic()), events, records)
        if reason:
            return Reading(tuple(events), tuple(records), reason)
        time.sleep(0.01)
    raise TimeoutError("the statistics query did not reach a terminal result")


def start(recorder: OutcomeRecorder) -> None:
    """Wait for the read cadence, then start one statistics query."""
    now = max(time.monotonic(), recorder.state.progress.next_read_at)
    if now > time.monotonic():
        time.sleep(now - time.monotonic())
    recorder.advance(GateOpen(inputs(), now))
    if not isinstance(recorder.state, Querying):
        pytest.fail("the quiet gate did not open a statistics query")


def read(recorder: OutcomeRecorder) -> Reading:
    """Start one statistics query and poll it to a terminal result."""
    start(recorder)
    return poll(recorder)


def launch(recorder: OutcomeRecorder, group_id: str, item: LaunchPayload) -> None:
    """Record one admitted launch at the current monotonic and wall times."""
    recorder.advance(
        Launched(
            group_id=group_id,
            is_cached=True,
            payload=item,
            now=time.monotonic(),
            admitted_at=dt.datetime.now(dt.UTC),
        )
    )


def finish(recorder: OutcomeRecorder, group_id: str) -> None:
    """Record one clean ending after its compilation returned."""
    recorder.advance(
        Ended(
            group_id=group_id,
            is_clean=True,
            now=time.monotonic(),
            observed_at=dt.datetime.now(dt.UTC),
        )
    )


def outcomes(records: tuple[object, ...]) -> tuple[OutcomeRecord, ...]:
    """Return the outcome rows from a reading, excluding its summary."""
    return tuple(record for record in records if isinstance(record, OutcomeRecord))


def summaries(records: tuple[object, ...]) -> tuple[WindowSummary, ...]:
    """Return the window summaries from a reading."""
    return tuple(record for record in records if isinstance(record, WindowSummary))


def toolchain(tmp_path: Path) -> Toolchain:
    """Build the private sccache binary, environment, source and output path."""
    executable = shutil.which("sccache")
    assert executable is not None, "sccache_host_marks must check availability"
    toolchain_bin = tmp_path / "toolchain" / "bin"
    toolchain_bin.mkdir(parents=True)
    fake_compiler = toolchain_bin / "rustc"
    shutil.copy2(Path(__file__).with_name("fake_rustc.py"), fake_compiler)
    fake_compiler.chmod(0o755)
    source = tmp_path / "fixture.rs"
    source.write_text("pub fn fixture() -> usize { 42 }\n", encoding="utf-8")
    environment = _base_environment(tmp_path, toolchain_bin)
    return Toolchain(
        executable, environment, source, tmp_path / "out" / "libfixture.rlib"
    )


def single_compile(
    recorder: OutcomeRecorder,
    tools: Toolchain,
    *,
    metadata: str,
    group_id: str,
) -> Reading:
    """Run one launch, compile and clean ending, then close its reading."""
    launch(recorder, group_id, payload(tools, metadata))
    result = _run_compile(
        _compile_command(tools.executable, tools.source, tools.output, metadata),
        tools.environment,
    )
    assert result.returncode == 0, result.stderr
    finish(recorder, group_id)
    return read(recorder)


def assert_cold_miss(reading: Reading) -> OutcomeRecord:
    """Assert the real cold compile produced one solo miss with admission."""
    (record,) = outcomes(reading.records)
    assert record.outcome == "miss", "the real delta is a miss"
    assert record.outcome_reason == "none", "a label has no refusal reason"
    assert record.outcome_attribution == "solo", "one clean launch"
    assert record.admitted_at is not None, "admission time was captured"
    assert record.admitted_at <= record.observed_at, (
        "the decision moment precedes the label"
    )
    assert summaries(reading.records), "the window summary is persisted"
    return record


def assert_warm_hit(reading: Reading, expected: CacheKey) -> None:
    """Assert the real warm compile produced one hit for the same identity."""
    (record,) = outcomes(reading.records)
    assert record.outcome == "hit", "the real delta is a hit"
    assert record.key == expected, "the same identity is reused"
    assert record.admitted_at is not None, "the hit carries admission"


def assert_mixed(reading: Reading) -> None:
    """Assert two concurrent results stay unknown rather than being split."""
    records = outcomes(reading.records)
    assert len(records) == 2, "both concurrent launches are retained"
    for record in records:
        assert record.outcome == "unattributed", "a mixed window is not split"
        assert record.outcome_reason == "mixed_window", "the reason is explicit"
        assert record.outcome_attribution == "none", "no attribution is invented"


def run_mixed(recorder: OutcomeRecorder, tools: Toolchain) -> Reading:
    """Run a cold miss and a warm hit concurrently, then close their window."""
    jobs = (
        ("mixed-cold", "observer-mixed"),
        ("mixed-warm", "observer-cold"),
    )
    processes = []
    with contextlib.ExitStack() as stack:
        for group_id, metadata in jobs:
            launch(recorder, group_id, payload(tools, metadata))
            process = stack.enter_context(
                subprocess.Popen(
                    _compile_command(
                        tools.executable, tools.source, tools.output, metadata
                    ),
                    env=tools.environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
            processes.append((group_id, process))
        for group_id, process in processes:
            _stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, stderr
            finish(recorder, group_id)
    return read(recorder)


def run_declined(recorder: OutcomeRecorder, tools: Toolchain) -> Reading:
    """Run one request the real server declines without compiling."""
    launch(
        recorder,
        "declined",
        payload(tools, "observer-probe", parser_class="declined"),
    )
    result = subprocess.run(
        [tools.executable, "rustc", "-vV"],
        check=False,
        capture_output=True,
        text=True,
        env=tools.environment,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    finish(recorder, "declined")
    return read(recorder)


def run_timeout(recorder: OutcomeRecorder, tools: Toolchain) -> Outputs:
    """Run one launch whose statistics query reaches its deadline."""
    launch(recorder, "timeout", payload(tools, "observer-timeout"))
    finish(recorder, "timeout")
    start(recorder)
    ticket = typ.cast("typ.Any", recorder.state).ticket
    return recorder.poll(ticket.deadline + 1.0)


def run_restart(
    recorder: OutcomeRecorder, server: SwitchableServer, tools: Toolchain
) -> Reading:
    """Run one launch whose query spans a server generation change."""
    launch(recorder, "restart", payload(tools, "observer-restart"))
    finish(recorder, "restart")
    start(recorder)
    current = typ.cast("Generation", server.current)
    server.current = dc.replace(
        current, pid=current.pid + 1, starttime=current.starttime + 1
    )
    return poll(recorder)


def assert_extraction(records: list[object]) -> None:
    """Prove the retained rows encode, decode and extract without loss."""
    header = StoreHeader(SALT.salt_id, dt.datetime.now(dt.UTC))
    snapshot = build_snapshot(
        decode_history(DecodedStore(header, tuple(records))), now=header.written_at
    )
    stats = snapshot.stats()

    assert stats.labelled == 2, "the controlled miss and hit are the labels"
    assert stats.admission_recorded == stats.events, (
        "every retained outcome carries its admission time"
    )
    assert stats.admission_usable == stats.events, (
        "each admission precedes its own label"
    )
    assert snapshot.diagnostics is not None, "the accounting block is present"
    assert snapshot.diagnostics.store.by_result["hit"] == 1, "one real hit"
    assert snapshot.diagnostics.store.by_result["miss"] == 1, "one real miss"
    assert snapshot.diagnostics.store.by_reason["mixed_window"] == 2, (
        "both mixed rows name their refusal"
    )
    assert snapshot.diagnostics.store.by_reason["timeout"] == 1, "the timeout is named"
