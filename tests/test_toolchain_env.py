"""Recognition and loader-path rules of :mod:`buildlimits.toolchain_env`.

Every case runs against fake toolchain trees under ``tmp_path``: the helper
only looks at the filesystem, so no compiler is ever executed.
"""

import os
import typing as typ
from pathlib import Path

import pytest
from hypothesis import event, example, given
from hypothesis import strategies as st
from toolchain_trees import ToolchainTree, make_shim, make_toolchain

from buildlimits.toolchain_env import (
    LIBRARY_PATH_VARIABLE,
    toolchain_environment,
    toolchain_library_dir,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

POISONED = "LD_LIBRARY_PATH"
CWD = "/"


def _env(path: str | None = None, **extra: str) -> dict[str, str]:
    """Build a caller environment with an optional loader path.

    Returns
    -------
    dict[str, str]
        The environment, without the variable when ``path`` is ``None``.
    """
    env = dict(extra)
    if path is not None:
        env[POISONED] = path
    return env


# --- recognition -------------------------------------------------------------


@pytest.mark.parametrize("tool", ["rustc", "rustdoc"])
def test_a_real_toolchain_binary_is_recognized(tmp_path: Path, tool: str) -> None:
    """``<root>/bin/rustc`` and ``rustdoc`` with a driver beside them qualify."""
    tree = make_toolchain(tmp_path)
    binary = tree.root / "bin" / tool
    assert toolchain_library_dir([str(binary), "-vV"], {}, CWD) == tree.lib, (
        "the toolchain's own lib directory is returned"
    )


def test_a_leading_sccache_layer_is_ignored(tmp_path: Path) -> None:
    """The compiler behind one or more ``sccache`` layers is the one inspected."""
    tree = make_toolchain(tmp_path)
    argv = ["/opt/bin/sccache", "sccache", str(tree.rustc), "-vV"]
    assert toolchain_library_dir(argv, {}, CWD) == tree.lib, "sccache is stripped"


def test_a_bare_name_is_found_on_the_callers_path(tmp_path: Path) -> None:
    """``rustc`` is looked up on the caller's ``PATH``, as the launch would."""
    tree = make_toolchain(tmp_path)
    env = {"PATH": f"/nonexistent:{tree.rustc.parent}"}
    assert toolchain_library_dir(["rustc", "-vV"], env, CWD) == tree.lib, (
        "PATH order and missing directories behave as in execvpe"
    )


def test_a_relative_path_resolves_against_the_working_directory(
    tmp_path: Path,
) -> None:
    """A path with a slash is taken relative to the request's directory."""
    tree = make_toolchain(tmp_path)
    relative = os.path.relpath(tree.rustc, tmp_path)
    assert toolchain_library_dir([relative], {}, str(tmp_path)) == tree.lib, (
        "the request's cwd, not the process's, anchors the lookup"
    )


def test_a_symlink_to_a_real_binary_is_recognized(tmp_path: Path) -> None:
    """The layout test follows links to the binary's real home."""
    tree = make_toolchain(tmp_path)
    link = tmp_path / "linked-rustc"
    link.symlink_to(tree.rustc)
    assert toolchain_library_dir([str(link)], {}, CWD) == tree.lib, (
        "the toolchain is found through the link"
    )


def test_the_rustup_proxy_is_not_recognized(tmp_path: Path) -> None:
    """The proxy resolves to ``rustup`` and prepends the lib itself."""
    shim = make_shim(tmp_path)
    assert toolchain_library_dir([str(shim)], {}, CWD) is None, (
        "a proxy is left to rustup"
    )


@pytest.mark.parametrize(
    "tool", ["clippy-driver", "cargo-clippy"], ids=["clippy", "clippy-wrapper"]
)
def test_other_tools_in_the_bin_directory_are_not_recognized(
    tmp_path: Path, tool: str
) -> None:
    """Only ``rustc`` and ``rustdoc`` qualify, even inside a real toolchain."""
    tree = make_toolchain(tmp_path)
    (tree.root / "bin" / tool).touch(mode=0o755)
    argv = [str(tree.root / "bin" / tool), str(tree.rustc), "-vV"]
    assert toolchain_library_dir(argv, {}, CWD) is None, (
        "clippy-driver keeps its own argument contract"
    )


def test_a_layout_without_the_driver_is_not_recognized(tmp_path: Path) -> None:
    """A ``bin/rustc`` with no compiler driver beside it is not a toolchain."""
    tree = make_toolchain(tmp_path, has_driver=False)
    assert toolchain_library_dir([str(tree.rustc)], {}, CWD) is None, (
        "the driver is what makes the layout a toolchain"
    )


def test_a_rustc_outside_a_bin_directory_is_not_recognized(tmp_path: Path) -> None:
    """The parent directory must be ``bin``."""
    tree = make_toolchain(tmp_path)
    stray = tree.root / "libexec"
    stray.mkdir()
    (stray / "rustc").touch(mode=0o755)
    assert toolchain_library_dir([str(stray / "rustc")], {}, CWD) is None, (
        "a binary elsewhere in the tree is not a toolchain compiler"
    )


@pytest.mark.parametrize(
    "argv",
    [[], ["/nonexistent/rustc"], ["no-such-compiler-here"], ["/usr/bin/true"]],
    ids=["empty", "missing-path", "missing-name", "other-tool"],
)
def test_an_unresolvable_or_unrelated_command_is_not_recognized(
    argv: list[str],
) -> None:
    """Nothing is guessed about a command that is absent or not a compiler."""
    assert toolchain_library_dir(argv, {"PATH": ""}, CWD) is None, "no toolchain"


# --- the loader path ---------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("{rustlib}", "{lib}:{rustlib}"),
        ("/a:{rustlib}:/b", "{lib}:/a:{rustlib}:/b"),
        (":{rustlib}:", "{lib}::{rustlib}:"),
        ("{rustlib}:{rustlib}", "{lib}:{rustlib}:{rustlib}"),
        ("/does/not/exist", "{lib}:/does/not/exist"),
        ("{lib}", "{lib}"),
        ("{rustlib}:{lib}", "{rustlib}:{lib}"),
        ("/a:{lib}/:/b", "/a:{lib}/:/b"),
        ("", ""),
        (None, None),
    ],
    ids=[
        "poisoned",
        "inside-other-entries",
        "empty-segments-kept",
        "duplicates-kept",
        "nonexistent-kept",
        "already-present",
        "present-after-poison",
        "present-with-trailing-slash",
        "empty",
        "unset",
    ],
)
def test_the_toolchain_lib_is_prefixed_once_to_a_non_empty_path(
    tmp_path: Path, path: str | None, expected: str | None
) -> None:
    """A non-empty path gains ``<root>/lib`` first unless it already names it."""
    tree = make_toolchain(tmp_path)
    fill = {"lib": str(tree.lib), "rustlib": str(tree.rustlib)}
    given_path = None if path is None else path.format(**fill)
    result = toolchain_environment([str(tree.rustc)], _env(given_path, A="1"), CWD)
    wanted = None if expected is None else expected.format(**fill)
    assert result.get(POISONED) == wanted, "only the loader path changes"
    assert result["A"] == "1", "other variables pass through"


def test_an_entry_that_links_to_the_lib_counts_as_present(tmp_path: Path) -> None:
    """Presence is judged by real path, as the loader would find the library."""
    tree = make_toolchain(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(tree.lib)
    path = f"{tree.rustlib}:{alias}"
    result = toolchain_environment([str(tree.rustc)], _env(path), CWD)
    assert result[POISONED] == path, "no second copy of the directory is added"


def test_a_relative_entry_is_resolved_in_the_request_directory(
    tmp_path: Path,
) -> None:
    """A relative entry names the lib when it resolves there from the cwd."""
    tree = make_toolchain(tmp_path)
    relative = os.path.relpath(tree.lib, tmp_path)
    result = toolchain_environment(
        [str(tree.rustc)], _env(f"{tree.rustlib}:{relative}"), str(tmp_path)
    )
    assert result[POISONED] == f"{tree.rustlib}:{relative}", "already listed"


def test_an_entry_that_cannot_be_looked_up_does_not_count_as_present(
    tmp_path: Path,
) -> None:
    """A missing component before ``..`` leaves the entry unusable to the loader."""
    tree = make_toolchain(tmp_path)
    path = f"{tree.rustlib}:{tree.lib}/missing/.."
    result = toolchain_environment([str(tree.rustc)], _env(path), CWD)
    assert result[POISONED] == f"{tree.lib}:{path}", (
        "lexically collapsing the entry would hide that the loader cannot open it"
    )


@pytest.mark.parametrize("argument", ["--version", "-vV"])
def test_the_input_mapping_and_argv_are_not_modified(
    tmp_path: Path, argument: str
) -> None:
    """The helper returns a new mapping and leaves its inputs as they were."""
    tree = make_toolchain(tmp_path)
    env = _env(str(tree.rustlib), PATH="/bin")
    before, argv = dict(env), [str(tree.rustc), argument]
    result = toolchain_environment(argv, env, CWD)
    assert env == before, "the caller's mapping is untouched"
    assert argv == [str(tree.rustc), argument], "argv is untouched"
    assert result is not env, "a new mapping is returned"


def test_a_path_that_cannot_hold_a_colon_is_left_alone(tmp_path: Path) -> None:
    """A toolchain directory containing ``:`` cannot be listed, so none is added."""
    tree = make_toolchain(tmp_path, "odd:name")
    result = toolchain_environment([str(tree.rustc)], _env("/x"), CWD)
    assert result[POISONED] == "/x", "the path cannot be expressed, so it is kept"


# --- generated paths ---------------------------------------------------------

SEGMENT_KINDS = ("lib", "rustlib", "other", "empty", "relative")


@pytest.fixture(scope="module")
def shared_tree(tmp_path_factory: pytest.TempPathFactory) -> ToolchainTree:
    """Build one toolchain for every generated example.

    Returns
    -------
    ToolchainTree
        A tree that outlives the function-scoped fixtures Hypothesis forbids.
    """
    return make_toolchain(tmp_path_factory.mktemp("generated"))


@pytest.fixture(scope="module")
def shared_shim(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build one rustup proxy for every generated example.

    Returns
    -------
    Path
        The proxy's path.
    """
    return make_shim(tmp_path_factory.mktemp("proxy"))


def _segments(tree: ToolchainTree, kinds: cabc.Sequence[str]) -> list[str]:
    """Spell each generated kind of search-path entry.

    Returns
    -------
    list[str]
        One entry per kind.
    """
    spelling = {
        "lib": str(tree.lib),
        "rustlib": str(tree.rustlib),
        "other": "/opt/other/lib",
        "empty": "",
        "relative": "vendor/lib",
    }
    return [spelling[kind] for kind in kinds]


KINDS = st.lists(st.sampled_from(SEGMENT_KINDS), max_size=8)


@given(kinds=KINDS)
@example(kinds=["rustlib"])
@example(kinds=["rustlib", "lib"])
@example(kinds=["empty"])
@example(kinds=[])
def test_a_path_is_prefixed_at_most_once_and_the_result_is_stable(
    shared_tree: ToolchainTree, kinds: list[str]
) -> None:
    """Applying the helper twice equals applying it once, and never repeats the lib."""
    argv = [str(shared_tree.rustc), "--version"]
    original = ":".join(_segments(shared_tree, kinds))
    once = toolchain_environment(argv, _env(original), CWD)
    twice = toolchain_environment(argv, once, CWD)
    assert twice == once, "idempotent"
    result = once.get(POISONED, "")
    already = str(shared_tree.lib) in original.split(":")
    event(f"already-present={already}, empty={not original}")
    if original and not already:
        assert result == f"{shared_tree.lib}:{original}", (
            "the lib comes first and the original follows verbatim"
        )
    else:
        assert result == original, "an empty or already-listed path is kept as it was"
    assert result.split(":").count(str(shared_tree.lib)) <= max(
        1, original.split(":").count(str(shared_tree.lib))
    ), "the directory is never added to a path that has it"


@given(kinds=KINDS)
def test_an_unrecognized_compiler_never_changes_the_path(
    shared_tree: ToolchainTree, shared_shim: Path, kinds: list[str]
) -> None:
    """The proxy and ``clippy-driver`` get exactly the caller's environment."""
    original = ":".join(_segments(shared_tree, kinds))
    clippy = shared_tree.root / "bin" / "clippy-driver"
    for argv in (
        [str(shared_shim), "-vV"],
        [str(clippy), str(shared_tree.rustc), "-vV"],
    ):
        env = _env(original, PATH="/bin")
        assert toolchain_environment(argv, env, CWD) == env, "left alone"


def test_the_variable_name_is_the_linux_loader_variable() -> None:
    """The constant the adapter documents is the glibc loader's variable."""
    assert LIBRARY_PATH_VARIABLE == POISONED, "LD_LIBRARY_PATH"
