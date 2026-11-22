"""A damaged retention record must not raise the floor past a gap.

The floor is the only reason an absent sequence stops being reported as a gap,
so it may advance only over history retention removed. A run that cannot read
the record must not derive a higher floor from the survivors: writing that
baseline back would replace the damage with a floor above the missing history,
and every later scan would trust it.
"""

import json
from pathlib import Path

from outcome_archive_helpers import NOW, layout, policy, records, run, write
from segment_test_helpers import write_private

from buildlimits.outcome_segments import archive_name, sidecar_name


def test_an_invalid_floor_does_not_hide_a_missing_sequence(tmp_path: Path) -> None:
    """The gap below the first survivor outlives a damaged record.

    The recorded floor is one and the store's first sequence is missing. A
    baseline derived from the survivors would be two, so this run would drop
    the gap and the next firing would inherit the raised floor. The damaged
    record instead keeps the lowest sequence the store could have started at,
    and the gap is still reported.
    """
    store = tmp_path / "cache-outcomes.jsonl"
    write(store, records(NOW, 3), policy())
    run(tmp_path, policy())
    store_layout = layout(tmp_path)
    (store_layout.archive_dir / archive_name(1)).unlink()
    (store_layout.archive_dir / sidecar_name(1)).unlink()
    write_private(store_layout.retention_file, b'{"floor_sequence": "one"}')

    report = run(tmp_path, policy())

    assert report.status == "ok", "damaged advisory state is not a run failure"
    assert report.gaps == (1,), "the missing sequence is still reported"
    state = json.loads(store_layout.retention_file.read_text(encoding="utf-8"))
    assert state["floor_sequence"] == 1, "the floor is not silently raised"
