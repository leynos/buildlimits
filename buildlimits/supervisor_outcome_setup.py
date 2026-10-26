"""Resource-free and construction boundaries for optional outcome recording."""

import contextlib
import dataclasses as dc
import typing as typ

from buildlimits.journal import log

if typ.TYPE_CHECKING:
    from pathlib import Path

    from buildlimits.pending_request import Pending
    from buildlimits.supervisor import Supervisor

OUTCOME_CLOSE_SECONDS = 1.0
_ORIGIN_PREFIXES: tuple[tuple[str, typ.Literal["registry", "git"]], ...] = (
    ("registry:", "registry"),
    ("git:", "git"),
)


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
