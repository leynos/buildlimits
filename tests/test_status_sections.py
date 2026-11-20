"""Optional status sections accept any mapping, not only a built-in dict."""

import types

from buildlimits.status_sections import section_of


def test_a_mapping_that_is_not_a_dict_is_still_a_section() -> None:
    """A read-only view of the same mapping renders like the document it wraps.

    The status document is JSON today, so every section is a dict; the helper
    promises a Mapping, and refusing another implementation would silently drop
    a real section from the operator's summary.
    """
    section = types.MappingProxyType({"limits": {"max_bytes": 1}})

    assert section_of({"retention": section}, "retention") is section, (
        "the wrapped document is the section, not a copy of it"
    )


def test_a_value_that_is_not_a_mapping_is_not_a_section() -> None:
    """A malformed section contributes nothing rather than raising."""
    assert section_of({"retention": [("limits", {})]}, "retention") is None, (
        "a list is not a section, so the caller sees no section at all"
    )
