"""Pure child-sighting folds for active recorder entries."""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_sightings import observe_sample

if typ.TYPE_CHECKING:
    from buildlimits.outcome_recorder_types import Progress, _Launch


def _metadata_index(running: tuple[_Launch, ...]) -> dict[str, list[str]]:
    """Index each running launch by its non-empty request identity.

    Returns
    -------
    dict[str, list[str]]
        Metadata values mapped to the group ids that reported them.
    """
    index: dict[str, list[str]] = {}
    for item in running:
        value = item.payload.metadata
        if value is not None:
            index.setdefault(value, []).append(item.group_id)
    return index


def _matches(
    metadata: frozenset[str], index: dict[str, list[str]]
) -> tuple[set[str], set[str]]:
    """Separate uniquely matched groups from groups sharing an identity.

    Returns
    -------
    tuple[set[str], set[str]]
        Group ids seen once, and group ids that matched more than once.
    """
    seen: set[str] = set()
    ambiguous: set[str] = set()
    for value in metadata:
        matches = index.get(value, ())
        if len(matches) == 1:
            seen.add(matches[0])
        elif len(matches) > 1:
            ambiguous.update(matches)
    return seen, ambiguous


def _observed(item: _Launch, seen: set[str], ambiguous: set[str]) -> _Launch:
    """Count one sample on a running launch, keeping its bounded state.

    Returns
    -------
    _Launch
        The updated launch, or the unchanged ended launch.
    """
    if item.runtime_seconds is not None:
        return item
    return dc.replace(
        item,
        sightings=observe_sample(
            item.sightings,
            is_unique_match=item.group_id in seen,
            is_shared_match=item.group_id in ambiguous,
        ),
    )


def sampled(progress: Progress, metadata: frozenset[str]) -> Progress:
    """Fold one sample across every cached entry that is still running.

    Returns
    -------
    Progress
        A copy whose active and attribution-window entries contain the sample.
    """
    running = tuple(item for item in progress.active if item.runtime_seconds is None)
    seen, ambiguous = _matches(metadata, _metadata_index(running))
    updated = {item.group_id: _observed(item, seen, ambiguous) for item in running}
    active = tuple(updated.get(item.group_id, item) for item in progress.active)
    window = dc.replace(
        progress.window,
        entries=tuple(
            updated.get(item.group_id, item) for item in progress.window.entries
        ),
    )
    return dc.replace(progress, active=active, window=window)
