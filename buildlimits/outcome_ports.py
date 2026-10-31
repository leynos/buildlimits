"""Domain-owned ports and result values for outcome recording."""

import collections.abc as cabc
import dataclasses as dc
import typing as typ

from buildlimits.outcome_key import Salt as Salt

if typ.TYPE_CHECKING:
    from buildlimits.outcome_attribution import Generation
    from buildlimits.outcome_record_format import StoreRecord
    from buildlimits.outcome_vocabulary import StoreOperation, StoreReason


@dc.dataclass(frozen=True, slots=True)
class Ticket:
    """Opaque identity and deadline for one statistics query."""

    serial: int
    deadline: float


@dc.dataclass(frozen=True, slots=True)
class Pending:
    """A statistics child that has not produced a terminal result."""


@dc.dataclass(frozen=True, slots=True)
class StatisticsText:
    """Unparsed statistics text returned by the statistics adapter."""

    text: str


@dc.dataclass(frozen=True, slots=True)
class QueryFailure:
    """A bounded statistics query failure with a closed reason."""

    reason: typ.Literal["timeout", "read_error"]


@dc.dataclass(frozen=True, slots=True)
class GenerationFailure:
    """A server identity query that could not produce a generation."""

    reason: typ.Literal["generation_unavailable"]


@dc.dataclass(frozen=True, slots=True)
class SampleFailure:
    """A bounded failure while sampling full-emit compiler children."""

    reason: typ.Literal["server_unknown", "group_unreadable"]


@dc.dataclass(frozen=True, slots=True)
class StoreFailure:
    """A bounded store or salt operation failure."""

    reason: StoreReason


@dc.dataclass(frozen=True, slots=True)
class StoreResult:
    """One completed writer operation and its bounded counters."""

    operation: StoreOperation
    reason: StoreReason
    records: int
    discarded: int


type SaltSource = cabc.Callable[[], Salt | StoreFailure]


class StatisticsPort(typ.Protocol):
    """Start, poll and close one bounded statistics query."""

    def start(self, now: float) -> Ticket | QueryFailure:
        """Start a query using an explicit monotonic time.

        Example:
            A query started at ``10.0`` has a deadline no later than ``12.0``.
        """
        ...

    def poll(
        self, ticket: Ticket, now: float
    ) -> Pending | StatisticsText | QueryFailure:
        """Poll a query without waiting beyond the caller's tick.

        Example:
            An unfinished query returns ``Pending()`` at an early poll.
        """
        ...

    def close(self, ticket: Ticket, /) -> None:
        """Close a ticket idempotently after a terminal result.

        Example:
            A timed-out ticket can be closed without a second poll.
        """
        ...


class ServerProcessPort(typ.Protocol):
    """Read the identity of the managed server that answered the query."""

    def generation(self) -> Generation | GenerationFailure | None:
        """Return the server identity without exposing process paths.

        Example:
            A refused connection returns ``None``; a server without
            ``INVOCATION_ID`` returns a generation with an empty invocation.
            Other failures return ``GenerationFailure("generation_unavailable")``.
        """
        ...

    def full_emit_metadata(self) -> frozenset[str] | SampleFailure:
        """Return metadata values from the current server's full-emit children.

        Returns
        -------
        frozenset[str] | SampleFailure
            In-memory child metadata or a closed sampling failure.
        """
        ...


class OutcomeStorePort(typ.Protocol):
    """Append bounded records and drain asynchronous writer results."""

    def salt_result(self) -> Salt | StoreFailure | None:
        """Return the asynchronous, one-shot salt handoff without waiting.

        Example:
            Before writer startup completes, ``salt_result()`` returns None.
        """
        ...

    def append(self, record: StoreRecord) -> None:
        """Enqueue a record without waiting for file I/O.

        Example:
            A full queue reports failure through a later ``results()`` value.
        """
        ...

    def results(self) -> tuple[StoreResult, ...]:
        """Return pending writer results without waiting for the writer.

        Example:
            An idle writer returns ``()``.
        """
        ...

    def close(self, timeout: float) -> None:
        """Stop the writer within the supplied bound.

        Example:
            ``close(1.0)`` gives the writer at most one second to stop.
        """
        ...
