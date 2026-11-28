"""An independent model of the sequences, rows and floor a store should hold.

The archive lifecycle property compares what an operator reads with an oracle
it maintains itself, because an expectation read back from the implementation
cannot fail when the implementation is wrong. This model holds the closed
sequences a generated store should have, the rows each one should replay, and
the retention floor those sequences imply. It applies the retention rules
directly — a record budget deletes whole entries oldest first and shortens the
first that does not fit, an age cutoff expires entries observed wholly before
it — and derives the reportable gaps from its own floor rather than from the
floor a run reports.

Every source the generator produces holds rows observed within minutes of each
other while every cutoff is days away, so no generated entry ever straddles a
cutoff. The age rule is still modelled in full so the model does not depend on
that coincidence. Only the lifecycle property uses this module.
"""

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import datetime as dt

    from buildlimits.outcome_record_format import OutcomeRecord

__all__ = ["LifecycleModel", "one_row_per_source"]


def one_row_per_source(rows: cabc.Sequence[OutcomeRecord]) -> LifecycleModel:
    """Return the model of a writer that seals every row as its own source.

    Parameters
    ----------
    rows : Sequence[OutcomeRecord]
        The rows the writer stored, oldest first.

    Returns
    -------
    LifecycleModel
        One sequence per row, numbered from one, with nothing active. The
        counter remembers the last sequence, so a later seal is numbered after
        every sequence that was ever written, not after the highest survivor.

    Examples
    --------
    Four rows produce the sequences ``1`` to ``4``; losing sequence ``2``
    leaves the counter at ``4``, so the next seal is sequence ``5``.
    """
    return LifecycleModel(
        groups={number: [row] for number, row in enumerate(rows, start=1)},
        active=[],
        counter=len(rows),
    )


@dc.dataclass(slots=True)
class LifecycleModel:
    """The closed history one generated store should hold.

    Attributes
    ----------
    groups : dict[int, list[OutcomeRecord]]
        The rows each surviving sequence holds, keyed by sequence in ascending
        order. A sequence the store no longer has simply has no key.
    active : list[OutcomeRecord]
        The rows the writer has not sealed, which retention cannot evict.
    counter : int
        The highest sequence the writer has allocated. Numbering is monotonic,
        so a deleted sequence is never reused.
    floor : int | None
        The floor the last completed run should have recorded, or None before
        any run has recorded one.
    removed : set[int]
        Sequences retention removed on purpose, whether or not the floor could
        advance past them. A removal above an earlier hole is not a gap: the
        store keeps a durable record of the removal, so the report names only
        sequences without one.
    """

    groups: dict[int, list[OutcomeRecord]]
    active: list[OutcomeRecord]
    counter: int
    floor: int | None = None
    removed: set[int] = dc.field(default_factory=set)

    @property
    def records(self) -> list[OutcomeRecord]:
        """The rows replay should yield, in sequence then append order.

        Returns
        -------
        list[OutcomeRecord]
            Every closed sequence's rows oldest sequence first, then the rows
            still active.
        """
        return [
            row for sequence in sorted(self.groups) for row in self.groups[sequence]
        ] + self.active

    @property
    def gaps(self) -> tuple[int, ...]:
        """The sequences a report should name as missing.

        Only a sequence between the floor and the highest survivor is
        reportable: below the floor retention removed it on purpose, and above
        every survivor the store is indistinguishable from one that simply
        ended there. A sequence retention removed above an earlier hole is not
        a gap either, even though the floor cannot advance past it: the store
        keeps a durable removal record the scan reads.

        Returns
        -------
        tuple[int, ...]
            The absent sequences oldest first.
        """
        if not self.groups:
            return ()
        # No recorded floor retains nothing intentionally, so sequence one is
        # the lowest a store can report; a recorded floor raises that bound.
        lowest = 1 if self.floor is None else max(1, self.floor)
        highest = max(self.groups)
        return tuple(
            sequence
            for sequence in range(lowest, highest)
            if sequence not in self.groups and sequence not in self.removed
        )

    def drop(self, sequence: int) -> None:
        """Remove one sequence, as losing its source file does.

        Parameters
        ----------
        sequence : int
            The sequence whose rows are gone. The counter is unchanged, so the
            lost sequence is not reused.
        """
        del self.groups[sequence]

    def seal(self, rows: cabc.Sequence[OutcomeRecord], cap: int) -> None:
        """Append rows the way a writer with a record cap does.

        The writer closes the active file before an append that would take it
        past the cap and keeps appending afterwards, so each sealed group
        holds the rows that were active when the cap was reached. A close
        never seals, so any rows left over stay active and unsequenced.

        Parameters
        ----------
        rows : Sequence[OutcomeRecord]
            The rows to append, oldest first.
        cap : int
            The writer's record cap.
        """
        for row in rows:
            if len(self.active) >= cap:
                self.counter += 1
                self.groups[self.counter] = self.active
                self.active = []
            self.active.append(row)

    def trim(self, cap: int) -> set[int]:
        """Apply a record budget over the whole retained store.

        The cap counts the active rows too, but retention cannot rewrite the
        active file, so it deletes closed entries oldest first while they fit
        the excess and shortens the first entry that does not. Every generated
        schedule leaves enough closed history to cover the excess; a run that
        could not would report itself unsettled instead of succeeding.

        Parameters
        ----------
        cap : int
            The record cap the policy applies.

        Returns
        -------
        set[int]
            The sequences the budget deletes whole.

        Raises
        ------
        AssertionError
            If the active rows alone exceed the cap, which no generated
            schedule creates and no settled run can satisfy.
        """
        closed = sum(len(rows) for rows in self.groups.values())
        excess = closed + len(self.active) - cap
        deleted: set[int] = set()
        for sequence in sorted(self.groups):
            if excess <= 0:
                break
            rows = self.groups[sequence]
            if len(rows) <= excess:
                del self.groups[sequence]
                deleted.add(sequence)
                excess -= len(rows)
                continue
            self.groups[sequence] = rows[excess:]
            excess = 0
        if excess > 0:
            # The run the property drives would report itself unsettled; the
            # model must not pretend a budget it cannot meet was met.
            msg = "a generated budget is met from closed history"
            raise AssertionError(msg)
        return deleted

    def expire(self, cutoff: dt.datetime) -> set[int]:
        """Apply an age cutoff to the closed history.

        An entry whose last observation is older than the cutoff goes whole.
        An entry that straddles it keeps only the rows observed at or after
        it; a generated entry never straddles one, but the rule is modelled
        in full rather than assumed away.

        Parameters
        ----------
        cutoff : datetime.datetime
            The oldest observation the policy retains.

        Returns
        -------
        set[int]
            The sequences the cutoff deletes whole.
        """
        deleted: set[int] = set()
        for sequence in sorted(self.groups):
            kept = [row for row in self.groups[sequence] if row.observed_at >= cutoff]
            if kept:
                self.groups[sequence] = kept
            else:
                del self.groups[sequence]
                deleted.add(sequence)
        return deleted

    def record_run(self, deleted: cabc.Iterable[int]) -> None:
        """Record the floor one completed run should leave.

        A store that has never recorded a floor starts at the lowest sequence
        it is known to have had: everything that survives this run plus
        everything it deleted. Only a contiguous deleted prefix then advances
        the floor, so a sequence that went missing for another reason keeps
        being reported as a gap. A deletion is remembered as intentional
        whether or not the floor could advance past it.

        Parameters
        ----------
        deleted : Iterable[int]
            The sequences this run deletes whole.
        """
        removed = set(deleted)
        self.removed.update(removed)
        if self.floor is None:
            self.floor = min([*self.groups, *removed], default=0)
        while self.floor in removed:
            self.floor += 1
