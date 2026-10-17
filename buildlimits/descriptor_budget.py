"""Descriptor admission budget, independent of compiler-token capacity.

Only pending request sockets and received rights count as retained. Startup
reserves control/launch headroom, and transient reception pressure is handled
by framing truncation and accept backpressure rather than by growing the queue.
"""

import resource

from buildlimits.descriptor_policy import DescriptorSample, calculate_budget
from buildlimits.descriptors import open_descriptors_except
from buildlimits.framing import MAX_FDS

DESCRIPTOR_RESERVE = 16
# An unlimited soft limit still needs a finite queued-retention ceiling.
UNLIMITED_DESCRIPTOR_CEILING = MAX_FDS * 128


def open_count() -> int | None:
    """Sample verified open descriptors without counting the directory handle.

    Returns
    -------
    int | None
        Open descriptors, or None when pressure prevents sampling.
    """
    try:
        return len(open_descriptors_except(()))
    except OSError:
        return None


class DescriptorBudget:
    """Bound queued retention independently of compiler-token capacity.

    Attributes
    ----------
    soft, hard : int
        Effective RLIMIT_NOFILE limits captured at startup or an idle recheck.
    budget : int
        Maximum request-owned sockets and received descriptors after reserving
        control-path headroom and two launch channels per configured group.
    high_water : int
        Largest retained count accepted within the budget.
    slots : int
        Configured compiler-group capacity used by the launch reserve.
    baseline_known : bool
        Whether the cached budget used a verified descriptor baseline.
    """

    def __init__(self) -> None:
        """Capture limits and an initial budget before requests arrive."""
        self.soft, self.hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        self.budget = 0
        self.high_water = 0
        self.slots = 0
        self.configure()

    def configure(self, slots: int | None = None, receiving: int = 0) -> None:
        """Recapture an idle baseline and reserve running launch channels.

        Parameters
        ----------
        slots : int | None
            Configured concurrent groups, or None to retain the existing reserve.
        receiving : int
            Unrecorded request descriptors included in the current sample.
        """
        if slots is not None:
            self.slots = slots
        self.soft, self.hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        sample = open_count()
        self.baseline_known = sample is not None
        self.budget = calculate_budget(
            DescriptorSample(self.soft, sample, receiving),
            self.slots,
            reserve=DESCRIPTOR_RESERVE,
            unlimited_ceiling=UNLIMITED_DESCRIPTOR_CEILING,
        )

    def recheck_if_idle(self, retained: int, receiving: int) -> None:
        """Recover an exhausted idle budget without sampling retained ownership.

        Parameters
        ----------
        retained : int
            Descriptors already owned by pending requests.
        receiving : int
            Unrecorded socket and received rights included in the live sample.
        """
        if not retained and self.budget == 0:
            self.configure(receiving=receiving)

    def receipt_ceiling(self, retained: int) -> int:
        """Reserve the accepted socket before permitting any additional rights.

        Parameters
        ----------
        retained : int
            Descriptors already owned by pending requests.

        Returns
        -------
        int
            Rights allowed by the remaining budget and the transport ceiling.
        """
        return min(MAX_FDS, max(0, self.budget - retained - 1))

    def observe(self, retained: int) -> None:
        """Record the high-water count after accepting a budgeted request.

        Parameters
        ----------
        retained : int
            Request-owned descriptors currently retained.
        """
        self.high_water = max(self.high_water, retained)

    def describe(
        self, retained: int, *, sample: dict[str, int | None] | None = None
    ) -> dict[str, int | None]:
        """Report provisional point-in-time descriptor diagnostics.

        Parameters
        ----------
        retained : int
            Current request-owned descriptor count.
        sample : dict[str, int | None] | None
            Already collected process gauges, including ``open``, to share one
            live sample across status interfaces. None samples usage locally.

        Returns
        -------
        dict[str, int | None]
            Effective limits, sample, budget, retention and high-water count.
        """
        return {
            "soft": self.soft,
            "hard": self.hard,
            "open": open_count() if sample is None else sample["open"],
            "retained": retained,
            "high_water": self.high_water,
            "budget": self.budget,
            **(sample or {}),
        }
