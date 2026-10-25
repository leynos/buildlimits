"""Keep a real toolchain compiler's own library directory on the loader path.

``rustc-dev`` installs a second copy of ``librustc_driver`` in
``<root>/lib/rustlib/<triple>/lib``. Cargo puts that directory on
``LD_LIBRARY_PATH`` for build scripts, so the loader binds the copy there, and
its ``RUNPATH`` of ``$ORIGIN/../lib`` then misses ``libLLVM``. The real
``rustc`` exits with status 127 and no output, and ``rustversion`` reports
``unexpected output from `rustc --version`: ""``.

The rustup proxy is immune because it prepends ``<root>/lib`` before it runs
the real compiler (``Toolchain::set_ldpath``, rustup 1.29.1 ``src/toolchain.rs``,
through ``env_var::insert_path``). :func:`toolchain_environment` does the same
for an adapter that runs a real ``rustc`` or ``rustdoc`` itself. It is
defensive, not causal: the adapter does not set the poisoned value, and a
build script that Cargo runs without this adapter is outside its reach. See
``docs/adr-002-toolchain-library-path.md``.

It differs from rustup in one deliberate way. rustup also sets the variable
when it is unset; here an unset or empty variable stays as it is, because
nothing can shadow ``RUNPATH`` then, and a request environment is otherwise
forwarded verbatim.

The helper only inspects the filesystem. It never runs the compiler, which is
what fails under the poisoned path, and it never changes ``argv``.
"""

import os
import typing as typ
from pathlib import Path

from buildlimits.groups import compiler_argv

if typ.TYPE_CHECKING:
    import collections.abc as cabc

LIBRARY_PATH_VARIABLE = "LD_LIBRARY_PATH"
REAL_COMPILERS = frozenset({"rustc", "rustdoc"})
DRIVER_PATTERN = "librustc_driver-*.so"


def toolchain_environment(
    argv: cabc.Sequence[str], env: cabc.Mapping[str, str], cwd: str
) -> dict[str, str]:
    """Return the environment to run a compiler in.

    The result is ``env`` with ``<root>/lib`` prefixed to a non-empty
    ``LD_LIBRARY_PATH`` when ``argv`` runs a real toolchain ``rustc`` or
    ``rustdoc`` rooted at ``<root>`` and the path does not already name that
    directory. Everything else is returned as an unchanged copy: the rustup
    proxy, ``clippy-driver``, an unresolvable name, a layout without a
    compiler driver, and an unset or empty variable.

    Parameters
    ----------
    argv : Sequence[str]
        The compiler's argument vector; a leading ``sccache`` layer is ignored.
    env : Mapping[str, str]
        The caller's environment, which supplies ``PATH`` and the loader path.
    cwd : str
        The directory that relative executables and path entries resolve in.

    Returns
    -------
    dict[str, str]
        A new mapping; ``env`` is never modified.

    Notes
    -----
    This function does not raise for filesystem trouble. It fails open, and
    each kind of trouble is read as the answer that does the least harm:

    - Failed toolchain recognition (the command, its layout or the driver
      cannot be found or read) returns the environment unchanged.
    - A ``PATH`` directory that cannot be searched is skipped; the search goes
      on, and a later match is repaired as usual.
    - A ``LD_LIBRARY_PATH`` entry that does not open does not count as
      ``<root>/lib``, so a recognized toolchain still gets ``<root>/lib``
      prefixed unless another entry identifies it.

    The helper only repairs a lookup that is already broken, so raising here
    would turn a harmless no-op into a stopped build; the compiler's own error
    is the diagnostic if the lookup really is broken.

    Examples
    --------
    >>> toolchain_environment(["rustc"], {"LD_LIBRARY_PATH": "/x"}, "/")
    {'LD_LIBRARY_PATH': '/x'}
    """
    result = dict(env)
    library_dir = toolchain_library_dir(argv, env, cwd)
    current = env.get(LIBRARY_PATH_VARIABLE)
    if library_dir is None or not current:
        return result
    if not _names(current, library_dir, cwd):
        result[LIBRARY_PATH_VARIABLE] = f"{library_dir}:{current}"
    return result


def toolchain_library_dir(
    argv: cabc.Sequence[str], env: cabc.Mapping[str, str], cwd: str
) -> Path | None:
    """Find the ``lib`` directory of the real toolchain that ``argv`` runs.

    Parameters
    ----------
    argv : Sequence[str]
        The compiler's argument vector.
    env : Mapping[str, str]
        The caller's environment; only ``PATH`` is read.
    cwd : str
        The directory that a relative executable resolves in.

    Returns
    -------
    Path | None
        ``<root>/lib`` for ``<root>/bin/rustc`` or ``<root>/bin/rustdoc`` with
        a compiler driver beside it, otherwise ``None``. Symbolic links are
        followed, so the rustup proxy, which resolves to ``rustup``, is
        never recognized.
    """
    compiler = compiler_argv(tuple(argv))
    found = locate_executable(compiler[0], env, cwd) if compiler else None
    if found is None:
        return None
    binary = Path(found).resolve()
    library_dir = binary.parent.parent / "lib"
    if binary.name not in REAL_COMPILERS or binary.parent.name != "bin":
        return None
    if ":" in str(library_dir) or not _has_driver(library_dir):
        return None
    return library_dir


def locate_executable(name: str, env: cabc.Mapping[str, str], cwd: str) -> str | None:
    """Resolve an executable as ``execvpe`` would, in the caller's directory.

    This lookup is shared by loader-path repair and rustc query identity
    checks. It deliberately returns the joined path for explicit paths, even
    when it does not exist, so callers retain their own failure policy.

    Parameters
    ----------
    name : str
        Executable name or explicit relative or absolute path.
    env : Mapping[str, str]
        Environment supplying ``PATH`` entries.
    cwd : str
        Working directory for relative paths.

    Returns
    -------
    str | None
        The first executable file, or ``None`` when there is none.
    """
    if "/" in name:
        return str(Path(cwd, name))
    for directory in os.get_exec_path(dict(env)):
        candidate = Path(cwd, directory, name)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def _has_driver(library_dir: Path) -> bool:
    """Report whether a directory holds the compiler driver.

    An unreadable or missing directory holds none; see the fail-open note on
    :func:`toolchain_environment`.

    Returns
    -------
    bool
        ``True`` when a ``librustc_driver-*.so`` file is present.
    """
    try:
        return any(library_dir.glob(DRIVER_PATTERN))
    except OSError:
        return False


def _names(search_path: str, library_dir: Path, cwd: str) -> bool:
    """Report whether a search path already lists a directory, by file identity.

    rustup skips its prepend when the directory appears anywhere in the
    variable, because the loader finds ``libLLVM`` wherever it is listed.
    Empty entries mean the working directory and are ignored.

    Returns
    -------
    bool
        ``True`` when any entry resolves to ``library_dir``.
    """
    return any(
        _is_directory(Path(cwd, entry), library_dir)
        for entry in search_path.split(":")
        if entry
    )


def _is_directory(entry: Path, library_dir: Path) -> bool:
    """Report whether a search-path entry opens as the given directory.

    The kernel looks the entry up, so a missing component before ``..`` fails
    here as it does for the loader. Collapsing the entry lexically would count
    ``<root>/lib/missing/..`` as present although the loader cannot use it.

    Returns
    -------
    bool
        ``True`` when the entry exists and is ``library_dir``.
    """
    try:
        return entry.samefile(library_dir)
    except OSError:
        return False
