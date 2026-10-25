"""Recognize Cargo's pinned stdin target-information query.

The target-information argv follows Cargo commit
`fe2f314aef06e688a9517da1ac0577bb1854d01f`
(``src/cargo/core/compiler/build_context/target_info.rs``). Unknown tokens
and values fail closed so an input file or code-generation option stays
admitted.
"""

import dataclasses

PROBE_PRINT_KINDS = frozenset({
    "file-names",
    "sysroot",
    "split-debuginfo",
    "crate-name",
    "cfg",
})
CRATE_TYPES = frozenset({"bin", "rlib", "dylib", "cdylib", "staticlib", "proc-macro"})
_WARNINGS_FLAG = "-W" + "warnings"


@dataclasses.dataclass(slots=True)
class _ProbeState:
    """Collect the required identity and output fields for Cargo's probe."""

    has_stdin: bool = False
    has_crate_name: bool = False
    crate_types: set[str] = dataclasses.field(default_factory=set)
    print_kinds: set[str] = dataclasses.field(default_factory=set)
    has_warnings: bool = False
    has_target: bool = False

    def is_complete(self) -> bool:
        """Check that the probe identifies stdin and requests file names.

        Returns
        -------
        bool
            Whether all required Cargo query fields were seen.
        """
        return all((
            self.has_stdin,
            self.has_crate_name,
            bool(self.crate_types),
            "file-names" in self.print_kinds,
        ))


def is_cargo_target_probe(arguments: tuple[str, ...]) -> bool:
    """Recognize Cargo's stdin target probe with no compile indicators.

    Parameters
    ----------
    arguments : tuple[str, ...]
        Arguments after the inner rustc executable.

    Returns
    -------
    bool
        Whether the complete vector matches the allow-listed Cargo query.

    Examples
    --------
    >>> is_cargo_target_probe(("-", "--crate-name", "___", "--print=file-names"))
    False
    """
    if "native-static-libs" in arguments:
        return False
    state = _ProbeState()
    index = 0
    while index < len(arguments):
        next_index = _consume_probe_argument(arguments, index, state)
        if next_index is None:
            return False
        index = next_index
    return state.is_complete()


def _consume_probe_argument(
    arguments: tuple[str, ...], index: int, state: _ProbeState
) -> int | None:
    """Apply one token parser and return its next index.

    Returns
    -------
    int | None
        The first unconsumed index, or ``None`` for an unsupported token.
    """
    single_token_parsers = (
        _consume_stdin,
        _consume_warnings,
        _consume_print,
    )
    for parser in single_token_parsers:
        if parser(arguments, index, state):
            return index + 1
    value_parsers = (
        _consume_crate_name,
        _consume_crate_type,
        _consume_target,
    )
    for parser in value_parsers:
        next_index = parser(arguments, index, state)
        if next_index is not None:
            return next_index
    return None


def _consume_stdin(arguments: tuple[str, ...], index: int, state: _ProbeState) -> bool:
    """Consume Cargo's single stdin marker once.

    Returns
    -------
    bool
        Whether this parser consumed the stdin marker.
    """
    if arguments[index] != "-" or state.has_stdin:
        return False
    state.has_stdin = True
    return state.has_stdin


def _consume_warnings(
    arguments: tuple[str, ...], index: int, state: _ProbeState
) -> bool:
    """Consume Cargo's warnings flag once.

    Returns
    -------
    bool
        Whether this parser consumed the warnings flag.
    """
    if arguments[index] != _WARNINGS_FLAG or state.has_warnings:
        return False
    state.has_warnings = True
    return state.has_warnings


def _consume_print(arguments: tuple[str, ...], index: int, state: _ProbeState) -> bool:
    """Consume one allow-listed Cargo print kind once.

    Returns
    -------
    bool
        Whether this parser consumed an allowed print kind.
    """
    argument = arguments[index]
    if not argument.startswith("--print="):
        return False
    kind = argument.removeprefix("--print=")
    if kind not in PROBE_PRINT_KINDS or kind in state.print_kinds:
        return False
    state.print_kinds.add(kind)
    return kind in state.print_kinds


def _consume_crate_name(
    arguments: tuple[str, ...], index: int, state: _ProbeState
) -> int | None:
    """Consume Cargo's synthetic crate name once.

    Returns
    -------
    int | None
        The next index, or ``None`` when the token or value is invalid.
    """
    if arguments[index] != "--crate-name" or state.has_crate_name:
        return None
    crate_name = following_argument(arguments, index)
    if crate_name != "___":
        return None
    state.has_crate_name = True
    return index + 2


def _consume_crate_type(
    arguments: tuple[str, ...], index: int, state: _ProbeState
) -> int | None:
    """Consume one allow-listed Cargo crate type.

    Returns
    -------
    int | None
        The next index, or ``None`` when the token or value is invalid.
    """
    if arguments[index] != "--crate-type":
        return None
    crate_type = following_argument(arguments, index)
    if crate_type not in CRATE_TYPES or crate_type in state.crate_types:
        return None
    state.crate_types.add(crate_type)
    return index + 2


def _consume_target(
    arguments: tuple[str, ...], index: int, state: _ProbeState
) -> int | None:
    """Consume one target triple in separated or equals form.

    Returns
    -------
    int | None
        The next index, or ``None`` when the token or value is invalid.
    """
    argument = arguments[index]
    if argument == "--target":
        target = following_argument(arguments, index)
        next_index = index + 2
    elif argument.startswith("--target="):
        target = argument.removeprefix("--target=")
        next_index = index + 1
    else:
        return None
    if state.has_target or target is None:
        return None
    if not _valid_target(target):
        return None
    state.has_target = True
    return next_index


def following_argument(arguments: tuple[str, ...], index: int) -> str | None:
    """Return an option value, preserving empty strings.

    Parameters
    ----------
    arguments : tuple[str, ...]
        The complete argument vector.
    index : int
        Position of the option whose value is requested.

    Returns
    -------
    str | None
        The following token, or ``None`` if the option is last.
    """
    return arguments[index + 1] if index + 1 < len(arguments) else None


def _valid_target(target: str) -> bool:
    """Reject target values that name options, paths or JSON target files.

    Returns
    -------
    bool
        Whether the value resembles a target triple rather than an input path.
    """
    return bool(target) and all(
        component
        and all(character.isalnum() or character == "_" for character in component)
        for component in target.split("-")
    )
