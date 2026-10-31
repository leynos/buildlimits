"""Bounded, privacy-safe counters for the optional recording status section."""

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    from buildlimits.outcome_ports import StoreResult
    from buildlimits.outcome_recorder_types import Outputs

_ATTRIBUTED_KINDS = frozenset({"solo", "unanimous"})


@dc.dataclass(slots=True)
class OutcomeCounters:
    """Cumulative counts the supervisor status section may expose."""

    attributed: int = 0
    outcomes: int = 0
    readings: int = 0
    readings_skipped: int = 0
    readings_failed: int = 0
    store_records: int = 0
    store_discarded: int = 0
    last_reason: str | None = None

    def record(self, outputs: Outputs) -> None:
        """Fold one recorder transition into the cumulative counts.

        Examples
        --------
        One labelled window adds an outcome and keeps its reason.
        """
        for event in outputs.events:
            self.outcomes += 1
            self.attributed += int(event.outcome_attribution in _ATTRIBUTED_KINDS)
            self.last_reason = event.outcome_reason
        for reason in outputs.reading_reasons:
            self.readings += 1
            self.readings_failed += int(reason != "none")
            self.last_reason = reason

    def skip_reading(self) -> None:
        """Count one due gate that deliberately did not read."""
        self.readings_skipped += 1

    def record_store(self, result: StoreResult) -> None:
        """Fold one aggregated writer result into the store counts."""
        self.store_records += result.records
        self.store_discarded += result.discarded

    def describe(self) -> dict[str, object]:
        """Return the bounded counter payload for the status section.

        Returns
        -------
        dict[str, object]
            Counts and the last closed reason, without private values.
        """
        return {
            "attributed": self.attributed,
            "outcomes": self.outcomes,
            "readings": self.readings,
            "readings_skipped": self.readings_skipped,
            "readings_failed": self.readings_failed,
            "store_records": self.store_records,
            "store_discarded": self.store_discarded,
            "last_reason": self.last_reason,
        }


__all__ = ["OutcomeCounters"]
