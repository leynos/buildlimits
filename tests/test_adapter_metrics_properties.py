"""Generated invocation histories preserve terminal and admission boundaries."""

from pathlib import Path

import pytest
from adapter_metrics_support import expected_record
from conftest import RecordingSink
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st
from rustc_query_support import cargo_probe, path_spellings, temporary_executable

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
    ["--print=cfg", "--print=sysroot"],
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
    ["other-wrapper", "rustc"],
]
_TOTALITY_PREFIXES = [
    [],
    *_COMPILER_PREFIXES,
    ["sccache", "rustc"],
    ["sccache", "clippy-driver", "rustc"],
    ["sccache", "dylint-driver", "rustc"],
    ["clippy-driver", "dylint-driver", "rustc"],
    ["sccache", "other-wrapper", "rustc"],
]


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
    assert sink.records[0] == expected_record(history, initial_attempts, step), (
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


@given(
    rest=st.one_of(
        st.sampled_from(_SAFE_QUERY_ARGS),
        st.lists(st.sampled_from(_QUERY_RESTS), max_size=20),
    )
)
@example(rest=["-vV"])
@example(rest=["--print=cfg", "--emit=link"])
@example(rest=["-", "--crate-name", "___", "--print=file-names", "@args"])
@example(rest=["--print", "cfg"])
@example(rest=cargo_probe("x86_64-unknown-linux-gnu"))
def test_workspace_wrapper_changes_only_compiler_identity(rest: list[str]) -> None:
    """Drivers share rustc's grammar except for direct-only print spelling."""
    direct = is_toolchain_query(["rustc", *rest], {}, "/")
    for driver in ("clippy-driver", "dylint-driver"):
        wrapped = is_toolchain_query([driver, "rustc", *rest], {}, "/")
        assert not wrapped or direct, "a wrapper never exempts a queued rustc form"
        if "--print" not in rest:
            assert wrapped is direct, "shared grammar preserves rustc's result"


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
    case=st.tuples(
        st.sampled_from(_SAFE_QUERY_ARGS),
        st.sampled_from(_COMPILE_INDICATORS),
        st.integers(min_value=0, max_value=30),
        st.sampled_from(_COMPILER_PREFIXES),
        st.integers(min_value=0, max_value=4),
    )
)
@example(case=(["-vV"], "lib.rs", 0, ["rustc"], 0))
@example(case=(["--print=cfg"], "--emit=obj", 1, ["clippy-driver", "rustc"], 0))
@example(
    case=(
        ["-", "--crate-name", "___", "--print=file-names"],
        "@args",
        3,
        ["dylint-driver", "rustc"],
        2,
    )
)
@example(case=(["-vV"], "native-static-libs", 0, ["other-wrapper", "rustc"], 3))
def test_compile_indicators_never_skip_admission(
    case: tuple[list[str], str, int, list[str], int],
) -> None:
    """Adding a source, output or compiler flag invalidates every query form."""
    query, indicator, position, prefix, layers = case
    inserted = [*query[:position], indicator, *query[position:]]
    argv = [*(["sccache"] * layers), *prefix, *inserted]
    assert not is_toolchain_query(argv, {}, "/"), (
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


@given(
    outer=st.sampled_from(["clippy-driver", "dylint-driver"]),
    inner=st.sampled_from(["clippy-driver", "dylint-driver"]),
    query=st.sampled_from(_SAFE_QUERY_ARGS),
    layers=st.integers(min_value=0, max_value=4),
)
@example(outer="clippy-driver", inner="clippy-driver", query=["-vV"], layers=1)
@example(
    outer="dylint-driver",
    inner="dylint-driver",
    query=cargo_probe("x86_64-unknown-linux-gnu"),
    layers=3,
)
def test_two_workspace_wrappers_always_queue(
    outer: str, inner: str, query: list[str], layers: int
) -> None:
    """The classifier permits one driver layer and rejects nested drivers."""
    argv = [*(["sccache"] * layers), outer, inner, "rustc", *query]
    assert not is_toolchain_query(argv, {}, "/"), (
        "nested and same-driver wrappers remain admitted"
    )


@given(
    spelling_indices=st.tuples(
        st.integers(min_value=0, max_value=3),
        st.integers(min_value=0, max_value=3),
    ),
    use_symlink=st.booleans(),
    same_file=st.booleans(),
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
@example(spelling_indices=(0, 3), use_symlink=True, same_file=True)
@example(spelling_indices=(3, 1), use_symlink=False, same_file=False)
def test_wrapper_path_spelling_preserves_file_identity(
    tmp_path: Path,
    spelling_indices: tuple[int, int],
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
    argv_spelling, configured_spelling = spelling_indices
    invoked = path_spellings(invoked_paths[path_index], cwd)[argv_spelling]
    configured = path_spellings(configured_paths[path_index], cwd)[configured_spelling]
    assert (
        is_toolchain_query(
            [invoked, "rustc", "-vV"],
            {"RUSTC_WORKSPACE_WRAPPER": configured},
            str(cwd),
        )
        is same_file
    ), "classification follows resolved executable identity across path spellings"


@given(
    case=st.tuples(
        st.sampled_from(_TOTALITY_PREFIXES),
        st.lists(st.one_of(st.text(max_size=24), st.binary(max_size=24)), max_size=12),
        st.one_of(st.none(), st.text(max_size=24)),
        st.text(max_size=24),
        st.text(max_size=24),
    )
)
@example(case=(["rustc"], [b"\xff"], None, "", ""))
@example(case=(["rustc"], ["--print=cfg"], None, "", ""))
@example(case=(["clippy-driver", "rustc"], ["--print", "cfg"], None, "", ""))
@example(case=(["dylint-driver", "rustc"], ["-vV", "-vV"], None, "", ""))
@example(
    case=(
        ["sccache", "clippy-driver", "rustc"],
        ["", "-not-a-known-flag"],
        "\x00",
        "",
        "",
    )
)
def test_query_classifier_is_total(
    case: tuple[list[str], list[str | bytes], str | None, str, str],
) -> None:
    """Generated raw and recognized-prefix argv always return one result."""
    prefix, rest, wrapper, path, dylint_flags = case
    env = {"PATH": path, "DYLINT_RUSTFLAGS": dylint_flags}
    if wrapper is not None:
        env["RUSTC_WORKSPACE_WRAPPER"] = wrapper
    argv = [*prefix, *rest]
    assert isinstance(is_toolchain_query(argv, env, "/"), bool), (
        "every argv and environment returns one classifier outcome"
    )
