"""Configuration contract for the inert outcome-recording plateau."""

from pathlib import Path

import pytest

from buildlimits import config


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ('"off"', "off"),
        ('"record"', "record"),
        ('"enabled"', None),
        ("true", None),
        ("1", None),
    ],
)
def test_cache_outcomes_toml_values(
    tmp_path: Path, value: str, expected: str | None
) -> None:
    """Known modes round-trip and unsupported TOML values fail closed."""
    path = tmp_path / "supervisor.toml"
    path.write_text(f"cache_outcomes = {value}\n", encoding="utf-8")

    if expected is None:
        with pytest.raises(config.ConfigError, match="cache_outcomes"):
            config.load(path)
    else:
        assert config.load(path).cache_outcomes == expected, "the mode is retained"


def test_recording_requires_the_managed_sccache_mode() -> None:
    """Recording cannot be enabled when the supervisor compiles directly."""
    with pytest.raises(config.ConfigError, match='requires cache_mode="sccache"'):
        config.validate(
            config.from_mapping({"cache_mode": "direct", "cache_outcomes": "record"})
        )
