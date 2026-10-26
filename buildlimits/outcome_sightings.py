"""Pure rustc metadata matching for bounded cache-child samples."""

import dataclasses as dc
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_record_format import ChildSighting


@dc.dataclass(frozen=True, slots=True)
class ChildSightings:
    """Bounded child evidence retained while a cached group is active."""

    samples: int = 0
    is_seen: bool = False
    is_ambiguous: bool = False
    has_metadata: bool = False

    def sighting(self) -> ChildSighting:
        """Return the closed-vocabulary result for the retained evidence.

        Returns
        -------
        ChildSighting
            ``seen`` wins, followed by ``ambiguous`` and then ``not_seen``;
            missing request metadata always gives ``unavailable``.
        """
        if not self.has_metadata:
            return "unavailable"
        if self.is_seen:
            return "seen"
        if self.is_ambiguous:
            return "ambiguous"
        if self.samples:
            return "not_seen"
        return "unavailable"


def full_emit_metadata(argv: cabc.Sequence[str]) -> str | None:
    """Return metadata only for a full rustc or clippy-driver invocation.

    Returns
    -------
    str | None
        The request metadata for a full emit, or None for another command or
        a dep-info-only pass.

    Examples
    --------
    ``full_emit_metadata(("rustc", "--emit=dep-info"))`` is None.
    """
    if not argv or Path(argv[0]).name not in {"rustc", "clippy-driver"}:
        return None
    emits = _emit_kinds(argv)
    if emits is not None and not emits.intersection({"link", "metadata"}):
        return None
    return request_metadata(argv)


def observe_sample(
    sightings: ChildSightings,
    *,
    is_unique_match: bool,
    is_shared_match: bool,
) -> ChildSightings:
    """Fold one successful sample taken while an entry was running.

    Returns
    -------
    ChildSightings
        A copy with one successful sample and its bounded match evidence.
    """
    return dc.replace(
        sightings,
        samples=sightings.samples + 1,
        is_seen=sightings.is_seen or is_unique_match,
        is_ambiguous=sightings.is_ambiguous or is_shared_match,
    )


def request_metadata(argv: cabc.Sequence[str]) -> str | None:
    """Read one rustc metadata value without retaining the argument vector.

    Returns
    -------
    str | None
        The first non-empty metadata value, or None when absent.

    Examples
    --------
    ``request_metadata(("rustc", "-Cmetadata=abc"))`` returns ``"abc"``.
    """
    for index, argument in enumerate(argv):
        value: str | None = None
        if argument in {"-C", "--codegen"} and index + 1 < len(argv):
            setting = argv[index + 1]
            if setting.startswith("metadata="):
                value = setting.partition("=")[2]
        elif argument.startswith(("-Cmetadata=", "--codegen=metadata=")):
            value = argument.partition("=")[2].removeprefix("metadata=")
        if value:
            return value
    return None


def _emit_kinds(argv: cabc.Sequence[str]) -> frozenset[str] | None:
    """Read every rustc emit set, where None means rustc's default full emit.

    Returns
    -------
    frozenset[str] | None
        The explicit emit kinds, or None when rustc defaults apply.
    """
    kinds: set[str] = set()
    found = False
    for index, argument in enumerate(argv):
        if argument == "--emit":
            value = argv[index + 1] if index + 1 < len(argv) else ""
        elif argument.startswith("--emit="):
            value = argument.partition("=")[2]
        else:
            continue
        found = True
        kinds.update(value.split(","))
    return frozenset(kinds) if found else None


__all__ = [
    "ChildSightings",
    "full_emit_metadata",
    "observe_sample",
    "request_metadata",
]
