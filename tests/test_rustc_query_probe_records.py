"""Tests for reading completed rustc timing-shim records."""

import json
from pathlib import Path

from rustc_query_probe_records import timing_entries


def test_timing_entries_ignores_unterminated_tail(tmp_path: Path) -> None:
    """Keep complete records when a concurrent shim append is unfinished."""
    record = {
        "argv": ["rustc", "-vV"],
        "started": 1.0,
        "ended": 1.1,
        "status": 0,
        "workspace_wrapper": None,
    }
    path = tmp_path / "timings.jsonl"
    completed_line = json.dumps(record) + "\n"
    cases = [
        (completed_line + '{"argv":["rustc","--print', [record]),
        (completed_line + json.dumps(record), [record]),
        (json.dumps(record), []),
    ]
    for content, expected in cases:
        path.write_text(content, encoding="utf-8")
        assert timing_entries(path) == expected, (
            "only newline-terminated records are complete"
        )
