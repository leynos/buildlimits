"""Canonical compiler arguments and selected Cargo environment for HMAC keys."""

import hashlib
import hmac
import json
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_key import Salt

_SHORT_OPTION_PREFIX_LENGTH = 2
_UNHASHED_CARGO_ENV = frozenset({"CARGO_MAKEFLAGS", "CARGO_BUILD_JOBS"})
_IGNORED_ARGUMENTS = frozenset({
    "--extern",
    "-L",
    "--check-cfg",
    "--out-dir",
    "--diagnostic-width",
})
_VALUE_OPTIONS = frozenset({
    "--allow",
    "--cap-lints",
    "--check-cfg",
    "--cfg",
    "--codegen",
    "--crate-name",
    "--crate-type",
    "--crate-version",
    "--diagnostic-width",
    "--deny",
    "--edition",
    "--emit",
    "--error-format",
    "--extern",
    "--forbid",
    "--json",
    "--lint-cap",
    "--out-dir",
    "--remap-path-prefix",
    "--sysroot",
    "--target",
    "-A",
    "-C",
    "-D",
    "-F",
    "-L",
    "-W",
    "-l",
    "-Z",
    "-o",
})


def argument_digest(
    argv: cabc.Sequence[str], env: cabc.Mapping[str, str], salt: Salt
) -> str:
    """HMAC normalized arguments and sccache's hashed Cargo environment.

    Returns
    -------
    str
        SHA-256 HMAC of compiler arguments and selected Cargo variables.

    Examples
    --------
    >>> from buildlimits.outcome_key import Salt
    >>> salt = Salt(b"s" * 32, "test")
    >>> first = argument_digest(("rustc", "--crate-name=serde"), {}, salt)
    >>> second = argument_digest(("other", "--crate-name=serde"), {}, salt)
    >>> first == second
    True
    """
    arguments = _normalized_arguments(argv[1:])
    target_value = _option_from_pairs(arguments, "--target")
    target_is_json = target_value is not None and target_value.endswith(".json")
    rest = []
    cfgs = []
    for argument in arguments:
        flag, _value = argument
        is_json_target = target_is_json and flag == "--target"
        if flag in _IGNORED_ARGUMENTS or is_json_target:
            continue
        (cfgs if flag == "--cfg" else rest).append(argument)
    canonical = json.dumps(
        {
            "argv": [
                *rest,
                *sorted(cfgs, key=lambda pair: (pair[1] is None, pair[1] or "")),
            ],
            "env": _cargo_environment(env),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hmac.new(salt.key, canonical.encode(), hashlib.sha256).hexdigest()


def _cargo_environment(env: cabc.Mapping[str, str]) -> list[tuple[str, str]]:
    """Return Cargo variables that participate in sccache's request key.

    Returns
    -------
    list[tuple[str, str]]
        Sorted key/value pairs, excluding variables ignored by sccache.
    """
    return sorted(
        (name, value) for name, value in env.items() if _is_hashed_cargo_variable(name)
    )


def _is_hashed_cargo_variable(name: str) -> bool:
    """Check the Cargo environment allowlist used for request identity.

    Returns
    -------
    bool
        Whether the variable is hashed by sccache for this key.
    """
    return all((
        name.startswith("CARGO_"),
        name not in _UNHASHED_CARGO_ENV,
        not name.startswith("CARGO_REGISTRIES_"),
    ))


def _normalized_arguments(argv: cabc.Sequence[str]) -> list[tuple[str, str | None]]:
    """Normalize option values into stable flag/value pairs.

    Returns
    -------
    list[tuple[str, str or None]]
        Arguments with separate and attached values represented consistently.
    """
    normalized: list[tuple[str, str | None]] = []
    index = 0
    while index < len(argv):
        argument = argv[index]
        if argument in _VALUE_OPTIONS:
            value = argv[index + 1] if index + 1 < len(argv) else None
            normalized.append((argument, value))
            index += 2 if value is not None else 1
        elif argument.startswith("--") and "=" in argument:
            option, _, value = argument.partition("=")
            normalized.append((option, value))
            index += 1
        elif _has_attached_short_value(argument):
            normalized.append((argument[:2], argument[2:]))
            index += 1
        else:
            normalized.append((argument, None))
            index += 1
    return normalized


def _has_attached_short_value(argument: str) -> bool:
    """Check whether an argument uses a supported attached short value.

    Returns
    -------
    bool
        Whether the argument starts with a value-taking short option.
    """
    prefixes = ("-A", "-C", "-D", "-F", "-L", "-W", "-Z", "-l")
    return argument.startswith(prefixes) and len(argument) > _SHORT_OPTION_PREFIX_LENGTH


def _option_from_pairs(
    arguments: cabc.Sequence[tuple[str, str | None]], option: str
) -> str | None:
    """Read the first normalized value for one option.

    Returns
    -------
    str or None
        The first matching option value, if present.
    """
    return next((value for flag, value in arguments if flag == option), None)
