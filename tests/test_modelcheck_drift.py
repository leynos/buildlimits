"""Drift is found by comparison, and only by comparison.

The model's ``# maps:`` annotations are its claim to transcribe the production
surface. Checking that claim mechanically cannot show a transcription is
right; it can only show that nothing on the production side has been added or
renamed without the model following. The tests therefore state the comparison
as a property over generated inventories as well as on examples, because the
interesting failures are the ones an example would not think of: an item that
is both excluded and mapped, a tag that appears with odd spacing, a mapping
whose dotted path has a trailing dot.

Non-vacuity is the roadmap's own success criterion: a fake ``Effect`` added to
the inventory and a renamed mapped path must each produce a non-empty report.
Both controls are here, because a drift check that reports nothing on a broken
inventory would pass every other test in this module.

Every test here was written before its module was implemented (EP-M2), so it
pins a contract the stub could not satisfy.
"""

import typing as typ

from hypothesis import given
from hypothesis import strategies as st

if typ.TYPE_CHECKING:
    from modelcheck.drift import DriftReport

_EVENT = "buildlimits.admission.Admission.token_arrived"
"""A production event the model claims to transcribe."""

_EFFECT = "buildlimits.effects.Exit"
"""An inventory item no model action maps, excluded with a reason."""

_PATH = st.from_regex(r"[a-z]{1,4}\.[A-Za-z_]{1,6}", fullmatch=True)
"""A dotted path shaped like the production surface.

The alphabet is bounded because an unbounded ``+`` spends Hypothesis's
generation budget on shrinking candidates and trips its own slow-input health
check before the property runs.
"""

_GENERATED_SOURCE = """\
action Gate:            # maps: buildlimits.admission.Admission.token_arrived
  g = 1

action Stop:            # maps: buildlimits.recovery_stop.request
  stopped = True
"""
"""A model whose two actions each claim one production path."""


def _report(
    *, mapped: frozenset[str], inventory: frozenset[str], exclusions: dict[str, str]
) -> DriftReport:
    """Compare one mapping set with one inventory, resolving every mapped path.

    Most of these tests vary one account at a time — the inventory, the
    mappings, or the exclusions — so the resolver is spelled out here rather
    than repeated at each call site. A test about staleness supplies its own.

    Parameters
    ----------
    mapped : frozenset[str]
        The dotted paths the model claims to transcribe.
    inventory : frozenset[str]
        The production surface to compare against.
    exclusions : dict[str, str]
        Path to the reason it is deliberately unmodelled.

    Returns
    -------
    DriftReport
        The comparison.
    """
    from modelcheck import drift

    return drift.drift(mapped, inventory, lambda path: True, exclusions)


def _assert_unmapped(item: str) -> None:
    """Assert that an inventory item no action maps is reported by name.

    Two controls below put a different item into the inventory and require the
    same outcome. They differ only in what the item represents, so the
    mechanism is stated once and each control keeps its own rationale.

    Parameters
    ----------
    item : str
        The production path to add to the inventory unmapped.
    """
    report = _report(
        mapped=frozenset({_EVENT}),
        inventory=frozenset({_EVENT, item}),
        exclusions={},
    )

    assert item in report.unmapped, "the item is named"
    assert not report.is_clean, "and the report is not clean"


def test_an_unmapped_item_is_named() -> None:
    """An inventory item with no mapping and no exclusion is a finding.

    This is the roadmap's negative control. A production surface the model does
    not account for is exactly what the check exists to notice, so it must
    appear in the report by name rather than only making the report dirty.
    """
    _assert_unmapped(_EFFECT)


def test_excluding_an_item_with_a_reason_clears_it() -> None:
    """A recorded exclusion is a considered gap, not a forgotten one.

    The item leaves ``unmapped`` and is carried in ``excluded`` so a reader can
    see that the gap was deliberate. A recorded exclusion is the more specific
    statement, so an exclusion for an item that is also mapped makes the item
    excluded rather than accounted for twice.
    """
    report = _report(
        mapped=frozenset({_EVENT}),
        inventory=frozenset({_EVENT, _EFFECT}),
        exclusions={_EFFECT: "reported by the exit status, not modelled"},
    )

    assert not report.unmapped, "the exclusion clears it"
    assert report.excluded == (_EFFECT,), "and is carried, not dropped"
    assert report.is_clean, "with a reason recorded it is clean"


def test_mapping_an_item_clears_it() -> None:
    """The same inventory is clean once the model maps the item too.

    The inventory is unchanged and the mappings grow, so any difference in the
    report is attributable to the mapping alone.
    """
    inventory = frozenset({_EVENT, _EFFECT})
    report = _report(
        mapped=frozenset({_EVENT, _EFFECT}), inventory=inventory, exclusions={}
    )

    assert not report.unmapped, "the mapping clears it"
    assert not report.stale, "and the path resolves"
    assert report.is_clean, "so the report is clean"


def test_a_mapped_path_that_no_longer_resolves_is_stale() -> None:
    """A rename the model has not followed names the old path.

    The report names the path the model still claims, not the one that replaced
    it: the model is what a reader has to change.
    """
    from modelcheck import drift

    renamed = "buildlimits.recovery_stop.request_stop"
    report = drift.drift(
        frozenset({_EVENT, "buildlimits.recovery_stop.request"}),
        frozenset({_EVENT, renamed}),
        lambda path: path in {_EVENT, renamed},
        {},
    )

    assert report.stale == ("buildlimits.recovery_stop.request",), "the old path"
    assert report.unmapped == (renamed,), "the new name is unmapped"
    assert not report.is_clean, "so the report is not clean"


def test_a_renamed_mapped_path_is_the_non_vacuity_control() -> None:
    """Renaming the resolver's answer alone makes the report non-empty.

    The previous test changes both the inventory and the resolver. This one
    changes only which path resolves, so a report that stayed clean would prove
    the comparison ignores ``resolves`` altogether.
    """
    from modelcheck import drift

    mapped = frozenset({_EVENT})

    clean = drift.drift(mapped, mapped, lambda path: True, {})
    assert clean.is_clean, "the baseline is clean"

    renamed = drift.drift(mapped, mapped, lambda path: False, {})
    assert renamed.stale == (_EVENT,), "only the renamed path is stale"
    assert not renamed.is_clean, "so the report is not clean"


def test_a_fake_effect_added_to_the_inventory_is_reported() -> None:
    """A made-up inventory item with a plausible name is still unmapped.

    The control the roadmap names: append to the real inventory shape an item
    no action maps, and require the report to name it. Using a real-looking
    dotted path rather than ``"FAKE"`` is what shows the comparison is by
    membership rather than by any structural coincidence.
    """
    _assert_unmapped("buildlimits.effects.Fabricated")


def test_the_report_is_sorted_and_deduplicated() -> None:
    """Two reports of the same comparison compare equal, whatever the input order.

    ``frozenset`` and mapping order are not stable across runs, and a report
    that printed them in arrival order would churn in a snapshot without
    anything having changed.
    """
    from modelcheck import drift

    first = drift.drift(
        frozenset({"b.Mapped", "a.Mapped"}),
        frozenset({"c.Unmapped", "a.Mapped"}),
        lambda path: path.endswith("Mapped"),
        {},
    )
    second = drift.drift(
        frozenset({"a.Mapped", "b.Mapped"}),
        frozenset({"a.Mapped", "c.Unmapped"}),
        lambda path: path.endswith("Mapped"),
        {},
    )

    assert first == second, "the same input reports the same thing"
    assert first.unmapped == ("c.Unmapped",), "sorted and deduplicated"
    assert not first.stale, "and nothing is stale"


def test_parse_mappings_reads_every_annotation() -> None:
    """Each ``# maps:`` comment yields its dotted path, deduplicated.

    The annotation is a comment, so it can follow any indentation and any
    amount of space after the colon; a line-oriented parser that required a
    fixed column would silently find nothing and report a clean drift.
    """
    from modelcheck import drift

    source = (
        "action Gate:  # maps: buildlimits.admission.Admission.token_arrived\n"
        "  g = 1\n"
        "action Stop:  # maps: buildlimits.recovery_stop.request\n"
        "  stopped = True\n"
        "action Twice:  # maps: buildlimits.recovery_stop.request\n"
        "  stopped = False\n"
    )

    assert drift.parse_mappings(source) == frozenset({
        _EVENT,
        "buildlimits.recovery_stop.request",
    }), "both annotations are read, the duplicate collapsed"


def test_parse_mappings_ignores_a_comment_without_a_path() -> None:
    """A ``# maps:`` with nothing after it names nothing, and is skipped.

    FizzBee comments are free text; the model may well explain what it does not
    map. Treating an empty annotation as the empty path would produce a stale
    finding for ``""``.
    """
    from modelcheck import drift

    source = (
        "action Gate:  # maps:\n"
        "  g = 1\n"
        "# maps: buildlimits.recovery_stop.request\n"
        "action Stop:\n"
        "  stopped = True\n"
    )

    assert drift.parse_mappings(source) == frozenset({
        "buildlimits.recovery_stop.request"
    }), "an empty annotation names nothing"


@given(
    items=st.sets(_PATH, max_size=10),
    claimed=st.sets(_PATH, max_size=10),
    broken=st.sets(_PATH, max_size=4),
)
def test_drift_partitions_the_inventory(
    items: set[str], claimed: set[str], broken: set[str]
) -> None:
    """Every inventory item is mapped, excluded or unmapped — exactly one.

    This is the property the whole check rests on. An item that appeared in two
    parts would make the report's counts disagree with the inventory; an item
    in none would be a production surface the check silently accepted.
    """
    from modelcheck import drift

    exclusions = {item: "deliberate" for item in sorted(broken) if item in claimed}
    report = drift.drift(
        frozenset(claimed),
        frozenset(items),
        lambda path: path not in broken,
        exclusions,
    )

    excluded = set(exclusions)
    mapped = claimed - excluded
    assert set(report.unmapped) == set(items) - claimed - excluded, "the remainder"
    assert set(report.excluded) == excluded, "every exclusion is carried"
    assert set(report.stale) == {path for path in mapped if path in broken}, (
        "every broken mapping is stale"
    )
    assert not set(report.unmapped) & (mapped | excluded), "and no item is in two"


def test_parse_mappings_round_trips_a_generated_model() -> None:
    """Every path written into a generated model is read back from it.

    The parse is a pure function of the text, so it has to survive the
    indentation, spacing and ordering a model author would actually write.
    """
    from modelcheck import drift

    paths = ("buildlimits.a.One", "buildlimits.b.Two", "buildlimits.c.Three")
    source = "".join(
        f"action A{index}:  # maps: {path}\n  x = {index}\n"
        for index, path in enumerate(paths)
    )

    assert drift.parse_mappings(source) == frozenset(paths), "every path round-trips"
