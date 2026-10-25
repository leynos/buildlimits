"""Predict the closed sccache 0.14 Rust parser result from an argv value."""

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

type ParserClass = typ.Literal["cacheable", "not_compile", "declined"]

_NOT_COMPILATION = frozenset({
    "--explain",
    "--help",
    "--pretty",
    "--print",
    "--unpretty",
    "--version",
    "-V",
})
_VALUE_OPTIONS = frozenset({
    "--allow",
    "--cap-lints",
    "--cfg",
    "--check-cfg",
    "--codegen",
    "--crate-name",
    "--crate-type",
    "--crate-version",
    "--deny",
    "--diagnostic-width",
    "--emit",
    "--edition",
    "--error-format",
    "--extern",
    "--forbid",
    "--json",
    "--lint-cap",
    "--out-dir",
    "--remap-path-prefix",
    "--target",
    "-A",
    "-C",
    "-D",
    "-F",
    "-L",
    "-W",
    "-l",
    "-Z",
})
_SHORT_VALUE_OPTIONS = frozenset({"-A", "-C", "-D", "-F", "-L", "-W", "-l", "-Z"})
_TOO_HARD_OPTIONS = frozenset({"-o", "--sysroot"})
_CORE_VALUE_OPTIONS = frozenset({
    "--crate-name",
    "--crate-type",
    "--emit",
    "--out-dir",
    "--target",
})
_SUPPORTED_CRATE_TYPES = frozenset({"lib", "rlib", "staticlib"})
_SUPPORTED_EMITS = frozenset({"link", "metadata", "dep-info"})
_SHORT_OPTION_PREFIX_LENGTH = 2


@dc.dataclass(slots=True)
class _ParserArguments:
    """Small mutable accumulator local to one pure parser prediction."""

    values: dict[str, list[str]]
    codegen: list[str]
    input_count: int = 0


type _ScanResult = _ParserArguments | ParserClass


def parser_class(argv: cabc.Sequence[str]) -> ParserClass:
    """Predict sccache's first Rust-parser class without retaining argv.

    The compiler occupies ``argv[0]`` and is ignored. Unknown flags and
    filesystem-dependent target forms decline conservatively.

    Returns
    -------
    ParserClass
        ``cacheable``, ``not_compile`` or ``declined``.

    Examples
    --------
    >>> parser_class(
    ...     (
    ...         "rustc", "--crate-name", "demo", "--crate-type=lib",
    ...         "src/lib.rs", "--emit=link", "--out-dir", "target",
    ...     )
    ... )
    'cacheable'
    >>> parser_class(
    ...     (
    ...         "rustc", "--crate-name", "demo", "--crate-type=lib",
    ...         "src/lib.rs", "--emit=dep-info", "--out-dir", "target",
    ...     )
    ... )
    'not_compile'
    """
    result = _scan_arguments(argv)
    if isinstance(result, str):
        return result
    return _classify(result)


def _scan_arguments(argv: cabc.Sequence[str]) -> _ScanResult:
    """Scan options in order and stop at the first terminal parser class.

    Returns
    -------
    _ScanResult
        Parsed fields or the first terminal class.
    """
    parsed = _new_arguments()
    index = 1
    while index < len(argv):
        if argv[index] == "--":
            return _scan_after_separator(argv[index + 1 :], parsed)
        step = _scan_argument(argv, index, parsed)
        if isinstance(step, str):
            return step
        index = step
    return parsed


def _scan_after_separator(
    arguments: cabc.Sequence[str], parsed: _ParserArguments
) -> _ScanResult:
    """Count raw inputs after the option terminator.

    Returns
    -------
    _ScanResult
        Accumulated fields, or decline when multiple inputs follow.
    """
    for _argument in arguments:
        if not _add_input(parsed):
            return "declined"
    return parsed


def _scan_argument(
    argv: cabc.Sequence[str], index: int, parsed: _ParserArguments
) -> int | ParserClass:
    """Scan one option or source input and return the next argv index.

    Returns
    -------
    int or ParserClass
        Next index, or the first terminal parser class.
    """
    argument = argv[index]
    if _not_compilation(argument):
        return "not_compile"
    if argument == "-" or argument in _TOO_HARD_OPTIONS:
        return "declined"
    option, attached_value = _option_parts(argument)
    if option in _VALUE_OPTIONS:
        consumed = _read_option_value(argv, index, attached_value)
        if consumed is None:
            return "declined"
        value, next_index = consumed
        terminal = _store_option(parsed, option, value)
        return terminal if terminal is not None else next_index
    if argument.startswith("-") or not _add_input(parsed):
        return "declined"
    return index + 1


def _new_arguments() -> _ParserArguments:
    """Create empty per-call parser fields.

    Returns
    -------
    _ParserArguments
        New accumulator with no input or options.
    """
    values = {option: [] for option in _CORE_VALUE_OPTIONS}
    return _ParserArguments(values, [])


def _not_compilation(argument: str) -> bool:
    """Check the exact options that request compiler information or output.

    Returns
    -------
    bool
        Whether this option is a non-compilation request.
    """
    return argument.partition("=")[0] in _NOT_COMPILATION


def _option_parts(argument: str) -> tuple[str, str | None]:
    """Split attached long and short option values.

    Returns
    -------
    tuple[str, str or None]
        Option name and attached value, if present.
    """
    short_option = argument[:_SHORT_OPTION_PREFIX_LENGTH]
    if (
        short_option in _SHORT_VALUE_OPTIONS
        and len(argument) > _SHORT_OPTION_PREFIX_LENGTH
    ):
        return short_option, argument[_SHORT_OPTION_PREFIX_LENGTH:]
    option, separator, value = argument.partition("=")
    if separator:
        return option, value
    return argument, None


def _read_option_value(
    argv: cabc.Sequence[str], index: int, attached: str | None
) -> tuple[str, int] | None:
    """Read an attached value or consume the following argv element.

    Returns
    -------
    tuple[str, int] or None
        Value and next index, or no value for a parse error.
    """
    if attached is not None:
        return attached, index + 1
    if index + 1 >= len(argv):
        return None
    return argv[index + 1], index + 2


def _store_option(
    parsed: _ParserArguments, option: str, value: str
) -> ParserClass | None:
    """Store relevant fields or return an immediate decline.

    Returns
    -------
    ParserClass or None
        Terminal decline, or no terminal result.
    """
    if option in parsed.values:
        should_decline = False
        match option:
            case "--emit" if parsed.values[option]:
                should_decline = True
            case "--crate-type" if _has_unsupported_crate_type(value):
                should_decline = True
            case "--target" if _is_unsure_target(value) or not value:
                should_decline = True
            case "--crate-name" | "--out-dir" if not value:
                should_decline = True
        if should_decline:
            return "declined"
        parsed.values[option].append(value)
        return None
    if option in {"-C", "--codegen"}:
        return _store_codegen(parsed, value)
    return None


def _store_codegen(parsed: _ParserArguments, value: str) -> ParserClass | None:
    """Store a codegen value unless it makes the request uncacheable.

    Returns
    -------
    ParserClass or None
        Terminal decline, or no terminal result.
    """
    option, separator, setting = value.partition("=")
    if option == "incremental":
        return "declined"
    if option != "extra-filename":
        parsed.codegen.append(value)
        return None
    if not separator or not setting:
        return "declined"
    parsed.codegen.append(value)
    return None


def _has_unsupported_crate_type(value: str) -> bool:
    """Check whether any requested crate type is outside the supported set.

    Returns
    -------
    bool
        Whether the option contains an unsupported crate type.
    """
    return any(
        crate_type not in _SUPPORTED_CRATE_TYPES for crate_type in value.split(",")
    )


def _add_input(parsed: _ParserArguments) -> bool:
    """Count one raw compiler input and reject a second.

    Returns
    -------
    bool
        Whether the input fits the supported single-input subset.
    """
    if parsed.input_count:
        return False
    parsed.input_count += 1
    return True


def _classify(parsed: _ParserArguments) -> ParserClass:
    """Apply sccache's post-scan required-field and emit ordering.

    Returns
    -------
    ParserClass
        The final cacheable, non-compilation or declined class.
    """
    if not parsed.input_count:
        return "declined"
    for required in ("--out-dir", "--emit", "--crate-name"):
        if not parsed.values[required]:
            return "declined"
    emits = set(parsed.values["--emit"][0].split(","))
    if not emits.intersection({"link", "metadata"}):
        return "not_compile"
    if not parsed.values["--crate-type"] or not _has_supported_crate_type(parsed):
        return "declined"
    if not emits.issubset(_SUPPORTED_EMITS):
        return "declined"
    return "cacheable"


def _has_supported_crate_type(parsed: _ParserArguments) -> bool:
    """Check whether at least one supported crate type was requested.

    Returns
    -------
    bool
        Whether an rlib, lib or staticlib target is present.
    """
    return any(
        crate_type in _SUPPORTED_CRATE_TYPES
        for value in parsed.values["--crate-type"]
        for crate_type in value.split(",")
    )


def _is_unsure_target(value: str) -> bool:
    """Identify target forms requiring filesystem evidence.

    Returns
    -------
    bool
        Whether a path-like non-JSON target must conservatively decline.
    """
    path_like = any(separator in value for separator in ("/", "\\"))
    path_like = path_like or value.startswith((".", "~"))
    return path_like and not value.endswith(".json")
