"""Generated invocation histories preserve terminal and admission boundaries."""

from pathlib import Path

import pytest
from conftest import RecordingSink
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from rustc_query_support import cargo_probe, temporary_executable

from buildlimits.adapter_metrics import AdapterMetrics
from buildlimits.metrics import MAX_COUNTER, Metrics
from buildlimits.rustc_query import is_toolchain_query

_QUERY_RESTS = [
    "-V",
    "--version",
    "-vV",
    "--verbose",
    "--print=cfg",
    "--print=sysroot",
    "--print=file-names",
    "--print=crate-root-lint-levels",
    "-",
    "--crate-name",
    "___",
    "--crate-type",
    "bin",
    "--print=split-debuginfo",
    "--target",
    "x86_64-unknown-linux-gnu",
    "lib.rs",
    "--emit=link",
    "-o",
    "@args",
    "-Ctarget-cpu=native",
    "native-static-libs",
    "unknown-token",
]
_SAFE_QUERY_ARGS = [
    ["-V"],
    ["--version", "--verbose"],
    ["-vV"],
    ["--print=cfg"],
    ["--print=target-features"],
    cargo_probe("x86_64-unknown-linux-gnu"),
]
_COMPILE_INDICATORS = [
    "lib.rs",
    "--emit",
    "--emit=obj",
    "-o",
    "@args",
    "native-static-libs",
]
_COMPILER_PREFIXES = [
    ["rustc"],
    ["clippy-driver", "rustc"],
    ["dylint-driver", "rustc"],
]


def _path_spellings(path: Path, cwd: Path) -> list[str]:
    """Return absolute, relative, dot-segment and trailing-slash spellings."""
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


def _expected_record(
    history: list[str], initial_attempts: int, step: int
) -> dict[str, str]:
    """Derive a summary from the immutable prefix ending at the first exit."""
    terminal = next(
        index
        for index, action in enumerate(history)
        if action in {"finish", "interrupt"}
    )
    prefix = history[:terminal]
    admissions = [index for index, action in enumerate(prefix) if action == "admit"]
    wait = (admissions[0] + 1) * step if admissions else (terminal + 1) * step
    runtime = (terminal - admissions[0]) * step if admissions else 0
    return {
        "BUILD_LIMITS_SCHEMA": "1",
        "BUILD_LIMITS_SOURCE": "adapter",
        "BUILD_LIMITS_EVENT": "adapter_finished",
        "BUILD_LIMITS_OUTCOME": "interrupted"
        if history[terminal] == "interrupt"
        else "completed",
        "BUILD_LIMITS_CONNECTION_ATTEMPTS": str(
            min(MAX_COUNTER, initial_attempts + prefix.count("attempt"))
        ),
        "BUILD_LIMITS_BUSY_RETRIES": "0",
        "BUILD_LIMITS_RESTART_RETRIES": "0",
        "BUILD_LIMITS_WAIT_SECONDS": f"{wait:.3f}",
        "BUILD_LIMITS_RUNTIME_SECONDS": f"{runtime:.3f}",
    }


@given(
    actions=st.lists(
        st.sampled_from(["attempt", "admit", "finish", "interrupt"]), max_size=40
    ),
    initial_attempts=st.sampled_from([0, MAX_COUNTER - 1, MAX_COUNTER]),
    step=st.integers(min_value=0, max_value=1000),
)
@example(
    actions=["admit", "admit", "finish", "interrupt", "finish"],
    initial_attempts=0,
    step=1,
)
@example(
    actions=["attempt", "attempt", "finish", "finish"],
    initial_attempts=MAX_COUNTER - 1,
    step=0,
)
def test_invocation_history(
    actions: list[str], initial_attempts: int, step: int
) -> None:
    """Only the first terminal boundary emits, using the first admission and capped retries."""
    sink = RecordingSink()
    now = [10.0]
    summary = AdapterMetrics(lambda: now[0], now[0], Metrics(sink, source="adapter"))
    summary.attempts = initial_attempts
    summary.outcome = "completed"
    history = [*actions, "finish"]
    for index, action in enumerate(history):
        now[0] = 10.0 + (index + 1) * step
        match action:
            case "attempt":
                summary.attempted()
            case "admit":
                summary.admission(now[0])
            case "interrupt":
                summary.interrupted()
            case "finish":
                summary.finish()
    assert len(sink.records) == 1, "at most one terminal record across repeated exits"
    assert sink.records[0] == _expected_record(history, initial_attempts, step), (
        "the first terminal prefix alone determines the summary"
    )
    assert summary.attempts == min(
        MAX_COUNTER, initial_attempts + history.count("attempt")
    ), "all retry increments saturate, including those after emission"
    all_admissions = [
        index for index, action in enumerate(history) if action == "admit"
    ]
    if all_admissions:
        assert summary.admitted == pytest.approx(
            10.0 + (all_admissions[0] + 1) * step
        ), "the first admission always wins"
    else:
        assert summary.admitted is None, "no admission was observed"


@given(rest=st.lists(st.sampled_from(_QUERY_RESTS), max_size=20))
@example(rest=["-vV"])
@example(rest=["--print=cfg", "--emit=link"])
@example(rest=["-", "--crate-name", "___", "--print=file-names", "@args"])
def test_workspace_wrapper_changes_only_compiler_identity(rest: list[str]) -> None:
    """Recognized drivers preserve rustc's result outside direct-only syntax."""
    # Separated ``--print KIND`` is intentionally a direct-rustc-only spelling.
    assert "--print" not in rest, (
        "the property isolates wrapper identity from direct-only grammar"
    )
    direct = is_toolchain_query(["rustc", *rest], {}, "/")
    for driver in ("clippy-driver", "dylint-driver"):
        assert is_toolchain_query([driver, "rustc", *rest], {}, "/") is direct, (
            "recognized workspace wrapper preserves the bare rustc result"
        )


@given(
    rest=st.lists(st.sampled_from(_QUERY_RESTS), max_size=16),
    wrapper=st.one_of(
        st.sampled_from([
            "/tooling/clippy-driver.sh",
            "/tooling/xclippy-driver",
            "/tooling/dylint-driver.extra",
        ]),
        st.text(alphabet=st.characters(blacklist_characters="/\x00"), max_size=24).map(
            lambda suffix: f"other-{suffix}"
        ),
    ),
)
@example(wrapper="/tooling/clippy-driver.sh", rest=["-vV"])
@example(wrapper="/tooling/xclippy-driver", rest=["--print=cfg"])
@example(wrapper="/tooling/dylint-driver.extra", rest=["-V"])
def test_unrecognized_wrapper_names_never_skip(rest: list[str], wrapper: str) -> None:
    """Names that only resemble allow-listed drivers remain admitted."""
    assert not is_toolchain_query([wrapper, "rustc", *rest], {}, "/"), (
        "unrecognized wrapper never bypasses admission"
    )


@given(rest=st.lists(st.sampled_from(_QUERY_RESTS), max_size=16))
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@example(rest=["-vV"])
@example(rest=["--print=cfg"])
def test_wrapper_environment_mismatch_never_skips(
    tmp_path: Path, rest: list[str]
) -> None:
    """A different configured executable cannot authorize a driver token."""
    cwd = tmp_path / "work"
    actual = temporary_executable(cwd / "actual" / "clippy-driver")
    configured = temporary_executable(cwd / "configured" / "clippy-driver")
    env = {"RUSTC_WORKSPACE_WRAPPER": str(configured)}
    assert actual != configured, "fixture executables have different identities"
    assert not is_toolchain_query([str(actual), "rustc", *rest], env, str(cwd)), (
        "configured wrapper mismatch fails closed"
    )


@given(
    query=st.sampled_from(_SAFE_QUERY_ARGS),
    indicator=st.sampled_from(_COMPILE_INDICATORS),
    position=st.integers(min_value=0, max_value=30),
    prefix=st.sampled_from(_COMPILER_PREFIXES),
)
@example(query=["-vV"], indicator="lib.rs", position=0, prefix=["rustc"])
@example(
    query=["--print=cfg"],
    indicator="--emit=obj",
    position=1,
    prefix=["clippy-driver", "rustc"],
)
@example(
    query=["-", "--crate-name", "___", "--print=file-names"],
    indicator="@args",
    position=3,
    prefix=["dylint-driver", "rustc"],
)
def test_compile_indicators_never_skip_admission(
    query: list[str], indicator: str, position: int, prefix: list[str]
) -> None:
    """Adding a source, output or compiler flag invalidates every query form."""
    inserted = [*query[:position], indicator, *query[position:]]
    assert not is_toolchain_query([*prefix, *inserted], {}, "/"), (
        "compile indicators always force admission"
    )


@given(
    argv=st.one_of(
        st.sampled_from(_SAFE_QUERY_ARGS),
        st.lists(st.sampled_from(_QUERY_RESTS), min_size=1, max_size=12),
    ),
    prefix=st.sampled_from(_COMPILER_PREFIXES),
    layers=st.integers(min_value=0, max_value=4),
)
def test_sccache_does_not_change_query_classification(
    argv: list[str], prefix: list[str], layers: int
) -> None:
    """Stripping any number of leading caller clients preserves the result."""
    wrapped = [*prefix, *argv]
    expected = is_toolchain_query(wrapped, {}, "/")
    actual = is_toolchain_query(
        [*("sccache" for _ in range(layers)), *wrapped], {}, "/"
    )
    assert actual is expected, "leading sccache layers do not change classification"


@given(query=st.sampled_from(_SAFE_QUERY_ARGS))
def test_two_workspace_wrappers_always_queue(query: list[str]) -> None:
    """The classifier permits one driver layer and rejects nested drivers."""
    assert not is_toolchain_query(
        ["clippy-driver", "dylint-driver", "rustc", *query], {}, "/"
    ), "nested Clippy and Dylint wrappers remain admitted"
    assert not is_toolchain_query(
        ["dylint-driver", "clippy-driver", "rustc", *query], {}, "/"
    ), "nested Dylint and Clippy wrappers remain admitted"


@given(
    path_spellings=st.tuples(
        st.integers(min_value=0, max_value=3),
        st.integers(min_value=0, max_value=3),
    ),
    use_symlink=st.booleans(),
    same_file=st.booleans(),
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@example(path_spellings=(0, 3), use_symlink=True, same_file=True)
@example(path_spellings=(3, 1), use_symlink=False, same_file=False)
def test_wrapper_path_spelling_preserves_file_identity(
    tmp_path: Path,
    path_spellings: tuple[int, int],
    use_symlink: bool,
    same_file: bool,
) -> None:
    """Path spelling preserves same-file identity and rejects different files."""
    cwd = tmp_path / "work"
    actual = temporary_executable(cwd / "tools" / "clippy-driver")
    invoked_alias = cwd / "aliases" / "clippy-driver"
    invoked_alias.parent.mkdir(parents=True, exist_ok=True)
    if not invoked_alias.exists():
        invoked_alias.symlink_to(actual)
    configured_file = (
        actual
        if same_file
        else temporary_executable(cwd / "configured" / "clippy-driver")
    )
    alias_directory = (
        "configured-aliases-same" if same_file else "configured-aliases-other"
    )
    configured_alias = cwd / alias_directory / "clippy-driver"
    configured_alias.parent.mkdir(parents=True, exist_ok=True)
    if not configured_alias.exists():
        configured_alias.symlink_to(configured_file)
    invoked_paths = [actual, invoked_alias]
    configured_paths = [configured_file, configured_alias]
    path_index = 1 if use_symlink else 0
    argv_spelling, configured_spelling = path_spellings
    invoked = _path_spellings(invoked_paths[path_index], cwd)[argv_spelling]
    configured = _path_spellings(configured_paths[path_index], cwd)[configured_spelling]
    assert (
        is_toolchain_query(
            [invoked, "rustc", "-vV"],
            {"RUSTC_WORKSPACE_WRAPPER": configured},
            str(cwd),
        )
        is same_file
    ), "classification follows resolved executable identity across path spellings"


@given(
    argv=st.lists(st.one_of(st.text(max_size=24), st.binary(max_size=24)), max_size=12),
    wrapper=st.one_of(st.none(), st.text(max_size=24)),
    path=st.text(max_size=24),
    dylint_flags=st.text(max_size=24),
)
@example(argv=[b"rustc", b"\xff"], wrapper=None, path="", dylint_flags="")
@example(
    argv=["clippy-driver", "rustc", "-vV"],
    wrapper="\x00",
    path="",
    dylint_flags="",
)
def test_query_classifier_is_total(
    argv: list[str | bytes],
    wrapper: str | None,
    path: str,
    dylint_flags: str,
) -> None:
    """Every generated argv and string environment returns one boolean result."""
    env = {"PATH": path, "DYLINT_RUSTFLAGS": dylint_flags}
    if wrapper is not None:
        env["RUSTC_WORKSPACE_WRAPPER"] = wrapper
    assert isinstance(is_toolchain_query(argv, env, "/"), bool), (
        "every argv and environment returns one classifier outcome"
    )
