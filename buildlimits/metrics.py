"""Validate identifier-free compile-admission records at the output boundary.

The package owns this vocabulary; it shares no runtime dependency with
rust-cleanup. Telemetry failures must never interfere with token accounting.
"""

import contextlib
import math
import typing as typ

from buildlimits.metrics_schema import (
    BOOLEANS as BOOLEANS,
)
from buildlimits.metrics_schema import (
    COUNTERS as COUNTERS,
)
from buildlimits.metrics_schema import (
    DURATIONS as DURATIONS,
)
from buildlimits.metrics_schema import (
    EVENTS as EVENTS,
)
from buildlimits.metrics_schema import (
    FIELD_KINDS as FIELD_KINDS,
)
from buildlimits.metrics_schema import (
    MAX_COUNTER as MAX_COUNTER,
)
from buildlimits.metrics_schema import (
    OUTCOMES as OUTCOMES,
)
from buildlimits.metrics_schema import (
    REASONS as REASONS,
)
from buildlimits.metrics_schema import (
    VOCABULARIES as VOCABULARIES,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.admission_state import Counts
    from buildlimits.effects import Exit


class MetricsSink(typ.Protocol):
    """Transport for a validated native journal record."""

    def emit(self, record: cabc.Mapping[str, str], /) -> None:
        """Send one bounded record.

        Parameters
        ----------
        record : Mapping[str, str]
            ASCII native journal fields with a fixed vocabulary.
        """


class MetricsPort(typ.Protocol):
    """Failure-isolated domain port for metric events."""

    def emit(self, event: str, **fields: object) -> None:
        """Record one event without affecting admission.

        Parameters
        ----------
        event : str
            A member of the event catalogue.
        **fields : object
            Scalar measurements whose names fix their kinds.
        """


class NullSink:
    """Discard metrics when no transport is available."""

    @staticmethod
    def emit(_record: cabc.Mapping[str, str], /) -> None:
        """Discard a record.

        Parameters
        ----------
        _record : Mapping[str, str]
            The already validated record, intentionally discarded.
        """


def refusal_reason(reason: str) -> str:
    """Map a refusal to the fixed vocabulary without retaining raw text.

    Parameters
    ----------
    reason : str
        The protocol's refusal reason.

    Returns
    -------
    str
        A known token, or ``other``.
    """
    token = reason.replace("-", "_")
    return token if token in REASONS else "other"


def exit_outcome(exit_: Exit) -> str:
    """Classify an exit without recording an exit code or signal number.

    Parameters
    ----------
    exit_ : Exit
        The compiler or supervisor's ending.

    Returns
    -------
    str
        ``completed``, ``failed``, ``signalled`` or ``unknown``.
    """
    if exit_.signal is not None:
        return "signalled"
    if exit_.code is None:
        return "unknown"
    return "completed" if exit_.code == 0 else "failed"


def bounded_field(name: str, value: object) -> str | None:
    """Validate a scalar against its field's kind and cap numeric values.

    Parameters
    ----------
    name : str
        An unprefixed, lower-case schema field.
    value : object
        The proposed scalar; arbitrary text and structured values are refused.

    Returns
    -------
    str | None
        The native text value, or ``None`` for an invalid field.
    """
    match FIELD_KINDS.get(name):
        case "token":
            return (
                value
                if isinstance(value, str) and value in VOCABULARIES[name]
                else None
            )
        case "boolean":
            return str(int(value)) if isinstance(value, bool) else None
        case "counter":
            return _number(name, value)
        case _:
            return None


def decode_native_fields(raw: cabc.Mapping[str, object]) -> dict[str, str]:
    """Copy only schema-owned measurements from native journal fields.

    Parameters
    ----------
    raw : Mapping[str, object]
        Native journal data whose prefixed measurement values must be text.
        Source, event and journal-unit trust are validated by the caller.

    Returns
    -------
    dict[str, str]
        Unprefixed canonical measurements. Missing, invalid and unknown fields
        are omitted independently; no raw values or identifiers are retained.

    Examples
    --------
    >>> decode_native_fields({"BUILD_LIMITS_QUEUED": "02", "MESSAGE": "private"})
    {'queued': '2'}
    """
    fields = {}
    for name in FIELD_KINDS:
        value = _native_field(name, raw.get(f"BUILD_LIMITS_{name.upper()}"))
        if value is not None:
            fields[name] = value
    return fields


def _native_field(name: str, value: object) -> str | None:
    """Decode scalar native text without accepting journal arrays or free text.

    Returns
    -------
    str | None
        Canonical bounded text, or no value when invalid.
    """
    if not isinstance(value, str):
        return None
    if name in BOOLEANS:
        return value if value in {"0", "1"} else None
    if FIELD_KINDS[name] == "counter":
        try:
            number = float(value) if name in DURATIONS else int(value)
        except ValueError:
            return None
        return bounded_field(name, number)
    return bounded_field(name, value)


def _number(name: str, value: object) -> str | None:
    """Bound counts as integers and durations as finite rounded seconds.

    Returns
    -------
    str | None
        The capped text value, or no value when invalid.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if value < 0:
        return None
    if name in DURATIONS:
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return f"{min(value, MAX_COUNTER):.3f}"
    if not isinstance(value, int):
        return None
    return str(min(value, MAX_COUNTER))


class Metrics:
    """Validate records and isolate every sink or warning failure.

    Parameters
    ----------
    sink : MetricsSink | None
        Injected transport; absent means discard records.
    source : str
        ``supervisor`` or ``adapter``.
    warn : Callable[[str], None] | None
        Receives at most one value-free warning; absent keeps emission silent.

    Attributes
    ----------
    dropped_records : int
        Capped count of invalid events and transport failures.
    dropped_fields : int
        Capped count of individually rejected fields.
    """

    def __init__(
        self,
        sink: MetricsSink | None = None,
        *,
        source: str = "supervisor",
        warn: cabc.Callable[[str], None] | None = None,
    ) -> None:
        """Bind a transport and validate the emitter's fixed source.

        Raises
        ------
        ValueError
            If the source is outside the schema.
        """
        if source not in {"supervisor", "adapter"}:
            msg = "unknown metrics source"
            raise ValueError(msg)
        self.sink = sink if sink is not None else NullSink()
        self.source = source
        self.warn = warn
        self.dropped_records = 0
        self.dropped_fields = 0
        self._warned = False

    def emit(self, event: str, **fields: object) -> None:
        """Emit a bounded record, dropping invalid fields individually.

        Parameters
        ----------
        event : str
            A catalogue event; invalid events drop the whole record.
        **fields : object
            Unprefixed measurements; unknown names never reach the sink.
        """
        if not isinstance(event, str) or event not in EVENTS:
            self._drop()
            return
        record = {
            "BUILD_LIMITS_SCHEMA": "1",
            "BUILD_LIMITS_SOURCE": self.source,
            "BUILD_LIMITS_EVENT": event,
        }
        for name, value in fields.items():
            bounded = bounded_field(name, value)
            if bounded is None:
                self.dropped_fields = min(MAX_COUNTER, self.dropped_fields + 1)
                self._warning()
            else:
                record[f"BUILD_LIMITS_{name.upper()}"] = bounded
        try:
            self.sink.emit(record)
        except Exception:  # ruff: ignore[blind-except] - telemetry cannot change admission or compiler results.
            self._drop()

    def _drop(self) -> None:
        """Count one lost record without retaining its content."""
        self.dropped_records = min(MAX_COUNTER, self.dropped_records + 1)
        self._warning()

    def _warning(self) -> None:
        """Warn once, shielding admission from a failing diagnostic callback."""
        if self._warned or self.warn is None:
            return
        self._warned = True
        # Diagnostic callbacks are best-effort too; do not echo their errors.
        with contextlib.suppress(Exception):
            self.warn(
                "dropping compile-admission metrics outside the emission contract"
            )


def gauge_fields(counts: Counts) -> dict[str, int | bool]:
    """Map a domain snapshot to the fixed metric gauge vocabulary.

    Parameters
    ----------
    counts : Counts
        Identifier-free admission state.

    Returns
    -------
    dict[str, int | bool]
        Six gauge values for the validating metrics boundary.
    """
    return {
        "queued": counts.queued,
        "held": counts.held,
        "running": counts.running,
        "draining": counts.draining,
        "server_drain": counts.server_drain,
        "capacity": counts.capacity,
    }
