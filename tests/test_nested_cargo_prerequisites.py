"""Real Cargo fixtures must verify production admission before creating services."""

from pathlib import Path

import pytest
import test_nested_cargo as nested


@pytest.mark.parametrize("wrapper", ["/usr/bin/rustc", "/fixture/build-limits-rustc"])
def test_missing_managed_admission_skips_before_fixture_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, wrapper: str
) -> None:
    """An unrelated wrapper or missing admission socket cannot launch the fixture."""

    def which(program: str) -> str:
        """Resolve a Cargo executable and the requested wrapper without spawning."""
        return "/fixture/cargo" if program == "cargo" else program

    def forbid(*_args: object, **_kwargs: object) -> None:
        """Catch fixture startup before admission prerequisites have been verified."""
        msg = "fixture started without managed admission"
        raise AssertionError(msg)

    monkeypatch.setenv("RUSTC_WRAPPER", wrapper)
    monkeypatch.setenv("BUILD_LIMITS_SOCKET", str(tmp_path / "absent.sock"))
    monkeypatch.setattr(nested.shutil, "which", which)
    monkeypatch.setattr(nested, "Harness", forbid)
    with pytest.raises(pytest.skip.Exception, match="admission"):
        nested.test_offline_nested_cargo_preserves_admission_and_recovers_baseline()
