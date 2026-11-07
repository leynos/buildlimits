"""Strip a caller's own cache layer from a requested compiler invocation.

The supervisor composes the cache itself, from ``cache_mode``. A caller that
wrapped the compiler before asking -- Cargo's ``RUSTC_WORKSPACE_WRAPPER`` is
the usual way -- would otherwise have its own client nested inside the
configured one, opening a second daemon's bookkeeping in a group that accounts
for one compile.

Both supported caches are stripped, so switching ``cache_mode`` never leaves
the other one nested. A workspace wrapper that is not a cache stays in place.
"""

from pathlib import Path

#: Executables a caller may already have wrapped the compiler in.
CALLER_CACHE_LAYERS = frozenset({"sccache", "kache"})


def compiler_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
    """Remove leading cache layers from a compiler argument vector.

    Parameters
    ----------
    argv : tuple[str, ...]
        The requested argument vector.

    Returns
    -------
    tuple[str, ...]
        The vector without leading cache layers. The compiler itself is never
        stripped, even when it is oddly named after a cache.

    Examples
    --------
    >>> compiler_argv(("/b/sccache", "/t/rustc", "-vV"))
    ('/t/rustc', '-vV')
    >>> compiler_argv(("/b/kache", "/t/rustc", "-vV"))
    ('/t/rustc', '-vV')
    >>> compiler_argv(("/t/clippy-driver", "/t/rustc", "-vV"))
    ('/t/clippy-driver', '/t/rustc', '-vV')
    """
    while len(argv) > 1 and Path(argv[0]).name in CALLER_CACHE_LAYERS:
        argv = argv[1:]
    return argv
