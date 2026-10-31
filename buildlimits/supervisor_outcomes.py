"""Isolate cache-outcome recording from supervisor admission decisions."""

import contextlib
import dataclasses as dc
import datetime as dt
import os
import typing as typ

from buildlimits import (
    outcome_key,
    outcome_parser,
    outcome_resources,
    outcome_shell_keys,
    outcome_shell_stats,
    outcome_store_salt,
    outcome_telemetry,
    supervisor_outcome_setup,
)
from buildlimits.journal import log
from buildlimits.nonblocking_child import ChildReaper
from buildlimits.outcome_gate import quiet_point
from buildlimits.outcome_ports import SampleFailure
from buildlimits.outcome_queries import PeerServerProcess, SccacheStatistics
from buildlimits.outcome_recorder_driver import OutcomeRecorder
from buildlimits.outcome_recorder_types import (
    Disabled,
    Ended,
    GateOpen,
    Launched,
    LaunchPayload,
    Querying,
    Sampled,
    SampleFailed,
    Stopping,
)
from buildlimits.outcome_sampling import ChildSampler
from buildlimits.outcome_sightings import request_metadata
from buildlimits.outcome_store import StoreWriter
from buildlimits.supervisor_outcome_setup import (
    OUTCOME_CLOSE_SECONDS,
    GroupEnding,
    NullOutcomeShell,
    build_shell,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_ports import (
        OutcomeStorePort,
        ServerProcessPort,
        StatisticsPort,
        StoreResult,
    )
    from buildlimits.outcome_recorder_types import Outputs
    from buildlimits.pending_request import Pending
    from buildlimits.supervisor import Supervisor

OUTCOME_TICK_SECONDS = 1.0
__all__ = [
    "OUTCOME_CLOSE_SECONDS",
    "OUTCOME_TICK_SECONDS",
    "GroupEnding",
    "NullOutcomeShell",
    "OutcomeDependencies",
    "OutcomeShell",
    "build_shell",
]


@dc.dataclass(slots=True)
class OutcomeDependencies:
    """Optional collaborators used to test the shell's resource boundaries."""

    environment: cabc.Mapping[str, str] | None = None
    writer: OutcomeStorePort | None = None
    statistics: StatisticsPort | None = None
    server: ServerProcessPort | None = None
    reaper: ChildReaper | None = None
    wall_clock: cabc.Callable[[], dt.datetime] | None = None


class OutcomeShell:
    """Record outcomes after admission without owning tokens or compilers."""

    def __init__(
        self,
        supervisor: Supervisor,
        *,
        dependencies: OutcomeDependencies | None = None,
    ) -> None:
        """Create bounded collaborators for the explicitly enabled mode."""
        dependencies = OutcomeDependencies() if dependencies is None else dependencies
        env = (
            os.environ if dependencies.environment is None else dependencies.environment
        )
        self._supervisor = supervisor
        if dependencies.wall_clock is None:
            self._wall_clock = lambda: dt.datetime.now(dt.UTC)
        else:
            self._wall_clock = dependencies.wall_clock
        reaper = ChildReaper() if dependencies.reaper is None else dependencies.reaper
        path = outcome_store_salt.store_path(env)
        writer = (
            StoreWriter(path) if dependencies.writer is None else dependencies.writer
        )
        self._resources = outcome_resources.OutcomeResources(
            reaper, writer, supervisor.clock
        )
        self._server = (
            PeerServerProcess(supervisor.settings.sccache_socket)
            if dependencies.server is None
            else dependencies.server
        )
        self._statistics = (
            SccacheStatistics(
                supervisor.settings.sccache_executable,
                env,
                supervisor.settings.sccache_socket,
                reaper=reaper,
            )
            if dependencies.statistics is None
            else dependencies.statistics
        )
        self._recorder = OutcomeRecorder(self._statistics, self._server)
        self._child_sampler = ChildSampler(self._server)
        self._telemetry = outcome_telemetry.OutcomeTelemetry(
            supervisor.metrics, supervisor.tracer
        )
        self._counters = outcome_shell_stats.OutcomeCounters()
        self._salt: outcome_key.Salt | None = None
        self._next_gate_at = 0.0
        self._disabled = False
        self._closed = False
        self._failure_reported = False

    def launched(self, entry: Pending, group_id: str) -> None:
        """Retain a privacy-safe launch only after its scope was released."""
        self._guard(self._launched, entry, group_id)

    def emptied(self, ending: GroupEnding) -> None:
        """Record the captured ending after admission has processed it."""
        self._guard(self._emptied, ending)

    def on_tick(self, now: float) -> None:
        """Poll owned work, publish results and start due quiet-point reads."""
        if self._closed:
            return
        if self._disabled:
            self._resources.service_failed()
            return
        self._guard(self._on_tick, now)

    def describe(self) -> dict[str, object]:
        """Return bounded live counters without changing recorder state.

        This is a status query, not a loop boundary: the fail-closed
        ``_guard`` wraps only the methods the supervisor loop calls, so a
        snapshot fault must not disable a recorder that is still working.
        A recorder that cannot describe itself reports the fixed unavailable
        state and keeps running; the next snapshot may succeed.

        Returns
        -------
        dict[str, object]
            The privacy-safe recording state for supervisor status, the fixed
            disabled state once recording has failed or the shell has closed, or
            a bounded ``snapshot_unavailable`` marker when the snapshot itself
            fails.
        """
        if self._disabled or self._closed:
            return supervisor_outcome_setup.disabled_description()
        try:
            return self._describe()
        except Exception:  # ruff: ignore[blind-except] - a status read must not disable recording.
            return {"enabled": False, "reason": "snapshot_unavailable"}

    def close(self, timeout: float = OUTCOME_CLOSE_SECONDS) -> None:
        """Cancel the query, reap its helper and close the writer within a bound."""
        if self._closed:
            return
        if self._disabled:
            self._close_resources(timeout)
            return
        self._guard(self._close, timeout)

    def _guard(self, action: cabc.Callable[..., object], *args: object) -> object:
        """Make one loop-called shell boundary total and permanently fail closed.

        Returns
        -------
        object
            The action's result, or ``None`` on failure or once disabled.
        """
        if self._disabled or self._closed:
            return None
        try:
            return action(*args)
        except Exception:  # ruff: ignore[blind-except] - keep recorder faults outside token and admission decisions.
            # The shield keeps even a defective fault path inside this
            # boundary, so D19's totality does not depend on _disable being
            # perfect.
            with contextlib.suppress(Exception):
                self._disable()
            return None

    def _launched(self, entry: Pending, group_id: str) -> None:
        """Build the bounded key payload and enter the recorder window."""
        if entry.cache_path != "cached":
            return
        argv = entry.request.argv
        env = entry.request.env
        metadata = request_metadata(argv)
        key = outcome_shell_keys.cache_key(argv, env, self._salt)
        payload = LaunchPayload(
            key=key,
            origin=supervisor_outcome_setup.origin_kind(
                outcome_key.crate_origin(env, self._salt)
                if self._salt is not None
                else None
            ),
            parser_class=outcome_parser.parser_class(argv),
            metadata=metadata,
        )
        self._publish(
            self._recorder.advance(
                Launched(
                    group_id=group_id,
                    is_cached=True,
                    payload=payload,
                    now=self._supervisor.clock(),
                )
            )
        )

    def _emptied(self, ending: GroupEnding) -> None:
        """Fold an admitted group's completion into the recorder."""
        if not ending.is_cached:
            return
        self._publish(
            self._recorder.advance(
                Ended(
                    ending.group_id,
                    ending.is_clean,
                    self._supervisor.clock(),
                    self._wall_clock(),
                )
            )
        )

    def _on_tick(self, now: float) -> None:
        """Advance one helper, writer handoff, gate and reading summary."""
        self._advance_helper(now)
        self._drain_writer()
        if not isinstance(self._recorder.state, Disabled):
            self._sample_children(now)
            if self._is_gate_due(now):
                self._open_gate(now)
        self._telemetry.flush(now)

    def _advance_helper(self, now: float) -> None:
        """Poll the query helper while it holds a ticket, else the reaper."""
        state = self._recorder.state
        if isinstance(state, Querying) and state.ticket:
            self._publish(self._recorder.poll(now))
        else:
            self._resources.reaper.poll(now)

    def _is_gate_due(self, now: float) -> bool:
        """Report whether a baseline exists and the next gate time arrived.

        Returns
        -------
        bool
            True only once the recorder has a baseline, no launch in the
            attribution window has ended yet, and the later of the two
            scheduled times has passed.
        """
        progress = self._recorder.state.progress
        has_ended_launch = any(
            entry.runtime_seconds is not None for entry in progress.window.entries
        )
        if progress.baseline is not None and not has_ended_launch:
            return False
        return now >= max(progress.next_read_at, self._next_gate_at)

    def _open_gate(self, now: float) -> None:
        """Open a gate when quiet, else record the skipped opportunity."""
        inputs = supervisor_outcome_setup.quiet_inputs(
            self._supervisor, self._recorder.state, now
        )
        reason = quiet_point(inputs)
        self._telemetry.selection(is_quiet=reason is None)
        self._next_gate_at = now + OUTCOME_TICK_SECONDS
        if reason is None:
            self._publish(self._recorder.advance(GateOpen(inputs, now)))
            return
        self._counters.skip_reading()
        self._telemetry.reading_skipped(reason)

    def _sample_children(self, now: float) -> None:
        """Take a due bounded sample while at least one cached group moves."""
        result = self._child_sampler.poll(
            now,
            cached_moving=self._supervisor.admission.counts().cached_moving,
        )
        if result is None:
            return
        if isinstance(result, SampleFailure):
            self._telemetry.child_sample(failed=True)
            self._publish(self._recorder.advance(SampleFailed(result)))
            return
        self._telemetry.child_sample(failed=False)
        self._publish(self._recorder.advance(Sampled(result)))

    def _drain_writer(self) -> None:
        """Consume asynchronous salt and store results without waiting."""
        if self._salt is None:
            salt = self._resources.writer.salt_result()
            if isinstance(salt, outcome_key.Salt):
                self._salt = salt
        for result in self._resources.writer.results():
            self._consume_store_result(result)

    def _consume_store_result(self, result: StoreResult) -> None:
        """Track and publish one aggregated writer result."""
        self._counters.record_store(result)
        self._telemetry.store_result(
            operation=result.operation,
            reason=result.reason,
            records=result.records,
            discarded=result.discarded,
        )

    def _publish(self, outputs: Outputs) -> None:
        """Publish metrics and enqueue immutable records from one transition."""
        self._telemetry.publish(outputs)
        self._counters.record(outputs)
        for record in outputs.records:
            self._resources.writer.append(record)

    def _describe(self) -> dict[str, object]:
        """Build the privacy-safe status payload for record mode.

        Returns
        -------
        dict[str, object]
            Whether recording is still on, plus bounded counters and the last
            closed reason.
        """
        enabled = not isinstance(self._recorder.state, Disabled)
        return {"enabled": enabled, **self._counters.describe()}

    def _disable(self) -> None:
        """Request cancellation once, without waiting, and make calls inert.

        This runs under a fault on the selector thread, so it may only ask
        owned work to stop: D17 bounds a join during supervisor shutdown, not
        during an ordinary launch, tick or status call. Later ticks finish the
        reaping and writer shutdown through ``OutcomeResources.service_failed``.
        """
        if self._disabled:
            return
        self._disabled = True
        if not self._failure_reported:
            self._failure_reported = True
            with contextlib.suppress(Exception):
                log("cache outcome recorder disabled after an internal failure")
            with contextlib.suppress(Exception):
                self._supervisor.metrics.emit(
                    "outcome_reading",
                    outcome_reason="recorder_disabled",
                    readings_failed=1,
                )
        with contextlib.suppress(Exception):
            now = self._supervisor.clock()
            outputs = self._recorder.advance(Stopping(now, self._wall_clock()))
            self._telemetry.publish(outputs)
            self._telemetry.flush(now, force=True)
            for record in outputs.records:
                self._resources.writer.append(record)
        self._resources.service_failed()

    def _close(self, timeout: float) -> None:
        """Flush terminal recorder effects before closing owned resources."""
        now = self._supervisor.clock()
        outputs = self._recorder.advance(Stopping(now, self._wall_clock()))
        self._publish(outputs)
        self._close_resources(timeout)

    def _close_resources(self, timeout: float) -> None:
        """Publish the last reading summary, then release owned resources."""
        with contextlib.suppress(Exception):
            self._telemetry.flush(self._supervisor.clock(), force=True)
        self._resources.close(timeout)
        self._closed = True
