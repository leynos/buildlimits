"""Shared fixtures for rustc query example and property tests."""

import typing as typ

from hypothesis import strategies as st

if typ.TYPE_CHECKING:
    from pathlib import Path


_CRATE_TYPES = ("bin", "rlib", "dylib", "cdylib", "staticlib", "proc-macro")
_DIRECT_PRINT_KINDS = (
    "cfg",
    "sysroot",
    "target-list",
    "target-cpus",
    "target-features",
)
_CARGO_EXTRA_PRINT_KINDS = ("sysroot", "split-debuginfo", "crate-name", "cfg")
_CARGO_TARGETS = (
    "x86_64-unknown-linux-gnu",
    "aarch64-apple-darwin",
    "wasm32-wasip2",
)


@st.composite
def _safe_query_arguments(draw: st.DrawFn) -> list[str]:
    """Generate accepted shared queries with varied valid token layouts.

    Returns
    -------
    st.SearchStrategy[list[str]]
        Strategy that yields one complete, safe rustc query argument vector.
    """
    query_kind = draw(st.sampled_from(("version", "print", "cargo")))
    if query_kind == "version":
        version = draw(st.sampled_from(("-V", "--version", "-vV")))
        if not draw(st.booleans()):
            return [version]
        return [version, "--verbose"] if draw(st.booleans()) else ["--verbose", version]
    if query_kind == "print":
        kinds = draw(
            st.lists(st.sampled_from(_DIRECT_PRINT_KINDS), min_size=1, max_size=5)
        )
        return [f"--print={kind}" for kind in kinds]

    crate_types = draw(st.sets(st.sampled_from(_CRATE_TYPES), min_size=1))
    extra_prints = draw(st.sets(st.sampled_from(_CARGO_EXTRA_PRINT_KINDS)))
    groups: list[tuple[str, ...]] = [("-",), ("--crate-name", "___")]
    groups.extend(("--crate-type", crate_type) for crate_type in sorted(crate_types))
    groups.append(("--print=file-names",))
    groups.extend((f"--print={kind}",) for kind in sorted(extra_prints))
    if draw(st.booleans()):
        groups.append(("-W" + "warnings",))

    target = draw(st.one_of(st.none(), st.sampled_from(_CARGO_TARGETS)))
    if target is not None:
        groups.append(
            ("--target", target) if draw(st.booleans()) else (f"--target={target}",)
        )

    ordered_groups = draw(st.permutations(groups))
    return [argument for group in ordered_groups for argument in group]


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
