"""Guards and metadata edges of the in-place sealed rewrite.

The sealed and archive rewrites refuse each other's entries, and a source's
metadata can be absent or describe its seal time with a value this code did not
write. These tests pin those edges without repeating the fault protocol the
rewrite's own module drives.
"""

import typing as typ
from pathlib import Path

import pytest
from outcome_archive_helpers import NOW, age_policy, layout, retain
from segment_test_helpers import document

from buildlimits.outcome_segments import sealed_meta_name, segment_name

if typ.TYPE_CHECKING:
    from buildlimits.outcome_segment_scan import SegmentEntry


def _assert_refused(
    root: Path,
    state: typ.Literal["sealed", "archived"],
    writer: typ.Literal["trim", "recompress"],
    message: str,
) -> None:
    """Assert one writer refuses an entry the other writer owns."""
    from buildlimits.outcome_archive_context import ArchiveContext
    from buildlimits.outcome_archive_files import recompress_archive
    from buildlimits.outcome_archive_trim import Trim
    from buildlimits.outcome_sealed_trim import trim_sealed_source

    entry = _entry_in_state(root, state)
    context = ArchiveContext(
        layout=layout(root),
        policy=age_policy(),
        preset=0,
        now=NOW,
        quarantine_token=lambda: "0123456789ab",
        clock=lambda: NOW,
    )
    write = trim_sealed_source if writer == "trim" else recompress_archive

    with pytest.raises(ValueError, match=message):
        write(context, entry, Trim())


@pytest.mark.parametrize(
    ("state", "writer", "message"),
    [
        ("archived", "trim", "sealed source"),
        ("sealed", "recompress", "published archive"),
    ],
    ids=["trim-refuses-archive", "recompress-refuses-source"],
)
def test_each_writer_refuses_the_form_the_other_owns(
    tmp_path: Path,
    state: typ.Literal["sealed", "archived"],
    writer: typ.Literal["trim", "recompress"],
    message: str,
) -> None:
    """Each rewrite refuses the entry form its sibling owns."""
    _assert_refused(tmp_path, state, writer, message)


def _entry_in_state(
    root: Path, state: typ.Literal["sealed", "archived"]
) -> SegmentEntry:
    """Build one entry of the given state, whose path need not exist.

    Returns
    -------
    SegmentEntry
        The entry the guard reads.
    """
    from buildlimits.outcome_segment_scan import SegmentEntry as _Entry

    return _Entry(
        sequence=1,
        path=layout(root).archive_dir / "segment-000000000001.jsonl.xz",
        state=state,
        physical_bytes=10,
        logical_bytes=20,
        record_count=1,
        has_metadata=True,
    )


def test_a_sealed_source_without_metadata_uses_the_runs_seal_time(
    tmp_path: Path,
) -> None:
    """An unreadable seal time falls back to the run's own moment."""
    from buildlimits.outcome_archive_context import ArchiveContext
    from buildlimits.outcome_sealed_trim import sealed_at_of

    context = ArchiveContext(
        layout=layout(tmp_path),
        policy=age_policy(),
        preset=0,
        now=NOW,
        quarantine_token=lambda: "0123456789ab",
        clock=lambda: NOW,
    )
    missing = sealed_at_of(context, 1)
    directory = layout(tmp_path).sealed_dir
    directory.mkdir(parents=True, exist_ok=True)
    (directory / sealed_meta_name(1)).write_text(
        '{"schema":1,"sequence":1,"sealed_at":7}', encoding="utf-8"
    )
    malformed = sealed_at_of(context, 1)

    assert missing is None, "an absent metadata document has no seal time"
    assert malformed is None, "a non-text seal time is not trusted"


def test_a_terminated_corrupt_row_is_quarantined(tmp_path: Path) -> None:
    """A terminated row that does not decode is corruption, not a torn tail."""
    directory = layout(tmp_path).sealed_dir
    directory.mkdir(parents=True, exist_ok=True)
    # The invalid row is followed by another so the streaming decoder,
    # rather than the end-of-stream check, refuses it.
    (directory / segment_name(1)).write_bytes(document(()) + b"{bad}\n{}\n")

    report = retain(tmp_path, age_policy())

    assert report.status == "corrupt", "the fault needs an operator"
    assert [(action.action, action.reason) for action in report.actions] == [
        ("quarantined", "corrupt")
    ], "the unreadable source is quarantined"
