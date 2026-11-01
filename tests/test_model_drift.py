"""The two W1 models still account for the production surface.

H2 split the unsplit model into a coupling half and a transaction half. Each
half carries its own ``# maps:`` annotations, and each half omits the paths the
other one transcribes, so neither directory's drift report can be read alone:
what matters is that between them every in-scope production path is mapped by at
least one half, and the only unmapped paths are the agreed out-of-scope ones.

This is the check a pull request runs. It is cheap because it compares text with
an import of the production package rather than exploring a state space, which
is why it can gate every change while the matrix runs nightly.

A deliberate exclusion is only as good as its reason: an empty reason is a
silent omission wearing an exclusion's clothes, so one is checked here too.
"""

import collections.abc as cabc
from pathlib import Path

import pytest

from modelcheck import drift
from modelcheck.adapters.configuration import TomlConfigurations
from modelcheck.adapters.inventory import BuildlimitsInventory

_ROOT = Path(__file__).resolve().parents[1]
"""The checkout root, from which the model directories are named."""

_MODELS = {
    "coupling": _ROOT / "models" / "admission" / "coupling",
    "transaction": _ROOT / "models" / "admission" / "transaction",
}
"""The two W1 model directories, each with its own mappings and exclusions."""

_OUT_OF_SCOPE = frozenset({
    "buildlimits.admission.Admission.begin_closing",
    "buildlimits.admission.Admission.health_failed",
    "buildlimits.admission.Admission.launch_failed",
    "buildlimits.effects.Shutdown",
})
"""The production paths W1 does not model at all.

Closing, health and start-up are outside RFC 0001's W1 scope. Naming them here
rather than deriving them keeps the test a statement of the agreed scope: a
path that quietly joined or left this set would fail, and the diff would say
which decision changed.
"""


def _accounts(directory: Path) -> tuple[frozenset[str], cabc.Mapping[str, str]]:
    """Return one model's mapped paths and its exclusions.

    Parameters
    ----------
    directory : Path
        A model directory holding ``configurations.toml`` and one ``.fizz``.

    Returns
    -------
    tuple[frozenset[str], collections.abc.Mapping[str, str]]
        The paths its actions claim to transcribe, and its recorded exclusions
        with their reasons.
    """
    source = TomlConfigurations(directory)
    return drift.parse_mappings(source.source_text()), source.exclusions()


@pytest.mark.parametrize("model", sorted(_MODELS))
def test_each_model_accounts_for_what_it_claims(model: str) -> None:
    """Neither half has an unmapped path or a stale mapping.

    A half is allowed to omit the other half's paths, because it records them as
    exclusions. It is not allowed to omit a path silently, and it is not allowed
    to keep claiming a path that no longer resolves.
    """
    inventory = BuildlimitsInventory()
    mapped, exclusions = _accounts(_MODELS[model])

    report = drift.drift(mapped, inventory.items(), inventory.resolves, exclusions)

    assert report.is_clean, (
        f"{model} leaves {report.unmapped} unaccounted for "
        f"and maps {report.stale} that no longer resolve"
    )


@pytest.mark.parametrize("model", sorted(_MODELS))
def test_every_exclusion_gives_a_reason(model: str) -> None:
    """An exclusion records why, not merely that the path is left out.

    The drift report carries exclusions so a reader can tell a considered gap
    from a forgotten one. A blank reason defeats that, and would let the split
    drop a path while still reporting clean.
    """
    _, exclusions = _accounts(_MODELS[model])

    blank = sorted(path for path, reason in exclusions.items() if not reason.strip())

    assert not blank, f"{model} excludes {blank} without a reason"


def test_the_halves_together_cover_the_whole_surface() -> None:
    """Every in-scope path is mapped by at least one half.

    This is the claim the split has to keep. Each half excludes the paths the
    other one transcribes, so neither report alone proves coverage; the union
    does. Exclusions cannot stand in for a mapping here: a path excluded by
    *both* halves would otherwise count as covered although neither transcribes
    it. Only the agreed out-of-scope set is allowed to be unmapped, and it must
    still be recorded as an exclusion so a reader sees the decision.
    """
    inventory = BuildlimitsInventory()
    mapped: frozenset[str] = frozenset()
    excluded: frozenset[str] = frozenset()
    for directory in _MODELS.values():
        half_mapped, half_exclusions = _accounts(directory)
        mapped |= half_mapped
        excluded |= frozenset(half_exclusions)

    unaccounted = drift.union_gaps(mapped, inventory.items(), _OUT_OF_SCOPE)

    assert not unaccounted, (
        f"the split maps no half to {list(unaccounted)}; an exclusion in both "
        f"halves is not a transcription"
    )
    assert excluded >= _OUT_OF_SCOPE, (
        "W1's out-of-scope paths must be recorded as exclusions, not omitted: "
        f"missing {sorted(_OUT_OF_SCOPE - excluded)}"
    )


def test_an_exclusion_in_both_halves_cannot_replace_a_mapping() -> None:
    """A path left out of both halves is a gap, however both record it.

    This is the negative control for cross-half coverage. It models the loss
    that the union test exists to catch: the half that used to transcribe the
    path drops the mapping and records the same "the other half owns it"
    exclusion the other half already carried. Both per-half reports stay clean
    because each can point at the other, so only the union can see the gap.
    """
    out_of_scope = frozenset({"a.Closing"})
    inventory = frozenset({"a.Mapped", "a.BothClaimElsewhere", "a.Closing"})
    # Neither half maps the lost path; each excludes it as the other's work.
    halves = (
        (
            frozenset({"a.Mapped"}),
            {
                "a.BothClaimElsewhere": "transaction owns it",
                "a.Closing": "out of scope",
            },
        ),
        (frozenset(), {"a.BothClaimElsewhere": "coupling owns it"}),
    )
    mapped = frozenset().union(*(half[0] for half in halves))
    excluded = frozenset().union(*(frozenset(half[1]) for half in halves))

    # The weaker formula counts the path as accounted for, which is the bug.
    assert not inventory - mapped - excluded, (
        "the weaker formula must accept the hole, or this is not a control"
    )

    gaps = drift.union_gaps(mapped, inventory, out_of_scope)

    assert gaps == ("a.BothClaimElsewhere",), (
        "the path excluded by both halves is a gap, not a transcription"
    )
