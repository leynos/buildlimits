"""The model directory every TOML-configuration test reads.

The adapter's tests write a model and a ``configurations.toml`` into a scratch
directory and read them back. Two modules need that layout — the adapter's own
tests and the exclusion tests, which live apart so each module is a coherent
subject — so it is defined once here. Importing it from a ``test_*.py`` module
instead would couple two test modules and make pytest collect a module whose
helpers are not tests.
"""

import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

MODEL = """\
action Init:  # maps: buildlimits.admission.Admission.submit
  n = 0
"""
"""The source a model directory holds in these tests."""


def model_directory(directory: Path, configurations: str = "") -> Path:
    """Write a model directory and return it.

    Parameters
    ----------
    directory : pathlib.Path
        The scratch directory pytest supplied, which is written into.
    configurations : str
        The ``configurations.toml`` contents, or nothing for no file.

    Returns
    -------
    pathlib.Path
        The directory, holding the model and, when it was given, the
        configuration file.
    """
    (directory / "admission.fizz").write_text(MODEL, encoding="utf-8")
    if configurations:
        (directory / "configurations.toml").write_text(configurations, encoding="utf-8")
    return directory
