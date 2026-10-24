"""First-terminal-result prediction for sccache 0.14 Rust arguments."""

import pytest
from outcome_test_helpers import outcome_module


def _argv(*arguments: str) -> tuple[str, ...]:
    """Build a complete request vector with the compiler in argv[0]."""
    return ("/toolchain/bin/rustc", *arguments)


def _compile_arguments(
    *,
    missing: str | None = None,
    crate_type: str | None = "lib",
    emit: str = "link",
) -> tuple[str, ...]:
    """Build the smallest valid cacheable Rust compilation request."""
    arguments = []
    if missing != "crate_name":
        arguments.extend(("--crate-name", "example"))
    if crate_type is not None:
        arguments.extend(("--crate-type", crate_type))
    if missing != "input":
        arguments.append("src/lib.rs")
    if missing != "emit":
        arguments.extend(("--emit", emit))
    if missing != "output_dir":
        arguments.extend(("--out-dir", "target"))
    return _argv(*arguments)


@pytest.mark.parametrize(
    ("option", "expected", "case"),
    [
        (("--explain", "E0600"), "not_compile", "explain"),
        (("--help",), "not_compile", "help"),
        (("--pretty", "expanded"), "not_compile", "pretty"),
        (("--print", "sysroot"), "not_compile", "print"),
        (("--unpretty", "expanded"), "not_compile", "unpretty"),
        (("--version",), "not_compile", "version"),
        (("-V",), "not_compile", "short-version"),
        (("-",), "declined", "too-hard-flag"),
        (("-o", "output"), "declined", "too-hard-output"),
        (("--sysroot", "/rust/sysroot"), "declined", "too-hard-sysroot"),
        (("--crate-name",), "declined", "missing-crate-name-value"),
        (("--emit",), "declined", "missing-emit-value"),
        (("--extern=serde",), "declined", "extern-without-path"),
    ],
    ids=(
        "explain",
        "help",
        "pretty",
        "print",
        "unpretty",
        "version",
        "short-version",
        "too-hard-flag",
        "too-hard-output",
        "too-hard-sysroot",
        "missing-crate-name-value",
        "missing-emit-value",
        "extern-without-path",
    ),
)
def test_terminal_option_classes_are_predicted(
    option: tuple[str, ...], expected: str, case: str
) -> None:
    """Pinned non-compilation and refusal options map to closed classes."""
    parser = outcome_module("outcome_parser")

    assert parser.parser_class(_argv(*option)) == expected, (
        f"parser option case {case!r} maps to {expected!r}"
    )


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (("--help",), ("-o", "output"), "not_compile"),
        (("-o", "output"), ("--help",), "declined"),
        (("--print", "sysroot"), ("--sysroot", "/rust/sysroot"), "not_compile"),
        (("--sysroot", "/rust/sysroot"), ("--print", "sysroot"), "declined"),
    ],
    ids=("not-compile-first", "cannot-cache-first", "print-first", "sysroot-first"),
)
def test_first_terminal_option_wins_left_to_right(
    first: tuple[str, ...], second: tuple[str, ...], expected: str
) -> None:
    """The predictor stops at the first terminal parser result in argv order."""
    parser = outcome_module("outcome_parser")

    assert parser.parser_class(_argv(*first, *second)) == expected, (
        "the first terminal parser result wins"
    )


@pytest.mark.parametrize(
    "emit",
    ["dep-info", "asm", ""],
    ids=("dep-info-only", "asm-only", "empty-member"),
)
def test_emit_without_link_or_metadata_is_not_compilation(emit: str) -> None:
    """Emit-only requests without link or metadata output are non-compilations."""
    parser = outcome_module("outcome_parser")

    assert parser.parser_class(_compile_arguments(emit=emit)) == "not_compile", (
        f"emit={emit!r} without link or metadata is not a compilation"
    )


@pytest.mark.parametrize(
    ("emit", "expected"),
    [("metadata", "cacheable"), ("link,asm", "declined")],
    ids=("metadata-only-cacheable", "unsupported-emit-declined"),
)
def test_emit_shape_returns_its_expected_parser_class(emit: str, expected: str) -> None:
    """Supported metadata and unsupported emit combinations remain distinct."""
    parser = outcome_module("outcome_parser")

    assert parser.parser_class(_compile_arguments(emit=emit)) == expected, (
        f"emit={emit!r} maps to {expected!r}"
    )


@pytest.mark.parametrize(
    ("option", "case"),
    [
        (("--emit=metadata",), "repeated-emit-equals"),
        (("--emit", "link"), "repeated-emit-separated"),
        (("-C", "extra-filename"), "extra-filename-missing-value"),
        (("--codegen=extra-filename",), "extra-filename-equals"),
        (("-Cincremental=target/incremental",), "incremental"),
        (("--codegen", "incremental"), "incremental-without-value"),
    ],
    ids=(
        "repeated-emit-equals",
        "repeated-emit-separated",
        "extra-filename-missing-value",
        "extra-filename-equals",
        "incremental",
        "incremental-without-value",
    ),
)
def test_repeated_emit_and_codegen_options_are_declined(
    option: tuple[str, ...], case: str
) -> None:
    """Repeated emits and unsupported codegen modes are refused."""
    parser = outcome_module("outcome_parser")

    assert parser.parser_class(_argv(*_compile_arguments()[1:], *option)) == (
        "declined"
    ), f"parser refusal case {case!r} declines {option!r}"


@pytest.mark.parametrize(
    "crate_type",
    ["bin", "dylib", "proc-macro"],
    ids=("bin", "dylib", "proc-macro"),
)
def test_unsupported_crate_types_are_declined(crate_type: str) -> None:
    """Only rlib/lib and staticlib crate types enter the supported subset."""
    parser = outcome_module("outcome_parser")

    assert parser.parser_class(_compile_arguments(crate_type=crate_type)) == (
        "declined"
    ), f"unsupported crate type {crate_type!r} is declined"


def test_unsure_target_is_declined_without_retaining_the_path() -> None:
    """Path-like non-JSON targets take the conservative unsure-target branch."""
    parser = outcome_module("outcome_parser")
    arguments = _compile_arguments()
    target_path = "/private/custom-target"
    argv = _argv(*arguments[1:], "--target", target_path)
    prediction = parser.parser_class(argv)

    assert prediction == "declined", "a filesystem-dependent target is conservative"
    assert target_path not in prediction, "the prediction does not retain paths"


def test_multiple_input_files_are_declined() -> None:
    """A second raw compiler input is outside sccache's supported subset."""
    parser = outcome_module("outcome_parser")

    assert (
        parser.parser_class(_argv(*_compile_arguments()[1:], "src/extra.rs"))
        == "declined"
    ), "multiple raw inputs are declined"


@pytest.mark.parametrize(
    "missing",
    ["input", "output_dir", "emit", "crate_name"],
    ids=("input", "out-dir", "emit", "crate-name"),
)
def test_missing_required_fields_are_declined(missing: str) -> None:
    """Every required-field omission ends in the CannotCache class."""
    parser = outcome_module("outcome_parser")

    assert parser.parser_class(_compile_arguments(missing=missing)) == "declined", (
        f"missing required field {missing!r} is declined"
    )


def test_missing_supported_crate_type_is_declined() -> None:
    """A request with no rlib or staticlib target is not cacheable."""
    parser = outcome_module("outcome_parser")

    assert parser.parser_class(_compile_arguments(crate_type=None)) == "declined", (
        "requests without a supported crate type are declined"
    )


def test_cargo_compiler_version_probe_is_declined_as_missing_input() -> None:
    """Cargo's -vV request reaches the parser's post-scan missing-input rule."""
    parser = outcome_module("outcome_parser")

    assert parser.parser_class(_argv("-vV")) == "declined", (
        "the version probe has no input and is declined"
    )
