"""Bounded immutable launch accounting for cache-statistics windows."""

import dataclasses as dc

MAX_WINDOW_ENTRIES = 64


@dc.dataclass(frozen=True, slots=True)
class OutcomeWindow[P]:
    """Retain a bounded sequence of opaque launch payloads.

    Example:
        ``OutcomeWindow[int]().append(7).entries`` is ``(7,)``.
    """

    entries: tuple[P, ...] = ()
    is_overflowed: bool = False

    def append(self, payload: P) -> OutcomeWindow[P]:
        """Add one payload, or mark overflow after the fixed bound.

        Returns
        -------
        OutcomeWindow[P]
            A new ledger; an overflowed ledger stays bounded.

        Example:
            The 65th append sets ``is_overflowed`` and retains the first 64.
        """
        if self.is_overflowed:
            return self
        if len(self.entries) == MAX_WINDOW_ENTRIES:
            return dc.replace(self, is_overflowed=True)
        return dc.replace(self, entries=(*self.entries, payload))
