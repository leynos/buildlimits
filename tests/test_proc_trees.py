"""Shared parsing for process identities read from ``/proc``."""

from pathlib import Path

import pytest

from buildlimits.proc_trees import process_starttime, read_process_starttime


def test_process_starttime_counts_fields_after_parenthesized_command() -> None:
    """A command containing spaces and parentheses does not shift field 22."""
    fields = ["S", *(["0"] * 18), "456"]
    stat_text = f"123 (rustc (wrapper) worker) {' '.join(fields)}"

    assert process_starttime(stat_text) == 456, (
        "the start time is counted from after the final command parenthesis"
    )


@pytest.mark.parametrize(
    "stat_text",
    [
        "123 rustc S " + " ".join(["0"] * 18 + ["456"]),
        "123 (rustc) S 0",
        "123 (rustc) S " + " ".join(["0"] * 18 + ["malformed"]),
        "123 (rustc) S " + " ".join(["0"] * 18 + ["-1"]),
    ],
    ids=("missing-command-parentheses", "missing-field", "invalid-field", "negative"),
)
def test_process_starttime_rejects_malformed_or_negative_field_22(
    stat_text: str,
) -> None:
    """Malformed process identities cannot be used for PID reuse checks."""
    with pytest.raises(ValueError, match="process"):
        process_starttime(stat_text)


def test_read_process_starttime_uses_the_injected_proc_root(tmp_path: Path) -> None:
    """The shared reader validates the stat row at its filesystem boundary."""
    stat_file = tmp_path / "123" / "stat"
    stat_file.parent.mkdir()
    stat_file.write_text(
        "123 (rustc (wrapper)) S " + " ".join(["0"] * 18 + ["456"]),
        encoding="ascii",
    )

    assert read_process_starttime(123, tmp_path) == 456, (
        "the injected proc root supplies the process identity row"
    )
