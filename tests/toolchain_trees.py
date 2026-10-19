"""Fake Rust toolchain layouts for tests that must never run a real compiler.

A rustup toolchain keeps its binaries in ``<root>/bin`` and its compiler driver
in ``<root>/lib``. A toolchain that ships ``rustc-dev`` also keeps a second copy
of the driver in ``<root>/lib/rustlib/<triple>/lib``. These builders create that
shape from empty files, which is all the filesystem-only recognition needs.

Examples
--------
>>> import tempfile
>>> from pathlib import Path
>>> with tempfile.TemporaryDirectory() as scratch:
...     tree = make_toolchain(Path(scratch))
...     (tree.rustc.name, tree.rustlib.name)
('rustc', 'lib')
"""

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path

TRIPLE = "x86_64-unknown-linux-gnu"
DRIVER = "librustc_driver-0123456789abcdef.so"


@dc.dataclass(frozen=True, slots=True)
class ToolchainTree:
    """The paths of one fake toolchain.

    Attributes
    ----------
    root : Path
        The toolchain directory, whose ``lib`` rustup prepends to the loader path.
    rustc : Path
        The real ``rustc`` binary.
    rustdoc : Path
        The real ``rustdoc`` binary.
    lib : Path
        ``<root>/lib``, beside the compiler driver.
    rustlib : Path
        The target library directory that ``rustc-dev`` duplicates the driver into.
    """

    root: Path
    rustc: Path
    rustdoc: Path
    lib: Path
    rustlib: Path


def make_toolchain(
    base: Path, name: str = "nightly", *, has_driver: bool = True
) -> ToolchainTree:
    """Create a toolchain tree with a duplicate driver in its rustlib directory.

    Parameters
    ----------
    base : Path
        An empty directory to build under, normally ``tmp_path``.
    name : str
        The toolchain directory name.
    has_driver : bool
        Whether ``<root>/lib`` holds the compiler driver. A tree without one is
        not a toolchain that this adapter recognizes.

    Returns
    -------
    ToolchainTree
        The created paths.
    """
    root = base / "toolchains" / name
    lib = root / "lib"
    rustlib = lib / "rustlib" / TRIPLE / "lib"
    rustlib.mkdir(parents=True)
    (root / "bin").mkdir()
    if has_driver:
        (lib / DRIVER).touch()
        (rustlib / DRIVER).touch()
    for tool in ("rustc", "rustdoc", "clippy-driver"):
        (root / "bin" / tool).touch(mode=0o755)
    return ToolchainTree(
        root, root / "bin" / "rustc", root / "bin" / "rustdoc", lib, rustlib
    )


def make_shim(base: Path) -> Path:
    """Create a rustup-style ``rustc`` proxy: a symlink to a ``rustup`` binary.

    Parameters
    ----------
    base : Path
        An empty directory to build under, normally ``tmp_path``.

    Returns
    -------
    Path
        The proxy path, which resolves to a file named ``rustup``.
    """
    bin_dir = base / "cargo" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "rustup").touch(mode=0o755)
    shim = bin_dir / "rustc"
    shim.symlink_to("rustup")
    return shim
