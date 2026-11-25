"""A snapshot of history that spans several record domains.

The store's salt rotates, so one retained history can hold rows keyed under
more than one domain. The snapshot records every domain it read, and the
loader must hand them back unchanged; a single-domain snapshot keeps the
earlier document shape, which existing snapshots pin.
"""

import datetime as dt
import typing as typ

from outcome_test_helpers import attributed_record
from predict_test_helpers import ACTIVE_FILE_ONLY, write_json

from buildlimits.outcome_record_format import DecodedStore, StoreHeader
from buildlimits.predict.dataset import build_snapshot
from buildlimits.predict.journal_provenance import JournalUnavailable
from buildlimits.predict.records import decode_history
from buildlimits.predict.snapshot_codec import snapshot_to_dict
from buildlimits.predict.snapshot_store import load_snapshot

if typ.TYPE_CHECKING:
    from pathlib import Path

NOW = dt.datetime(2026, 11, 17, 12, tzinfo=dt.UTC)


def test_a_multi_domain_history_records_every_domain(tmp_path: Path) -> None:
    """Every domain is written and read back, oldest first."""
    decoded = DecodedStore(
        StoreHeader("salt-b", NOW),
        (attributed_record(observed_at=NOW, outcome="hit"),),
        ("salt-a", "salt-b"),
    )
    snapshot = build_snapshot(
        decode_history(decoded),
        now=NOW,
        history_loss=ACTIVE_FILE_ONLY,
        journal=JournalUnavailable("none"),
    )

    document = snapshot_to_dict(snapshot)

    source = typ.cast("dict[str, object]", document["source"])
    assert source["salt_ids"] == ["salt-a", "salt-b"], (
        "the document names each domain it spans"
    )
    loaded = load_snapshot(write_json(tmp_path / "history.jsonl", document))
    assert loaded.source.salt_ids == ("salt-a", "salt-b"), "the loader restores them"


def test_a_single_domain_history_omits_the_field() -> None:
    """A history read from one file keeps the earlier document shape."""
    decoded = DecodedStore(
        StoreHeader("salt-a", NOW),
        (attributed_record(observed_at=NOW, outcome="hit"),),
    )

    document = snapshot_to_dict(
        build_snapshot(
            decode_history(decoded),
            now=NOW,
            history_loss=ACTIVE_FILE_ONLY,
            journal=JournalUnavailable("none"),
        )
    )

    source = typ.cast("dict[str, object]", document["source"])
    assert "salt_ids" not in source, "no empty list is written"
