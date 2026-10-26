"""Examples and properties for the rustc query admission boundary."""

import os
from pathlib import Path

import pytest
from rustc_query_support import cargo_probe, temporary_executable

from buildlimits.rustc_query import is_toolchain_query

_PRINT_KINDS = (
    "sysroot",
    "target-libdir",
    "cfg",
    "target-list",
    "target-cpus",
    "target-features",
)


def _assert_query_stays_admitted(
    argv: list[str], env: dict[str, str], cwd: Path
) -> None:
    """Assert that an uncertain wrapper query retains compile admission.

    Parameters
    ----------
    argv
        The compiler command under test.
    env
        The environment used for wrapper identity verification.
    cwd
        The working directory used to resolve relative executables.
    """
    assert not is_toolchain_query(argv, env, str(cwd)), (
        "an uncertain wrapper query must remain admitted"
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ["-V"],
        ["--version"],
        ["-vV"],
        ["-V", "--verbose"],
        ["--verbose", "--version"],
        ["-vV", "--verbose"],
        *[[f"--print={kind}"] for kind in _PRINT_KINDS],
        *[["--print", kind] for kind in _PRINT_KINDS],
        ["--print=cfg", "--print=sysroot", "--print=cfg"],
        cargo_probe(),
        cargo_probe("x86_64-unknown-linux-gnu"),
        [
            "-",
            "--crate-name",
            "___",
            "--print=file-names",
            "--target=x86_64-unknown-linux-gnu",
            "--crate-type",
            "rlib",
        ],
    ],
    ids=[
        "version-short",
        "version-long",
        "version-verbose",
        "version-short-verbose",
        "version-long-verbose-reversed",
        "version-verbose-flag",
        *[f"print-equals-{kind}" for kind in _PRINT_KINDS],
        *[f"print-separated-{kind}" for kind in _PRINT_KINDS],
        "repeated-print-requests",
        "cargo-target-info",
        "cargo-target-info-target",
        "cargo-target-info-target-equals",
    ],
)
def test_allow_list_query_forms(arguments: list[str]) -> None:
    """Only the three source-backed query forms bypass admission."""
    assert is_toolchain_query(["/toolchain/bin/rustc", *arguments], {}, "/"), (
        "allow-listed query form is exempt"
    )


_NEAR_MISS_ARGUMENTS = [
    ["lib.rs", "-vV"],
    ["--print=cfg", "--emit=link"],
    ["-vV", "-vV"],
    [""],
    ["@path"],
    ["-vV", "@path"],
    ["--print=file-names"],
    ["--print=native-static-libs"],
    ["--print=link-args"],
    ["--print=crate-root-lint-levels"],
    ["--print=cfg=out.txt"],
    ["--print=cfg,sysroot"],
    [*cargo_probe(), "-Ctarget-cpu=native"],
    [*cargo_probe(), "-o", "output"],
    ["--verbose"],
    ["+nightly", "-vV"],
    ["--print=cfg", "--target=x86_64-unknown-linux-gnu"],
    [*cargo_probe("x86_64.json")],
    [*cargo_probe("lib.rs")],
    [*cargo_probe("../x86_64-unknown-linux-gnu")],
    [*cargo_probe("-x86_64-unknown-linux-gnu")],
    [*cargo_probe(), "-Zthreads=8"],
    [*cargo_probe(), "--cfg", "feature=demo"],
    [*cargo_probe(), "--print=cfg"],
    [*cargo_probe(), "--crate-type", "rlib"],
    [*cargo_probe(), "-W" + "warnings"],
    [*cargo_probe(), "-"],
    [
        *cargo_probe("x86_64-unknown-linux-gnu"),
        "--target=x86_64-unknown-linux-gnu",
    ],
]


@pytest.mark.parametrize(
    ("prefix", "arguments"),
    [
        (prefix, arguments)
        for prefix in (
            ["rustc"],
            ["clippy-driver", "/t/rustc"],
            ["dylint-driver", "/t/rustc"],
        )
        for arguments in _NEAR_MISS_ARGUMENTS
    ]
    + [([], []), (["rustc"], [])],
    ids=[
        f"{prefix}-{index}"
        for prefix in ("rustc", "clippy-driver", "dylint-driver")
        for index in range(len(_NEAR_MISS_ARGUMENTS))
    ]
    + ["empty-argv", "rustc-without-query"],
)
def test_near_misses_stay_admitted(prefix: list[str], arguments: list[str]) -> None:
    """Compile indicators and unsupported query syntax remain admitted."""
    assert not is_toolchain_query([*prefix, *arguments], {}, "/"), (
        "unsupported or compiling argv stays admitted"
    )


def test_driver_rejects_separate_print_spelling() -> None:
    """Drivers allow only the equals spelling to keep their grammar narrow."""
    assert not is_toolchain_query(
        ["clippy-driver", "rustc", "--print", "cfg"], {}, "/"
    ), "workspace wrappers reject the direct-only separate print spelling"


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["sccache", "rustc", "-vV"], True),
        (["/abs/sccache", "sccache", "/t/rustc", "-vV"], True),
        (["clippy-driver", "/t/rustc", "-vV"], True),
        (["/t/clippy-driver", "/t/rustc", "-V"], True),
        (["sccache", "clippy-driver", "/t/rustc", "-vV"], True),
        (
            ["/abs/sccache", "sccache", "/t/clippy-driver", "/t/rustc", "--print=cfg"],
            True,
        ),
        (["dylint-driver", "/t/rustc", "-vV"], True),
        (["sccache", "/d/dylint-driver", "/t/rustc", "-vV"], True),
        (["clippy-driver", "/t/rustc", *cargo_probe()], True),
        (["sccache", "clippy-driver", "/t/rustc", *cargo_probe()], True),
        (["dylint-driver", "/t/rustc", *cargo_probe()], True),
        (["sccache", "dylint-driver", "/t/rustc", *cargo_probe()], True),
        (["other-wrapper", "rustc", "-vV"], False),
        (["sccache", "other-wrapper", "rustc", "-vV"], False),
        (["cargo-clippy", "rustc", "-vV"], False),
        (["clippy-driver", "-vV"], False),
        (["clippy-driver", "/t/linked-rustc", "-vV"], False),
        (["clippy-driver", "dylint-driver", "/t/rustc", "-vV"], False),
        (["clippy-driver", "sccache", "/t/rustc", "-vV"], False),
        (["clippy-driver", "/t/rustc", "--rustc", "-vV"], False),
        (["clippy-driver", "/t/rustc", "--print", "cfg"], False),
        (["/t/rustdoc", "-V"], False),
    ],
)
def test_direct_and_workspace_wrapper_shapes(argv: list[str], expected: bool) -> None:
    """Only direct rustc and the two recognized workspace drivers are eligible."""
    assert is_toolchain_query(argv, {}, "/") is expected, (
        "example classification matches wrapper and compiler identity"
    )


def test_wrapper_identity_matches_path_and_symlink(
    tmp_path: Path,
) -> None:
    """The inherited wrapper may name the same driver through PATH or a symlink."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    driver = temporary_executable(tmp_path / "tools" / "clippy-driver")
    alias_dir = tmp_path / "aliases"
    alias_dir.mkdir()
    alias = alias_dir / "clippy-driver"
    alias.symlink_to(driver)
    env = {
        "PATH": os.pathsep.join([str(driver.parent), str(alias_dir)]),
        "RUSTC_WORKSPACE_WRAPPER": "clippy-driver",
    }

    assert is_toolchain_query(["clippy-driver", "rustc", "-vV"], env, str(cwd)), (
        "PATH resolves the configured wrapper to the invoked executable"
    )
    env["RUSTC_WORKSPACE_WRAPPER"] = str(alias)
    assert is_toolchain_query([str(driver), "rustc", "-vV"], env, str(cwd)), (
        "symlink and executable path identify the same wrapper"
    )


@pytest.mark.parametrize("wrapper_value", ["different", "missing"])
def test_wrapper_identity_mismatch_fails_closed(
    tmp_path: Path, wrapper_value: str
) -> None:
    """A different or unresolvable configured executable keeps admission."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    driver = temporary_executable(tmp_path / "actual" / "clippy-driver")
    if wrapper_value == "different":
        configured = temporary_executable(tmp_path / "other" / "clippy-driver")
    else:
        configured = tmp_path / "missing" / "clippy-driver"
    env = {
        "PATH": str(driver.parent),
        "RUSTC_WORKSPACE_WRAPPER": str(configured),
    }

    _assert_query_stays_admitted([str(driver), "rustc", "-vV"], env, cwd)


def test_wrapper_identity_lookup_error_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A filesystem error during identity verification keeps admission."""
    driver = temporary_executable(tmp_path / "clippy-driver")
    lookup_errors: list[tuple[Path, str | os.PathLike[str]]] = []

    def fail_samefile(path: Path, other: str | os.PathLike[str]) -> bool:
        lookup_errors.append((path, other))
        raise OSError("injected identity lookup failure")

    monkeypatch.setattr(Path, "samefile", fail_samefile)
    env = {"RUSTC_WORKSPACE_WRAPPER": str(driver)}

    _assert_query_stays_admitted([str(driver), "rustc", "-vV"], env, tmp_path)
    assert lookup_errors, "the failure came from the identity check"


def test_an_arbitrary_wrapper_does_not_gain_trust_from_environment(
    tmp_path: Path,
) -> None:
    """A matching environment variable cannot promote an unknown wrapper name."""
    wrapper = temporary_executable(tmp_path / "my-wrapper")
    env = {"RUSTC_WORKSPACE_WRAPPER": str(wrapper)}
    _assert_query_stays_admitted([str(wrapper), "rustc", "-vV"], env, tmp_path)


@pytest.mark.parametrize(
    ("flags", "expected"),
    [("", True), (" \t\n ", True), ("-Ctarget-cpu=native", False)],
)
def test_dylint_rustflags_fail_closed(flags: str, expected: bool) -> None:
    """Dylint's separately injected rustflags are outside the query grammar."""
    assert (
        is_toolchain_query(
            ["dylint-driver", "rustc", "-vV"], {"DYLINT_RUSTFLAGS": flags}, "/"
        )
        is expected
    ), "non-whitespace Dylint flags remain admitted"
