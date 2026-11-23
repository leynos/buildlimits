"""Controlled outcome observation against the real sccache 0.14 backend.

The unit recorder contracts use fake ports. This opt-in check drives the same
reducer through the real statistics and generation ports, so the counter
footprints, labels and admission times are evidence from the supported binary
rather than from a fixture's model of it.
"""

import dataclasses as dc
import datetime as dt
import hashlib
import shutil
import subprocess
import time
import typing as typ
from pathlib import Path

import pytest
from sccache_host import (
    _base_environment,
    _compile_command,
    _private_server,
    _run_compile,
    sccache_host_marks,
)

from buildlimits.admission_state import Counts
from buildlimits.outcome_gate import QuietInputs
from buildlimits.outcome_key import CacheKey, Salt, toolchain_identity
from buildlimits.outcome_queries import PeerServerProcess, SccacheStatistics
from buildlimits.outcome_record_format import (
    DecodedStore,
    OutcomeRecord,
    StoreHeader,
    WindowSummary,
)
from buildlimits.outcome_recorder_driver import OutcomeRecorder
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

pytestmark = [
    *sccache_host_marks(),
    pytest.mark.timeout(180),
]

_SALT = Salt(b"o" * 32, "observer-test")
_READ_TIMEOUT_SECONDS = 10.0

type _Toolchain = tuple[str, dict[str, str], Path, Path]


class _Reading(typ.NamedTuple):
    """One completed reading and every effect it produced."""

    events: tuple[object, ...]
    records: tuple[object, ...]
    reason: str


class _SwitchableServer:  # pylint: disable=trivial-attribute-wrapper  # a test double must satisfy the port while overriding one answer.
    """Wrap the real generation port so a restart can be injected mid-query."""

    def __init__(self, real: PeerServerProcess) -> None:
        """Retain the real port and its current generation."""
        self.real = real
        self.current = real.generation()

    def generation(self) -> object:
        """Return the generation the next boundary will observe."""
        return self.current

    def full_emit_metadata(self) -> object:
        """Delegate child sampling to the real process port."""
        return self.real.full_emit_metadata()


def _inputs() -> QuietInputs:
    """Return a quiet-point snapshot with no cached group moving."""
    return QuietInputs(
        counts=Counts(0, 0, 0, 0, 0, 4, 0),
        is_reachable=True,
        is_cache_allowed=True,
        is_recovery_idle=True,
        has_barrier=False,
        has_query_in_flight=False,
        has_baseline=True,
    )


def _key(executable: str, environment: dict[str, str], metadata: str) -> CacheKey:
    """Build one privacy-safe key for a controlled compilation."""
    return CacheKey(
        crate_name="fixture",
        crate_origin="registry:fixture@0.0.1",
        target="host",
        argument_digest=hashlib.sha256(metadata.encode()).hexdigest(),
        toolchain_identity=toolchain_identity(executable, environment, "0.14.0", _SALT),
    )


def _payload(
    executable: str,
    environment: dict[str, str],
    metadata: str,
    *,
    parser_class: str = "cacheable",
) -> LaunchPayload:
    """Return the bounded launch metadata for one request."""
    return LaunchPayload(
        key=_key(executable, environment, metadata),
        origin="registry",
        parser_class=typ.cast("typ.Any", parser_class),
    )


def _collect(outputs: Outputs, events: list[object], records: list[object]) -> str:
    """Fold one output into the collected effects and return its reason."""
    events.extend(outputs.events)
    records.extend(outputs.records)
    return "" if not outputs.reading_reasons else outputs.reading_reasons[-1]


def _poll(recorder: OutcomeRecorder) -> _Reading:
    """Poll an already-started query to its terminal result."""
    events: list[object] = []
    records: list[object] = []
    deadline = time.monotonic() + _READ_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        reason = _collect(recorder.poll(time.monotonic()), events, records)
        if reason:
            return _Reading(tuple(events), tuple(records), reason)
        time.sleep(0.01)
    pytest.fail("the statistics query did not reach a terminal result")


def _start(recorder: OutcomeRecorder) -> None:
    """Wait for the read cadence, then start one statistics query."""
    now = max(time.monotonic(), recorder.state.progress.next_read_at)
    if now > time.monotonic():
        time.sleep(now - time.monotonic())
    recorder.advance(GateOpen(_inputs(), now))
    if not isinstance(recorder.state, Querying):
        pytest.fail("the quiet gate did not open a statistics query")


def _read(recorder: OutcomeRecorder) -> _Reading:
    """Start one statistics query and poll it to a terminal result."""
    _start(recorder)
    return _poll(recorder)


def _launch(recorder: OutcomeRecorder, group_id: str, payload: LaunchPayload) -> None:
    """Record one admitted launch at the current monotonic and wall times."""
    recorder.advance(
        Launched(
            group_id=group_id,
            is_cached=True,
            payload=payload,
            now=time.monotonic(),
            admitted_at=dt.datetime.now(dt.UTC),
        )
    )


def _finish(recorder: OutcomeRecorder, group_id: str) -> None:
    """Record one clean ending after its compilation returned."""
    recorder.advance(
        Ended(
            group_id=group_id,
            is_clean=True,
            now=time.monotonic(),
            observed_at=dt.datetime.now(dt.UTC),
        )
    )


def _outcomes(records: tuple[object, ...]) -> tuple[OutcomeRecord, ...]:
    """Return the outcome rows from a reading, excluding its summary."""
    return tuple(record for record in records if isinstance(record, OutcomeRecord))


def _summaries(records: tuple[object, ...]) -> tuple[WindowSummary, ...]:
    """Return the window summaries from a reading."""
    return tuple(record for record in records if isinstance(record, WindowSummary))


def _toolchain(tmp_path: Path) -> _Toolchain:
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
    return executable, environment, source, tmp_path / "out" / "libfixture.rlib"


def _single_compile(
    recorder: OutcomeRecorder,
    executable: str,
    environment: dict[str, str],
    source: Path,
    output: Path,
    *,
    metadata: str,
    group_id: str,
) -> _Reading:
    """Run one launch, compile and clean ending, then close its reading."""
    _launch(
        recorder,
        group_id,
        _payload(executable, environment, metadata),
    )
    result = _run_compile(
        _compile_command(executable, source, output, metadata), environment
    )
    assert result.returncode == 0, result.stderr
    _finish(recorder, group_id)
    return _read(recorder)


def _assert_cold_miss(reading: _Reading) -> OutcomeRecord:
    """Assert the real cold compile produced one solo miss with admission."""
    (record,) = _outcomes(reading.records)
    assert record.outcome == "miss", "the real delta is a miss"
    assert record.outcome_reason == "none", "a label has no refusal reason"
    assert record.outcome_attribution == "solo", "one clean launch"
    assert record.admitted_at is not None, "admission time was captured"
    assert record.admitted_at <= record.observed_at, (
        "the decision moment precedes the label"
    )
    assert _summaries(reading.records), "the window summary is persisted"
    return record


def _assert_warm_hit(reading: _Reading, key: CacheKey) -> None:
    """Assert the real warm compile produced one hit for the same identity."""
    (record,) = _outcomes(reading.records)
    assert record.outcome == "hit", "the real delta is a hit"
    assert record.key == key, "the same identity is reused"
    assert record.admitted_at is not None, "the hit carries admission"


def _assert_mixed(reading: _Reading) -> None:
    """Assert two concurrent results stay unknown rather than being split."""
    records = _outcomes(reading.records)
    assert len(records) == 2, "both concurrent launches are retained"
    for record in records:
        assert record.outcome == "unattributed", "a mixed window is not split"
        assert record.outcome_reason == "mixed_window", "the reason is explicit"
        assert record.outcome_attribution == "none", "no attribution is invented"


def _run_mixed(
    recorder: OutcomeRecorder,
    executable: str,
    environment: dict[str, str],
    source: Path,
    output: Path,
) -> _Reading:
    """Run a cold miss and a warm hit concurrently, then close their window."""
    jobs = (
        ("mixed-cold", "observer-mixed"),
        ("mixed-warm", "observer-cold"),
    )
    processes = []
    for group_id, metadata in jobs:
        _launch(recorder, group_id, _payload(executable, environment, metadata))
        process = subprocess.Popen(
            _compile_command(executable, source, output, metadata),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        processes.append((group_id, process))
    for group_id, process in processes:
        _stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        _finish(recorder, group_id)
    return _read(recorder)


def _run_declined(
    recorder: OutcomeRecorder,
    executable: str,
    environment: dict[str, str],
) -> _Reading:
    """Run one request the real server declines without compiling."""
    _launch(
        recorder,
        "declined",
        _payload(executable, environment, "observer-probe", parser_class="declined"),
    )
    result = subprocess.run(
        [executable, "rustc", "-vV"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    _finish(recorder, "declined")
    return _read(recorder)


def _run_timeout(
    recorder: OutcomeRecorder, executable: str, environment: dict[str, str]
) -> Outputs:
    """Run one launch whose statistics query reaches its deadline."""
    _launch(recorder, "timeout", _payload(executable, environment, "observer-timeout"))
    _finish(recorder, "timeout")
    _start(recorder)
    ticket = typ.cast("typ.Any", recorder.state).ticket
    return recorder.poll(ticket.deadline + 1.0)


def _run_restart(
    recorder: OutcomeRecorder,
    server: _SwitchableServer,
    executable: str,
    environment: dict[str, str],
) -> _Reading:
    """Run one launch whose query spans a server generation change."""
    _launch(recorder, "restart", _payload(executable, environment, "observer-restart"))
    _finish(recorder, "restart")
    _start(recorder)
    current = typ.cast("Generation", server.current)
    server.current = dc.replace(
        current, pid=current.pid + 1, starttime=current.starttime + 1
    )
    return _poll(recorder)


def test_the_real_sccache_observer_labels_controlled_windows(tmp_path: Path) -> None:
    """Miss, hit, mixed, declined, timeout and restart are told apart exactly."""
    executable, environment, source, output = _toolchain(tmp_path)
    records: list[object] = []

    with _private_server(executable, environment, tmp_path / "server.log") as (
        _server,
        _scoped,
    ):
        recorder = OutcomeRecorder(
            SccacheStatistics(executable, environment, tmp_path / "s.sock"),
            _SwitchableServer(PeerServerProcess(tmp_path / "s.sock")),
        )
        baseline = _read(recorder)
        assert baseline.reason == "none", "the fresh generation is the baseline"
        assert baseline.events == (), "an empty baseline emits no label"

        missed = _single_compile(
            recorder,
            executable,
            environment,
            source,
            output,
            metadata="observer-cold",
            group_id="cold",
        )
        miss_record = _assert_cold_miss(missed)
        records.extend(missed.records)

        hit = _single_compile(
            recorder,
            executable,
            environment,
            source,
            output,
            metadata="observer-cold",
            group_id="warm",
        )
        _assert_warm_hit(hit, miss_record.key)
        records.extend(hit.records)

        mixed = _run_mixed(recorder, executable, environment, source, output)
        _assert_mixed(mixed)
        records.extend(mixed.records)

        refused = _run_declined(recorder, executable, environment)
        (declined_record,) = _outcomes(refused.records)
        assert declined_record.outcome == "declined", "the server declined the probe"
        assert declined_record.outcome_attribution == "solo", "one clean launch"
        records.extend(refused.records)

        timed_out = _run_timeout(recorder, executable, environment)
        assert timed_out.reading_reasons == ("timeout",), "the deadline is enforced"
        (timeout_record,) = _outcomes(timed_out.records)
        assert timeout_record.outcome == "unattributed", "no label on a timeout"
        assert timeout_record.outcome_reason == "timeout", "the reason is explicit"
        records.extend(timed_out.records)

        recovered = _read(recorder)
        assert recovered.reason == "none", "the timeout leaves a fresh baseline"

    _assert_extraction(records)


def test_the_real_sccache_observer_refuses_a_restart_window(tmp_path: Path) -> None:
    """A generation change inside a query leaves its launch unlabelled."""
    executable, environment, _source, _output = _toolchain(tmp_path)

    with _private_server(executable, environment, tmp_path / "server.log") as (
        _server,
        _scoped,
    ):
        server = _SwitchableServer(PeerServerProcess(tmp_path / "s.sock"))
        recorder = OutcomeRecorder(
            SccacheStatistics(executable, environment, tmp_path / "s.sock"), server
        )
        assert _read(recorder).reason == "none", "the baseline is accepted"
        restarted = _run_restart(recorder, server, executable, environment)

    assert restarted.reason == "generation_changed", (
        "a server restart inside the query is refused"
    )
    (record,) = _outcomes(restarted.records)
    assert record.outcome == "unattributed", "no label across a restart"
    assert record.outcome_reason == "generation_changed", (
        "the restart reason is explicit"
    )
    assert record.admitted_at is not None, "the refused row still carries admission"


def _assert_extraction(records: list[object]) -> None:
    """Prove the retained rows encode, decode and extract without loss."""
    header = StoreHeader(_SALT.salt_id, dt.datetime.now(dt.UTC))
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
