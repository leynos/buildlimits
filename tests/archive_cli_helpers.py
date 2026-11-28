"""Launchers and settings shared by the archive command's process tests.

The command is exercised the way a systemd timer runs it: the installed
console script in a fresh process. These helpers keep the entry point, the
configuration it reads and the repository root in one place, so every
process-level test starts the same surface from the same directory.
"""

import json
import sys
import typing as typ
from pathlib import Path

from segment_test_helpers import document

from buildlimits.outcome_segments import SegmentLayout, sealed_meta_name, segment_name
from buildlimits.outcome_store_documents import sealed_metadata
from buildlimits.utc_text import utc_now

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_record_format import StoreRecord

# The installed entry point is the surface a systemd unit names, so tests run
# it rather than a test-only wrapper around ``main``.
SCRIPT = Path(sys.executable).with_name("build-limits-outcome-archive")

# A ``python -m`` launch resolves the package from this checkout even when the
# console script is not on the unit's PATH.
REPO_ROOT = Path(__file__).parents[1]

__all__ = [
    "REPO_ROOT",
    "SCRIPT",
    "command_arguments",
    "sealed_measured_source",
    "sealed_source",
    "write_config",
]


def sealed_source(store: Path, sequence: int, data: bytes) -> Path:
    """Write one sealed source beside ``store``, as the writer would close it.

    Parameters
    ----------
    store : Path
        The ``cache-outcomes.jsonl`` path the source belongs to.
    sequence : int
        The sequence number the source's name carries.
    data : bytes
        The JSON Lines document the writer closed.

    Returns
    -------
    Path
        The source path.
    """
    directory = SegmentLayout.beside(store).sealed_dir
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / segment_name(sequence)
    path.write_bytes(data)
    path.chmod(0o600)
    return path


def sealed_measured_source(
    store: Path,
    sequence: int,
    rows: cabc.Sequence[StoreRecord],
    *,
    salt_id: str = "salt-1",
) -> Path:
    """Write one sealed source together with the writer's metadata.

    The planner trusts the writer's ``.meta.json`` rather than reading a
    source's rows, so a fixture that needs a *measured* sealed source publishes
    both files. A source without the metadata is unmeasured: its bytes count
    towards a budget but its span and record count do not.

    Parameters
    ----------
    store : Path
        The ``cache-outcomes.jsonl`` path the source belongs to.
    sequence : int
        The sequence number the source's name carries.
    rows : Sequence[StoreRecord]
        The rows the writer closed into the source.
    salt_id : str
        The salt domain the document is encoded under.

    Returns
    -------
    Path
        The source path.
    """
    path = sealed_source(store, sequence, document(tuple(rows), salt_id))
    # The writer's own builder keeps the fixture's metadata identical to the
    # document a real seal publishes; only the planner's fields matter here.
    metadata = sealed_metadata(list(rows), sequence, salt_id, utc_now())
    (path.parent / sealed_meta_name(sequence)).write_text(json.dumps(metadata))
    return path


def write_config(
    path: Path,
    *,
    preset: int = 0,
    max_bytes: int = 15_000_000_000,
    max_records: int = 0,
) -> Path:
    """Write a configuration that enables recorded, archived outcomes.

    Parameters
    ----------
    path : Path
        The configuration file to write.
    preset : int
        The XZ preset the command should use.
    max_bytes : int
        The logical-byte budget the archiver retains.
    max_records : int
        The record cap, or zero for none.

    Returns
    -------
    Path
        The configuration path.
    """
    path.write_text(
        "\n".join([
            'cache_outcomes = "record"',
            'cache_mode = "sccache"',
            # The fixtures are dated, so a real age window would start evicting
            # them once the wall clock passed it. Zero disables age eviction.
            "cache_outcomes_max_age_days = 0",
            f"cache_outcomes_max_bytes = {max_bytes}",
            f"cache_outcomes_max_records = {max_records}",
            f"cache_outcomes_archive_preset = {preset}",
            "",
        ])
    )
    return path


def command_arguments(store: Path, config: Path) -> list[str]:
    """Return the options every archive command shares.

    Parameters
    ----------
    store : Path
        The ``cache-outcomes.jsonl`` path.
    config : Path
        The supervisor configuration path.

    Returns
    -------
    list[str]
        The store and configuration arguments.
    """
    return ["--store", str(store), "--config", str(config)]
