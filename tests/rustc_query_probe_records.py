"""Read and classify rustc probe records emitted by the timing shim."""

import json
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    from pathlib import Path


class ProbeRecord(typ.TypedDict):
    """One complete JSON-line record from the timing shim."""

    argv: list[str]
    started: float
    ended: float
    status: int
    workspace_wrapper: str | None
    error: typ.NotRequired[str]


def timing_entries(path: Path) -> list[ProbeRecord]:
    """Read completed JSON-lines records from the timing shim.

    Parameters
    ----------
    path : Path
        Timing shim log to read.

    Returns
    -------
    list[ProbeRecord]
        Timing records written by completed shim invocations.
    """
    if not path.exists():
        return []
    return [
        typ.cast("ProbeRecord", json.loads(line))
        for line in path.read_text().splitlines()
    ]


def is_version_query(entry: cabc.Mapping[str, object]) -> bool:
    """Recognize the version query shapes used in host probe logs.

    Parameters
    ----------
    entry : Mapping[str, object]
        Completed timing record to inspect.

    Returns
    -------
    bool
        Whether any argument is a supported version query flag.
    """
    argv = typ.cast("list[str]", entry["argv"])
    return any(argument in {"-vV", "-V", "--version"} for argument in argv)


def is_target_info_query(entry: cabc.Mapping[str, object]) -> bool:
    """Recognize Cargo's stdin target-information query in shim logs.

    Parameters
    ----------
    entry : Mapping[str, object]
        Completed timing record to inspect.

    Returns
    -------
    bool
        Whether the arguments match Cargo's stdin probe shape.
    """
    argv = typ.cast("list[str]", entry["argv"])
    return (
        "-" in argv
        and "--crate-name" in argv
        and "___" in argv
        and "--print=file-names" in argv
    )
