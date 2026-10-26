"""Shared fixtures for rustc query example and property tests."""

import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path


_CRATE_TYPES = ("bin", "rlib", "dylib", "cdylib", "staticlib", "proc-macro")


def cargo_probe(target: str | None = None) -> list[str]:
    """Build Cargo's pinned stdin target-information query.

    Parameters
    ----------
    target : str | None
        Optional target triple.

    Returns
    -------
    list[str]
        Cargo's allow-listed query arguments.
    """
    arguments = ["-", "--crate-name", "___", "--print=file-names"]
    if target is not None:
        arguments.extend(["--target", target])
    for crate_type in _CRATE_TYPES:
        arguments.extend(["--crate-type", crate_type])
    arguments.extend([
        "--print=sysroot",
        "--print=split-debuginfo",
        "--print=crate-name",
        "--print=cfg",
        "-W" + "warnings",
    ])
    return arguments


def temporary_executable(path: Path, source: str = "#!/bin/sh\nexit 0\n") -> Path:
    """Create a hermetic executable path for wrapper identity tests.

    Parameters
    ----------
    path : Path
        Executable fixture location.
    source : str
        Script contents. The default is a successful no-op executable.

    Returns
    -------
    Path
        The executable fixture path.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    path.chmod(0o755)
    return path


def path_spellings(path: Path, cwd: Path) -> list[str]:
    """Return absolute, relative, dot-segment and trailing-slash spellings.

    Parameters
    ----------
    path : Path
        Executable path to render in different forms.
    cwd : Path
        Working directory used for relative spellings.

    Returns
    -------
    list[str]
        Equivalent absolute, relative, and dot-segment path strings.
    """
    relative_path = path.relative_to(cwd)
    relative = relative_path.as_posix()
    parent = relative_path.parent.as_posix()
    name = relative_path.name
    return [
        str(path),
        relative,
        f"./{relative}",
        f"./{parent}/../{parent}/{name}/",
    ]
