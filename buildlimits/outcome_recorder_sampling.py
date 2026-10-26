"""Pure child-sighting folds for active recorder entries."""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_sightings import observe_sample

if typ.TYPE_CHECKING:
    from buildlimits.outcome_recorder_types import Progress, _Launch


def sampled(progress: Progress, metadata: frozenset[str]) -> Progress:
    """Fold one sample across every cached entry that is still running.

    Returns
    -------
    Progress
        A copy whose active and attribution-window entries contain the sample.
    """
    running = tuple(item for item in progress.active if item.runtime_seconds is None)
    by_metadata: dict[str, list[str]] = {}
    for item in running:
        value = item.payload.metadata
        if value is not None:
            by_metadata.setdefault(value, []).append(item.group_id)

    seen: set[str] = set()
    ambiguous: set[str] = set()
    for value in metadata:
        matches = by_metadata.get(value, ())
        if len(matches) == 1:
            seen.add(matches[0])
        elif len(matches) > 1:
            ambiguous.update(matches)

    def update(item: _Launch) -> _Launch:
        """Count the sample and attach only a unique or shared exact match.

        Returns
        -------
        _Launch
            The updated active launch with bounded sighting state.
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

    updated = {item.group_id: update(item) for item in running}
    active = tuple(updated.get(item.group_id, item) for item in progress.active)
    window = dc.replace(
        progress.window,
        entries=tuple(
            updated.get(item.group_id, item) for item in progress.window.entries
        ),
    )
    return dc.replace(progress, active=active, window=window)
