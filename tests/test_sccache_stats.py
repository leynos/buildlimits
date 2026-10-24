"""Tests for stable replay fixture serialization and comparison."""

from pathlib import Path

import pytest
import sccache_stats

_WINDOW = {"name": "cold-miss", "truth": "miss"}
_WINDOW_JSONL = '{"name":"cold-miss","truth":"miss"}\n'


def test_matching_windows_leave_the_fixture_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Checking stable readings never rewrites the committed fixture."""
    fixture = tmp_path / "windows.jsonl"
    fixture.write_text(_WINDOW_JSONL, encoding="utf-8")
    monkeypatch.setattr(sccache_stats, "_WINDOWS_FIXTURE", fixture)

    sccache_stats._assert_windows_match([_WINDOW])

    assert fixture.read_text(encoding="utf-8") == _WINDOW_JSONL, (
        "fixture verification is read-only"
    )


def test_different_windows_request_explicit_regeneration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A changed observation fails without modifying the committed fixture."""
    fixture = tmp_path / "windows.jsonl"
    fixture.write_text(_WINDOW_JSONL, encoding="utf-8")
    monkeypatch.setattr(sccache_stats, "_WINDOWS_FIXTURE", fixture)

    with pytest.raises(
        AssertionError, match="BUILD_LIMITS_SCCACHE_REGENERATE_FIXTURE=1"
    ):
        sccache_stats._assert_windows_match([{"name": "changed", "truth": "miss"}])

    assert fixture.read_text(encoding="utf-8") == _WINDOW_JSONL, (
        "a failed comparison leaves the reviewed fixture untouched"
    )


def test_fixture_regeneration_replaces_the_target_atomically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The explicit regeneration path writes the new stable JSONL payload."""
    fixture = tmp_path / "windows.jsonl"
    fixture.write_text("old fixture\n", encoding="utf-8")
    monkeypatch.setattr(sccache_stats, "_WINDOWS_FIXTURE", fixture)

    sccache_stats._write_windows([_WINDOW])

    assert fixture.read_text(encoding="utf-8") == _WINDOW_JSONL, (
        "regeneration serializes replay windows deterministically"
    )
    assert not fixture.with_suffix(".jsonl.tmp").exists(), (
        "the atomic temporary file is removed after replacement"
    )
