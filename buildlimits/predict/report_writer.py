"""Write one report's files into a directory.

A report is a value: the deterministic document, the measured timings and the
rendered Markdown. Putting them on disk is an output adapter concern, so it
lives here and :mod:`buildlimits.predict.report` stays free of the filesystem
and of JSON.
"""

import json
import typing as typ

from buildlimits.predict.paths import same_file
from buildlimits.predict.report import (
    MARKDOWN,
    RESULT_FILES,
    RESULTS,
    TIMINGS,
    Report,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


def write_report(report: Report, directory: Path) -> None:
    """Write both result files and the timings beside them.

    The three destinations are checked for a symbolic or hard link between
    them first, so a directory that would make two artefacts one file is
    refused before anything is written; see
    :func:`_require_distinct_destinations`.
    """
    directory.mkdir(parents=True, exist_ok=True)
    _require_distinct_destinations(directory)
    _write_json(directory / RESULTS, report.document)
    _write_json(directory / TIMINGS, report.timings)
    (directory / MARKDOWN).write_text(report.markdown, encoding="utf-8")


def _require_distinct_destinations(directory: Path) -> None:
    """Refuse a report directory whose files alias each other.

    The three names are fixed, but a symbolic link or a hard link placed in
    the directory can still make two of them one file. The check runs before
    the first write, so a refused run leaves every artefact as it was.

    Raises
    ------
    ValueError
        If two destinations resolve to, or share an inode with, one another.
    """
    destinations = [directory / name for name in RESULT_FILES]
    for index, first in enumerate(destinations):
        for second in destinations[index + 1 :]:
            if same_file(first, second):
                msg = (
                    f"report destinations {first.name} and {second.name} are "
                    "the same file; choose a directory without a link "
                    "between them"
                )
                raise ValueError(msg)


def _write_json(path: Path, document: cabc.Mapping[str, object]) -> None:
    """Write one deterministic JSON document."""
    path.write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
