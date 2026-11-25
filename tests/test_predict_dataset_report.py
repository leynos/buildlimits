"""The plain dataset report prints store and journal evidence apart.

The two blocks are independent measurements: the store block describes the
retained rows, and the journal block describes what the recorder itself
published. A snapshot written before one of them existed must still report the
other, so a report can never hide evidence the document holds.
"""

import dataclasses as dc
import typing as typ
from pathlib import Path

from outcome_test_helpers import OBSERVED_AT, attributed_record
from predict_test_helpers import journal_note

from buildlimits.metrics_records import Record
from buildlimits.outcome_record_format import DecodedStore, StoreHeader
from buildlimits.predict import dataset_report
from buildlimits.predict.accounting import HistoryLoss
from buildlimits.predict.dataset import Snapshot, build_snapshot
from buildlimits.predict.records import decode_history

if typ.TYPE_CHECKING:
    import pytest

    from buildlimits.predict.journal_provenance import JournalCounts


def _snapshot_with_journal(note: JournalCounts, *, with_diagnostics: bool) -> Snapshot:
    """Return an empty snapshot carrying ``note`` and optionally the store block.

    Returns
    -------
    Snapshot
        A snapshot whose journal evidence is independent of its store counts,
        mirroring a document written before the store accounting existed.
    """
    history = decode_history(DecodedStore(StoreHeader("fixed-salt", OBSERVED_AT), ()))
    snapshot = build_snapshot(
        history,
        now=OBSERVED_AT,
        history_loss=HistoryLoss.active_file_only(),
        journal=note,
    )
    return snapshot if with_diagnostics else dc.replace(snapshot, diagnostics=None)


def _report(
    note: JournalCounts,
    *,
    with_diagnostics: bool,
    capsys: pytest.CaptureFixture[str],
) -> str:
    """Report one snapshot and return what the command printed.

    The cases below differ only in whether the store block is present and in
    what they assert about the output, so the arrange-and-capture shape is one
    helper.

    Returns
    -------
    str
        The captured standard output.
    """
    dataset_report.report_snapshot(
        _snapshot_with_journal(note, with_diagnostics=with_diagnostics),
        Path("out.jsonl"),
    )
    return capsys.readouterr().out


def test_the_plain_report_prints_journal_evidence_without_store_diagnostics(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An older snapshot without store counts still reports the observer.

    The store block and the journal block are separate evidence. A snapshot
    written before the retained-store accounting existed carries no store
    counts, and skipping its journal counts as well would hide the recorder's
    own measurements for no reason.
    """
    note = journal_note(
        Record(
            "supervisor",
            "cache_outcome",
            {
                "cache_result": "hit",
                "outcome_reason": "none",
                "outcome_attribution": "solo",
                "crate_origin": "registry",
                "record_omitted": "none",
            },
            OBSERVED_AT,
        ),
        Record(
            "supervisor",
            "outcome_store",
            {
                "store_operation": "append",
                "store_reason": "none",
                "store_records": "1",
                "store_discarded": "0",
            },
            OBSERVED_AT,
        ),
    )

    output = _report(note, with_diagnostics=False, capsys=capsys)

    assert "label loss: no diagnostics recorded" in output, "the store gap is named"
    assert "observer: requests=0 outcomes=1" in output, (
        "the journal's own counts are still reported"
    )


def test_the_plain_report_prints_both_store_and_journal_evidence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A snapshot with both blocks reports each one."""
    note = journal_note(
        Record(
            "supervisor",
            "request_admitted",
            {"cache_path": "cached", "cache_backend": "sccache"},
            OBSERVED_AT,
        ),
    )

    output = _report(note, with_diagnostics=True, capsys=capsys)

    assert "store rows: total=0" in output, "the store block is printed"
    assert "observer: requests=1" in output, "the journal block is printed"
    assert "label loss: no diagnostics recorded" not in output, (
        "a present store block is not described as absent"
    )


def test_a_history_without_generation_evidence_is_named(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An empty history reports the dimension as unavailable, not as zero.

    The snapshot this test builds holds no outcome rows, so its generation map
    is empty. Printing that as a measured breakdown would claim the dimension
    was measured and found empty, which is a different statement from the
    evidence never having existed.
    """
    output = _report(journal_note(), with_diagnostics=True, capsys=capsys)

    assert "by generation: unavailable" in output, (
        "an unmeasured dimension is named rather than printed as a zero"
    )


def test_a_store_holding_generation_evidence_prints_its_buckets(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A populated generation map is printed as measured counts.

    The report must read the evidence the snapshot carries. Printing the
    unavailable sentence for a snapshot that holds a populated map would hide
    the very measurement the dimension was added to supply.
    """
    first = "a" * 64
    history = decode_history(
        DecodedStore(
            StoreHeader("fixed-salt", OBSERVED_AT),
            tuple(
                dc.replace(attributed_record(outcome="hit"), generation_digest=first)
                for _ in range(2)
            ),
        )
    )
    snapshot = build_snapshot(
        history,
        now=OBSERVED_AT,
        history_loss=HistoryLoss.active_file_only(),
        journal=journal_note(),
    )

    dataset_report.report_snapshot(snapshot, Path("out.jsonl"))
    output = capsys.readouterr().out

    assert "by generation: unavailable" not in output, (
        "a measured map is never reported as unavailable"
    )
    assert first in output, "the generation digest the rows carry is printed"
    assert "by generation: " in output, "the measured breakdown is printed"
