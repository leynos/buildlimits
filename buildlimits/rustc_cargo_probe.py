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

    seen_flags: set[str] = dataclasses.field(default_factory=set)
    crate_names: set[str] = dataclasses.field(default_factory=set)
    crate_types: set[str] = dataclasses.field(default_factory=set)
    print_kinds: set[str] = dataclasses.field(default_factory=set)
    has_target: bool = False

    def is_complete(self) -> bool:
        """Check that the probe identifies stdin and requests file names.

        Returns
        -------
        bool
            Whether all required Cargo query fields were seen.
        """
        return all((
            "-" in self.seen_flags,
            "___" in self.crate_names,
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
    if _consume_unique_flag(arguments, index, "-", state.seen_flags):
        return index + 1
    if _consume_unique_flag(arguments, index, _WARNINGS_FLAG, state.seen_flags):
        return index + 1
    if _consume_print(arguments, index, state):
        return index + 1
    if arguments[index] == "--crate-name":
        return _consume_unique_value_option(
            arguments,
            index,
            ("--crate-name", frozenset({"___"})),
            state.crate_names,
        )
    if arguments[index] == "--crate-type":
        return _consume_unique_value_option(
            arguments,
            index,
            ("--crate-type", CRATE_TYPES),
            state.crate_types,
        )
    return _consume_target(arguments, index, state)


def _consume_unique_flag(
    arguments: tuple[str, ...], index: int, flag: str, seen: set[str]
) -> bool:
    """Consume one allow-listed single token once.

    Returns
    -------
    bool
        Whether the token was consumed and recorded.
    """
    if arguments[index] != flag or flag in seen:
        return False
    seen.add(flag)
    return True


def _consume_unique_value_option(
    arguments: tuple[str, ...],
    index: int,
    rule: tuple[str, frozenset[str]],
    seen: set[str],
) -> int | None:
    """Consume one value option while rejecting unknown or repeated values.

    Returns
    -------
    int | None
        The next argv index, or ``None`` when the token or value is invalid.
    """
    option, allowed_values = rule
    if arguments[index] != option:
        return None
    value = following_argument(arguments, index)
    if value not in allowed_values or value in seen:
        return None
    seen.add(value)
    return index + 2


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
    return _record_unique_value(kind, PROBE_PRINT_KINDS, state.print_kinds)


def _record_unique_value(value: str, allowed: frozenset[str], seen: set[str]) -> bool:
    """Record one allowed value once for either print or value options.

    Returns
    -------
    bool
        Whether the value was allowed and had not already been seen.
    """
    if value not in allowed or value in seen:
        return False
    seen.add(value)
    return True


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
