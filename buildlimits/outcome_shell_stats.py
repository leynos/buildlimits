"""Bounded, privacy-safe counters for the optional recording status section."""

import collections
import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    from buildlimits.outcome_ports import StoreResult
    from buildlimits.outcome_record_format import StoreRecord
    from buildlimits.outcome_recorder_types import Outputs

_ATTRIBUTED_KINDS = frozenset({"solo", "unanimous"})


@dc.dataclass(slots=True)
class OutcomeCounters:
    """Cumulative counts the supervisor status section may expose."""

    attributed: int = 0
    outcomes: int = 0
    record_omitted: int = 0
    record_omitted_key: int = 0
    record_omitted_label_time: int = 0
    uncorrelated_rows: int = 0
    repeated_attempts: int = 0
    _seen: collections.OrderedDict[tuple[str, str], None] = dc.field(
        default_factory=collections.OrderedDict, repr=False
    )
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
            self.record_omitted += int(event.record_omitted != "none")
            # Split by the closed omission vocabulary, so the status says
            # whether an outcome lacked its identity or its label time.
            self.record_omitted_key += int(event.record_omitted == "key_unavailable")
            self.record_omitted_label_time += int(
                event.record_omitted == "label_time_unavailable"
            )
            self.last_reason = event.outcome_reason
        for reason in outputs.reading_reasons:
            self.readings += 1
            self.readings_failed += int(reason != "none")
            self.last_reason = reason

    def note_record(self, record: StoreRecord) -> None:
        """Count one appended row's correlation evidence.

        A row that names no attempt is the legacy shape the replay reports as
        uncorrelated. A row whose attempt identity this process already
        appended is counted as a repeat; the writer cannot say whether it
        agrees with its original, because that verdict compares whole recorded
        evidence over the entire store and belongs to the replay. The index is
        bounded, so a long-lived process never grows it without limit.
        """
        attempt = getattr(record, "attempt", None)
        generation = getattr(record, "supervisor_generation", None)
        if attempt is None or generation is None:
            self.uncorrelated_rows += 1
            return
        key = (generation, attempt)
        self.repeated_attempts += int(key in self._seen)
        self._seen[key] = None
        self._seen.move_to_end(key)
        while len(self._seen) > _SEEN_ATTEMPTS:
            self._seen.popitem(last=False)

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
            "record_omitted": self.record_omitted,
            "record_omitted_key": self.record_omitted_key,
            "record_omitted_label_time": self.record_omitted_label_time,
            "uncorrelated_rows": self.uncorrelated_rows,
            "repeated_attempts": self.repeated_attempts,
            "readings": self.readings,
            "readings_skipped": self.readings_skipped,
            "readings_failed": self.readings_failed,
            "store_records": self.store_records,
            "store_discarded": self.store_discarded,
            "last_reason": self.last_reason,
        }


_SEEN_ATTEMPTS = 4096
"""Attempt identities one process remembers, so the index stays bounded."""

__all__ = ["OutcomeCounters"]
