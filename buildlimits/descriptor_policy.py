"""Pure queued-descriptor budget policy over a process sample.

DescriptorBudget owns operating-system sampling; this calculation owns only
the retention ceiling and launch reserve, independent of descriptor ownership.
"""

import dataclasses as dc


@dc.dataclass(frozen=True, slots=True)
class DescriptorSample:
    """Explicit descriptor baseline with unrecorded receipt accounted for.

    Attributes
    ----------
    soft : int
        Effective descriptor soft limit; a negative value denotes infinity.
    opened : int | None
        Sampled open count, or None when sampling was unavailable.
    receiving : int
        Unrecorded request descriptors already included in the sample.
    """

    soft: int
    opened: int | None
    receiving: int


def calculate_budget(
    sample: DescriptorSample,
    slots: int,
    *,
    reserve: int,
    unlimited_ceiling: int,
) -> int:
    """Calculate request retention from explicitly sampled descriptor usage.

    Parameters
    ----------
    sample : DescriptorSample
        Explicit process sample and its unrecorded receipt adjustment.
    slots : int
        Configured compiler groups, each requiring two launch channels.
    reserve : int
        Additional descriptors reserved for control-path operations.
    unlimited_ceiling : int
        Finite retention ceiling when the sampled soft limit is unlimited.

    Returns
    -------
    int
        Non-negative retention budget. An unknown sample permits no retention.

    Examples
    --------
    >>> sample = DescriptorSample(64, 12, 4)
    >>> calculate_budget(sample, 3, reserve=16, unlimited_ceiling=1024)
    34
    >>> unknown = DescriptorSample(64, None, 0)
    >>> calculate_budget(unknown, 3, reserve=16, unlimited_ceiling=1024)
    0
    """
    ceiling = sample.soft if sample.soft >= 0 else unlimited_ceiling
    baseline = (
        ceiling if sample.opened is None else max(0, sample.opened - sample.receiving)
    )
    return max(0, ceiling - baseline - reserve - 2 * slots)
