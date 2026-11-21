"""Resource-free and construction boundaries for optional outcome recording."""

import contextlib
import dataclasses as dc
import typing as typ

from buildlimits.journal import log
from buildlimits.outcome_gate import QuietInputs
from buildlimits.outcome_record_encode import header_floor_bytes
from buildlimits.outcome_record_format import MAX_COMPACT_BYTES
from buildlimits.outcome_recorder_types import Querying, RecorderState
from buildlimits.outcome_retention import policy_from_settings
from buildlimits.outcome_retention_report import RetentionLimits

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt
    from pathlib import Path

    from buildlimits.nonblocking_child import ChildReaper
    from buildlimits.outcome_ports import (
        OutcomeStorePort,
        ServerProcessPort,
        StatisticsPort,
    )
    from buildlimits.pending_request import Pending
    from buildlimits.supervisor import Supervisor

OUTCOME_CLOSE_SECONDS = 1.0
_ORIGIN_PREFIXES: tuple[tuple[str, typ.Literal["registry", "git"]], ...] = (
    ("registry:", "registry"),
    ("git:", "git"),
)


@dc.dataclass(slots=True)
class OutcomeDependencies:
    """Optional collaborators used to test the shell's resource boundaries."""

    environment: cabc.Mapping[str, str] | None = None
    writer: OutcomeStorePort | None = None
    statistics: StatisticsPort | None = None
    server: ServerProcessPort | None = None
    reaper: ChildReaper | None = None
    wall_clock: cabc.Callable[[], dt.datetime] | None = None


def retention_document(
    supervisor: Supervisor, writer: OutcomeStorePort
) -> dict[str, object]:
    """Describe the effective policy and the writer's active snapshot.

    The limits are derived from the same validated settings the writer uses,
    so a status reader cannot see a different policy from the one the writer
    enforces. Closed history is reported by the archive command, which is the
    component that owns it.

    Returns
    -------
    dict[str, object]
        ``limits`` and ``active`` mappings, both JSON-compatible.
    """
    policy = policy_from_settings(
        supervisor.settings,
        header_floor=header_floor_bytes(),
        segment_limit=MAX_COMPACT_BYTES,
    )
    return {
        "limits": RetentionLimits.of(policy).as_dict(),
        "active": writer.retention().as_dict(),
    }


@dc.dataclass(frozen=True, slots=True)
class GroupEnding:
    """The cache-window facts captured before admission removes a group."""

    group_id: str
    is_cached: bool
    is_clean: bool


def disabled_description() -> dict[str, object]:
    """Return the fixed public status for any disabled recorder.

    Returns
    -------
    dict[str, object]
        A fresh disabled status mapping without exception details.
    """
    return {"enabled": False, "reason": "recorder_disabled"}


class NullOutcomeShell:
    """Resource-free implementation used whenever recording is off."""

    @staticmethod
    def launched(_entry: Pending, _group_id: str, /) -> None:
        """Ignore an admitted group without opening a recording resource."""

    @staticmethod
    def emptied(_ending: GroupEnding, /) -> None:
        """Ignore a group ending while outcome recording is disabled."""

    @staticmethod
    def on_tick(_now: float, /) -> None:
        """Keep the disabled shell inert on every supervisor tick."""

    @staticmethod
    def describe() -> dict[str, object] | None:
        """Omit the optional outcome section while recording is off.

        Returns
        -------
        None
            No status section is exposed while recording is disabled.
        """
        return None

    @staticmethod
    def retention() -> dict[str, object] | None:
        """Omit retention state while no outcome store exists.

        Returns
        -------
        None
            No retention section is exposed while recording is disabled.
        """
        return None

    @staticmethod
    def close(_timeout: float = OUTCOME_CLOSE_SECONDS, /) -> None:
        """Close no resources because the null shell owns none."""


class FailedOutcomeShell(NullOutcomeShell):
    """Expose an inert failed state when enabled startup cannot be assembled."""

    describe = staticmethod(disabled_description)


class OutcomeShellPort(typ.Protocol):
    """Loop-facing calls shared by active and null outcome shells."""

    def launched(self, entry: Pending, group_id: str, /) -> None:
        """Observe a successfully released cached launch."""

    def emptied(self, ending: GroupEnding, /) -> None:
        """Observe a cached group after admission accounts for its ending."""

    def on_tick(self, now: float, /) -> None:
        """Poll bounded recorder work once from the event loop."""

    def describe(self) -> dict[str, object] | None:
        """Return an optional status section."""

    def retention(self) -> dict[str, object] | None:
        """Return the effective retention limits and active history."""

    def close(self, timeout: float = OUTCOME_CLOSE_SECONDS, /) -> None:
        """Release all optional resources within the supplied bound."""


def build_shell(supervisor: Supervisor) -> OutcomeShellPort:
    """Create resource-owning collaborators only for explicit record mode.

    Returns
    -------
    OutcomeShellPort
        An active, null, or fail-closed shell for the configured mode.
    """
    if supervisor.settings.cache_outcomes != "record":
        return NullOutcomeShell()
    try:
        from buildlimits.supervisor_outcomes import OutcomeShell

        return OutcomeShell(supervisor)
    except Exception:  # ruff: ignore[blind-except] - recorder construction must not block supervisor startup.
        with contextlib.suppress(Exception):
            log("cache outcome recorder disabled during initialization")
        with contextlib.suppress(Exception):
            supervisor.metrics.emit(
                "outcome_reading",
                outcome_reason="recorder_disabled",
                readings_failed=1,
            )
        return FailedOutcomeShell()


def exists_or_unknown(path: Path) -> bool:
    """Treat a barrier inspection error as present, preserving the safe gate.

    Returns
    -------
    bool
        Whether the marker exists or could not be inspected safely.
    """
    try:
        path.stat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def quiet_inputs(
    supervisor: Supervisor, state: RecorderState, now: float
) -> QuietInputs:
    """Sample every quiet-gate clause without changing admission state.

    Parameters
    ----------
    supervisor : Supervisor
        The running supervisor whose read-only state supplies the clauses.
    state : RecorderState
        The recorder state, read only to report a query already in flight.
    now : float
        Monotonic time used to describe recovery.

    Returns
    -------
    QuietInputs
        The current values used by the independent quiet-point gate.
    """
    recovery = supervisor.recovery.describe(now)
    return QuietInputs(
        counts=supervisor.admission.counts(),
        is_reachable=supervisor.admission.cached,
        is_cache_allowed=supervisor.server.cache_allowed(),
        is_recovery_idle=recovery["phase"] == "idle",
        has_barrier=exists_or_unknown(supervisor.settings.recovery_marker),
        has_query_in_flight=isinstance(state, Querying),
    )


def origin_kind(value: str | None) -> typ.Literal["registry", "git", "path", "unknown"]:
    """Reduce a private origin label to the closed persisted origin class.

    Returns
    -------
    Literal[registry, git, path, unknown]
        The fixed, privacy-safe origin category.
    """
    if value is None:
        return "unknown"
    for prefix, kind in _ORIGIN_PREFIXES:
        if value.startswith(prefix):
            return kind
    return "path"
