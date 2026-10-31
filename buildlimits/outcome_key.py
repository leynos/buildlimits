"""Pure, salted cache-outcome request keys with paths hidden."""

import dataclasses as dc
import hashlib
import hmac
import typing as typ
from pathlib import PurePosixPath

from buildlimits.outcome_key_arguments import (
    _normalized_arguments as _normalized_arguments,
)
from buildlimits.outcome_key_arguments import (
    _option_from_pairs as _option_from_pairs,
)
from buildlimits.outcome_key_arguments import (
    argument_digest as argument_digest,
)

if typ.TYPE_CHECKING:
    import collections.abc as cabc

_CARGO_SOURCE_PATH_PARTS = 4


@dc.dataclass(frozen=True, slots=True)
class Salt:
    """The local key material and its non-secret rotation identifier."""

    key: bytes
    salt_id: str


@dc.dataclass(frozen=True, slots=True)
class ToolchainIdentity:
    """An approximate toolchain identity without clear-text paths."""

    compiler_digest: str
    rustup_toolchain_digest: str
    cache_version: str
    server_version: str


@dc.dataclass(frozen=True, slots=True)
class CacheKey:
    """Normalized request identity used by outcome history."""

    crate_name: str
    crate_origin: str | None
    target: str
    argument_digest: str
    toolchain_identity: ToolchainIdentity


def _crate_name_option(argv: cabc.Sequence[str]) -> str | None:
    """Read the raw value of either rustc crate-name spelling.

    Returns
    -------
    str or None
        The option value, or no value when the option is absent.

    Examples
    --------
    >>> _crate_name_option(("rustc", "--crate-name", "serde"))
    'serde'
    >>> _crate_name_option(("rustc",)) is None
    True
    """
    for index, argument in enumerate(argv[1:], start=1):
        if argument == "--crate-name" and index + 1 < len(argv):
            return argv[index + 1]
        if argument.startswith("--crate-name="):
            return argument.partition("=")[2]
    return None


def _is_safe_crate_name(value: str) -> bool:
    """Check that a crate name cannot reveal a local path.

    Returns
    -------
    bool
        Whether the value is a non-empty name without path separators.

    Examples
    --------
    >>> _is_safe_crate_name("serde")
    True
    >>> _is_safe_crate_name("/private/project")
    False
    """
    is_named = value not in {"", ".", ".."}
    has_separator = any(separator in value for separator in ("/", "\\"))
    return is_named and not has_separator


def crate_name(argv: cabc.Sequence[str]) -> str | None:
    """Read a safe crate name from either rustc option spelling.

    Returns
    -------
    str or None
        The crate name, or no value for a missing or path-like value.

    Examples
    --------
    >>> crate_name(("rustc", "--crate-name=serde"))
    'serde'
    >>> crate_name(("rustc", "--crate-name", "/private/project")) is None
    True
    """
    value = _crate_name_option(argv)
    if value is None or not _is_safe_crate_name(value):
        return None
    return value


def _cargo_home(env: cabc.Mapping[str, str]) -> str | None:
    """Resolve Cargo's source cache root from the environment.

    Returns
    -------
    str or None
        The configured or default cache root, when one can be resolved.

    Examples
    --------
    >>> _cargo_home({"CARGO_HOME": "/cache"})
    '/cache'
    >>> _cargo_home({}) is None
    True
    """
    cargo_home = env.get("CARGO_HOME")
    if cargo_home:
        return cargo_home
    home = env.get("HOME")
    return (PurePosixPath(home) / ".cargo").as_posix() if home else None


def _source_origin(parts: tuple[str, ...], env: cabc.Mapping[str, str]) -> str | None:
    """Match a Cargo checkout against its registry or Git layout.

    Returns
    -------
    str or None
        A stable source label, or no value for an unrecognized layout.

    Examples
    --------
    >>> _source_origin(("registry", "src", "index", "serde-1.2.3"), {}) is None
    True
    """
    return _registry_origin(parts, env) or _git_origin(parts, env)


def crate_origin(env: cabc.Mapping[str, str], salt: Salt) -> str | None:
    """Classify a Cargo source origin without retaining its local path.

    Returns
    -------
    str or None
        A registry or Git label, a keyed path label, or no value when Cargo
        supplied no manifest directory.

    Examples
    --------
    >>> salt = Salt(key=b"s" * 32, salt_id="test")
    >>> crate_origin({"CARGO_MANIFEST_DIR": "/work/project"}, salt).startswith("path:")
    True
    """
    manifest = env.get("CARGO_MANIFEST_DIR")
    if manifest is None:
        return None

    cargo_home = _cargo_home(env)
    relative = _relative_to(manifest, cargo_home) if cargo_home else None
    if relative is not None:
        origin = _source_origin(PurePosixPath(relative).parts, env)
        if origin is not None:
            return origin
    return f"path:{_hmac_digest(manifest, salt)}"


def target(argv: cabc.Sequence[str], salt: Salt) -> str:
    """Return the host, a target triple or a keyed target-path label.

    Returns
    -------
    str
        A target name or an HMAC label that does not reveal a local path.

    Examples
    --------
    >>> target(("rustc",), Salt(b"s" * 32, "test"))
    'host'
    >>> target(("rustc", "--target=x86_64-unknown-linux-gnu"), Salt(b"s" * 32, "test"))
    'x86_64-unknown-linux-gnu'
    """
    arguments = _normalized_arguments(argv[1:])
    value = _option_from_pairs(arguments, "--target")
    if value is None:
        return "host"
    if value.endswith(".json") or _is_path_like(value):
        return f"path:{_hmac_digest(value, salt)}"
    return value


def toolchain_identity(
    compiler: str,
    env: cabc.Mapping[str, str],
    server_version: str,
    salt: Salt,
) -> ToolchainIdentity:
    """Build a path-free identity from the compiler and rustup selection.

    Returns
    -------
    ToolchainIdentity
        Separate keyed digests and the sccache/server versions.

    Examples
    --------
    >>> identity = toolchain_identity(
    ...     "/toolchain/rustc", {}, "0.14.0", Salt(b"s" * 32, "test")
    ... )
    >>> "/" not in identity.compiler_digest
    True
    """
    return ToolchainIdentity(
        compiler_digest=_hmac_digest(compiler, salt),
        rustup_toolchain_digest=_hmac_digest(env.get("RUSTUP_TOOLCHAIN", ""), salt),
        cache_version="6",
        server_version=server_version,
    )


def _hmac_digest(value: str, salt: Salt) -> str:
    """Return the salted digest of one sensitive value.

    Returns
    -------
    str
        Lowercase SHA-256 HMAC hex.

    Examples
    --------
    >>> len(_hmac_digest("path", Salt(b"s" * 32, "test")))
    64
    """
    return hmac.new(salt.key, value.encode(), hashlib.sha256).hexdigest()


def _relative_to(path: str, parent: str) -> str | None:
    """Return the suffix below an exact parent path boundary.

    Returns
    -------
    str or None
        Relative suffix, or no value when the path is outside the parent.

    Examples
    --------
    >>> _relative_to("/cache/pkg", "/cache")
    'pkg'
    """
    parent_path = PurePosixPath(parent) if parent.strip("/") else PurePosixPath("/")
    path_value = PurePosixPath(path)
    if parent_path == PurePosixPath("/") and path_value.anchor == "//":
        path_value = PurePosixPath("/", *path_value.parts[1:])
    try:
        relative = path_value.relative_to(parent_path)
    except ValueError:
        return None
    return relative.as_posix() if relative.parts else None


def _registry_origin(parts: tuple[str, ...], env: cabc.Mapping[str, str]) -> str | None:
    """Match Cargo's registry source layout against package metadata.

    Returns
    -------
    str or None
        Stable registry label when path and Cargo package metadata agree.

    Examples
    --------
    >>> _registry_origin(
    ...     ("registry", "src", "index", "serde-1.2.3"),
    ...     {"CARGO_PKG_NAME": "serde", "CARGO_PKG_VERSION": "1.2.3"},
    ... )
    'registry:serde@1.2.3'
    """
    name, version = env.get("CARGO_PKG_NAME"), env.get("CARGO_PKG_VERSION")
    if len(parts) != _CARGO_SOURCE_PATH_PARTS or parts[:2] != ("registry", "src"):
        return None
    if not name or not version:
        return None
    if parts[3] != f"{name}-{version}":
        return None
    return f"registry:{name}@{version}"


def _git_origin(parts: tuple[str, ...], env: cabc.Mapping[str, str]) -> str | None:
    """Match Cargo's Git checkout layout using package name and revision.

    Returns
    -------
    str or None
        Stable Git label when the checkout layout identifies its package.

    Examples
    --------
    >>> _git_origin(
    ...     ("git", "checkouts", "serde-abc123", "rev-a"),
    ...     {"CARGO_PKG_NAME": "serde"},
    ... )
    'git:serde@rev-a'
    """
    name = env.get("CARGO_PKG_NAME")
    if len(parts) != _CARGO_SOURCE_PATH_PARTS or parts[:2] != ("git", "checkouts"):
        return None
    checkout, revision = parts[2], parts[3]
    if not name or not revision:
        return None
    if not checkout.startswith(f"{name}-"):
        return None
    if len(checkout) <= len(name) + 1:
        return None
    return f"git:{name}@{revision}"


def _is_path_like(value: str) -> bool:
    """Identify path-valued options without filesystem access.

    Returns
    -------
    bool
        Whether the value has a local path spelling.

    Examples
    --------
    >>> _is_path_like("target.json")
    False
    """
    windows_drive = len(value) > 1 and value[1] == ":"
    return (
        "/" in value or "\\" in value or value.startswith((".", "~")) or windows_drive
    )
