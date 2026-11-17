"""Validation contracts for the pinned parameter file."""

from pathlib import Path

import pytest
from predict_test_helpers import write_json

from buildlimits.predict.config import CONFIG_VERSION, load_config


def _changed(tmp_path: Path, **changes: object) -> Path:
    """Write the packaged parameters with ``changes`` applied."""
    document = load_config().as_dict()
    document.update(changes)
    return write_json(tmp_path / "config.json", document)


def _without(tmp_path: Path, name: str) -> Path:
    """Write the packaged parameters with one key removed."""
    document = load_config().as_dict()
    del document[name]
    return write_json(tmp_path / "config.json", document)


def test_a_document_that_is_not_an_object_is_a_type_error(tmp_path: Path) -> None:
    """A JSON array is not a parameter file."""
    with pytest.raises(TypeError, match="must hold one JSON object"):
        load_config(write_json(tmp_path / "config.json", [CONFIG_VERSION]))


def test_a_non_integer_version_is_a_type_error(tmp_path: Path) -> None:
    """The version must be a JSON integer, not text or a boolean."""
    with pytest.raises(TypeError, match="integer config_version"):
        load_config(_changed(tmp_path, config_version="one"))


def test_the_version_is_checked_before_the_key_set(tmp_path: Path) -> None:
    """A file from a later build reports its version, not its odd keys."""
    document = load_config().as_dict()
    document["config_version"] = CONFIG_VERSION + 1
    document["a_key_from_the_future"] = 1

    with pytest.raises(ValueError, match="unsupported version") as raised:
        load_config(write_json(tmp_path / "config.json", document))

    assert "a_key_from_the_future" not in str(raised.value), "keys are not listed"


def test_a_missing_key_is_rejected(tmp_path: Path) -> None:
    """Every parameter is required, so a short file cannot run."""
    with pytest.raises(ValueError, match="missing"):
        load_config(_without(tmp_path, "block_size"))


def test_an_unknown_key_is_rejected(tmp_path: Path) -> None:
    """An extra key is a corrupt file, not an extension point."""
    with pytest.raises(ValueError, match="unknown"):
        load_config(_changed(tmp_path, surprise_option=1))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("beta_window", 8.5),
        ("beta_window", True),
        ("cold_start", "high"),
        ("ewma_alpha", None),
        ("log_loss_clip", [0.1]),
    ],
)
def test_a_value_of_the_wrong_type_is_a_type_error(
    tmp_path: Path, name: str, value: object
) -> None:
    """Counts stay integral and rates stay numeric; neither coerces silently."""
    with pytest.raises(TypeError, match=name):
        load_config(_changed(tmp_path, **{name: value}))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ewma_alpha", 1.5),
        ("ewma_alpha", -0.1),
        ("block_size", 0),
        ("bootstrap_alpha", 2.0),
        ("cold_start", 2.0),
    ],
)
def test_a_value_outside_its_range_is_rejected(
    tmp_path: Path, name: str, value: float
) -> None:
    """A parameter outside its meaningful range is a corrupt file."""
    with pytest.raises(ValueError, match="within"):
        load_config(_changed(tmp_path, **{name: value}))


def test_a_clip_too_small_to_change_one_is_rejected(tmp_path: Path) -> None:
    """A clip whose complement rounds back to one leaves the loss undefined.

    ``1.0 - 2**-54`` is exactly ``1.0``, so a certain miss would take the
    logarithm of zero; ``2**-53`` still changes the value and is accepted.
    """
    with pytest.raises(ValueError, match="above"):
        load_config(_changed(tmp_path, log_loss_clip=2.0**-54))

    accepted = load_config(_changed(tmp_path, log_loss_clip=2.0**-53))

    assert 1.0 - accepted.log_loss_clip < 1.0, "the upper tail is clipped"


def test_a_whole_number_fills_a_fractional_field(tmp_path: Path) -> None:
    """JSON has one number type, so ``0`` is a valid rate and becomes a float."""
    config = load_config(_changed(tmp_path, cold_start=0))

    assert config.cold_start == pytest.approx(0.0), "the rate is read as given"
    assert isinstance(config.cold_start, float), "fractional fields stay floats"
