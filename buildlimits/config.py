"""Supervisor configuration and the runtime paths the adapters share.

The supervisor reads ``~/.config/buildlimits/supervisor.toml``, which the
provisioning role writes. Every key is optional, so an unconfigured
supervisor uses the defaults below. The adapters read nothing but the
environment: ``BUILD_LIMITS_SOCKET`` overrides the supervisor socket, and
``BUILD_LIMITS_HEARTBEAT_SECS`` the heartbeat interval.

Sockets live under ``$XDG_RUNTIME_DIR`` because Unix socket paths are limited
to about 108 bytes.
"""

import dataclasses as dc
import os
import tomllib
import typing as typ
from pathlib import Path

from buildlimits import cache_modes

if typ.TYPE_CHECKING:
    import collections.abc as cabc

# Derived rather than restated: ``cache_modes.MODES`` is the only place a mode
# string is read, so a mode accepted here but missing there -- which would pass
# validation and then compose no backend at all -- cannot be introduced.
# ``direct`` leads, then the caching modes in the order the table declares them.
CACHE_MODES: tuple[str, ...] = (
    "direct",
    *(mode for mode in cache_modes.MODES if mode != "direct"),
)
CACHE_RECOVERY_POLICIES = ("manual", "starved")
_CACHE_MODE_ERROR = f"cache_mode must be one of {', '.join(CACHE_MODES)}"

# Settings that only mean something in one cache mode. ``cache_recovery`` is
# deliberately absent: ``starved`` is the default, so rejecting it in kache
# mode would make the default configuration invalid on every kache host. It
# stays accepted and inert instead; see ADR 005.
_MODE_KEYS: dict[str, frozenset[str]] = {
    "sccache": frozenset({"sccache_executable", "sccache_unit", "sccache_socket"}),
    "kache": frozenset({"kache_executable", "kache_socket", "kache_cache_dir"}),
}


class ConfigError(Exception):
    """The supervisor configuration is invalid."""


def runtime_dir(env: cabc.Mapping[str, str] = os.environ) -> Path:
    """Locate the per-user runtime directory for the supervisor's sockets.

    Parameters
    ----------
    env : Mapping[str, str]
        The environment to consult.

    Returns
    -------
    Path
        ``$XDG_RUNTIME_DIR/buildlimits``, or ``/run/user/<uid>/buildlimits``
        when the variable is unset, as it can be in agent subprocesses.
    """
    base = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(base) / "buildlimits"


def supervisor_socket(env: cabc.Mapping[str, str] = os.environ) -> Path:
    """Locate the supervisor's socket.

    Parameters
    ----------
    env : Mapping[str, str]
        The environment to consult.

    Returns
    -------
    Path
        ``BUILD_LIMITS_SOCKET`` when set, otherwise the default path.
    """
    override = env.get("BUILD_LIMITS_SOCKET")
    return Path(override) if override else runtime_dir(env) / "supervisor.sock"


@dc.dataclass(frozen=True, slots=True)
class SupervisorConfig:
    """The supervisor's settings.

    Attributes
    ----------
    device : Path
        The token device.
    slots : int
        The pool's configured capacity, reported by status.
    socket_path : Path
        Where the supervisor listens.
    lock_path : Path
        The singleton lock.
    compilers_slice : str
        The slice unit that holds every compiler scope.
    cancel_grace_seconds : float
        Seconds between ``SIGTERM`` and ``SIGKILL`` for a terminated group.
    drain_timeout_seconds : float
        The longest start-up waits for surviving groups to drain.
    cache_mode : str
        ``sccache`` to compose admitted compilers with the managed server,
        ``kache`` to compose them with an unowned kache daemon, or ``direct``
        to run them without a cache.
    sccache_executable : str
        The sccache client, when ``cache_mode`` is ``sccache``.
    sccache_unit : str
        The managed server's user unit.
    sccache_socket : Path
        The managed server's socket.
    cache_recovery : str
        ``manual`` to recover the managed server only when an operator asks,
        or ``starved`` to also recover it when a waiting request cannot
        progress because the server retains detached work. It is accepted and
        inert in every mode but ``sccache``.
    recovery_marker : Path
        The restart barrier. The managed service refuses to start while this
        path exists, so recovery cannot leave the cache running with its
        detached work unaccounted.
    kache_executable : str
        The kache client, when ``cache_mode`` is ``kache``.
    kache_socket : Path or None
        The separately managed kache daemon's socket, exported to every
        admitted group as ``KACHE_SOCKET_PATH``. ``None`` lets kache resolve
        its own default socket.
    kache_cache_dir : Path or None
        The kache store health checks, exported to every admitted group as
        ``KACHE_CACHE_DIR``. ``None`` leaves the check to kache's own
        resolution, under ``$XDG_CACHE_HOME/kache`` or ``~/.cache/kache``.
        Health cannot check a path the supervisor was never told, so a host
        that moves the store should name it here.
    """

    device: Path = Path("/dev/guild")
    slots: int = 2
    socket_path: Path = dc.field(default_factory=supervisor_socket)
    lock_path: Path = dc.field(
        default_factory=lambda: runtime_dir() / "supervisor.lock"
    )
    compilers_slice: str = "buildlimits-compilers.slice"
    cancel_grace_seconds: float = 10.0
    drain_timeout_seconds: float = 120.0
    cache_mode: str = "sccache"
    sccache_executable: str = "sccache"
    sccache_unit: str = "buildlimits-sccache.service"
    sccache_socket: Path = dc.field(
        default_factory=lambda: runtime_dir() / "sccache.sock"
    )
    cache_recovery: str = "starved"
    recovery_marker: Path = dc.field(
        default_factory=lambda: runtime_dir() / "cache-recovery"
    )
    kache_executable: str = "kache"
    # None means "let kache find its own daemon"; the probe and every composed
    # group export nothing, so kache's own resolution order applies.
    kache_socket: Path | None = None
    # None means "kache's own store"; health cannot check it, so a host that
    # moves the store between filesystems must name it here.
    kache_cache_dir: Path | None = None


def load(path: Path | None) -> SupervisorConfig:
    """Load the supervisor configuration.

    Parameters
    ----------
    path : Path | None
        The TOML file, or ``None`` for defaults only. A missing file also
        yields the defaults.

    Returns
    -------
    SupervisorConfig
        The validated configuration.

    Raises
    ------
    ConfigError
        If the file is malformed or a value is out of range.

    Examples
    --------
    >>> load(None).cache_mode
    'sccache'
    """
    if path is None or not path.exists():
        return validate(SupervisorConfig())
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as error:
        msg = f"cannot read {path}: {error}"
        raise ConfigError(msg) from error
    return validate(from_mapping(raw))


def from_mapping(raw: cabc.Mapping[str, object]) -> SupervisorConfig:
    """Build a configuration from parsed TOML.

    Parameters
    ----------
    raw : Mapping[str, object]
        The parsed document.

    Returns
    -------
    SupervisorConfig
        The configuration, not yet validated.

    Raises
    ------
    ConfigError
        If a key is unknown or has the wrong type.
    """
    fields = {field.name: field for field in dc.fields(SupervisorConfig)}
    values: dict[str, object] = {}
    for key, value in raw.items():
        if key not in fields:
            msg = f"unknown setting {key!r}"
            raise ConfigError(msg)
        values[key] = _coerce(key, fields[key].type, value)
    _reject_foreign_keys(values)
    # _coerce has converted each value to its field's declared type.
    return SupervisorConfig(**typ.cast("dict[str, typ.Any]", values))


def _reject_foreign_keys(values: cabc.Mapping[str, object]) -> None:
    """Reject a written setting that the chosen cache mode cannot use.

    Every key is optional, so what is invalid is decided by the keys the
    operator actually wrote, not by a difference from the defaults. A key that
    would look accepted and then do nothing is worse than an error: it hides a
    mode that was not the one intended.

    Only a clash between two cache backends is an error. ``direct`` names no
    backend, so it owns no keys and a backend's settings are merely inert
    there, as ``sccache_socket`` has always been.

    Parameters
    ----------
    values : Mapping[str, object]
        The converted settings, keyed as written in the document.

    Raises
    ------
    ConfigError
        If one backend's setting was written while another backend is
        selected.
    """
    mode = values.get("cache_mode", "sccache")
    if mode not in _MODE_KEYS:
        # ``validate`` owns an unknown mode's message, and ``direct`` owns no
        # keys to clash with.
        return
    for owner, keys in _MODE_KEYS.items():
        if owner == mode:
            continue
        for key in keys & values.keys():
            msg = (
                f"setting {key!r} is only valid when cache_mode is "
                f"{owner!r}, not {mode!r}"
            )
            raise ConfigError(msg)


def validate(config: SupervisorConfig) -> SupervisorConfig:
    """Check value ranges.

    Parameters
    ----------
    config : SupervisorConfig
        The configuration to check.

    Returns
    -------
    SupervisorConfig
        The same configuration.

    Raises
    ------
    ConfigError
        If a value is out of range.
    """
    broken = [message for holds, message in _RANGE_RULES if not holds(config)]
    if broken:
        raise ConfigError(broken[0])
    return config


_RANGE_RULES: tuple[tuple[cabc.Callable[[SupervisorConfig], bool], str], ...] = (
    (lambda config: config.slots >= 1, "slots must be a positive integer"),
    (lambda config: config.cache_mode in CACHE_MODES, _CACHE_MODE_ERROR),
    (
        lambda config: config.cache_recovery in CACHE_RECOVERY_POLICIES,
        (f"cache_recovery must be one of {', '.join(CACHE_RECOVERY_POLICIES)}"),
    ),
    (
        lambda config: (
            min(config.cancel_grace_seconds, config.drain_timeout_seconds) > 0
        ),
        "grace and drain timeouts must be positive",
    ),
    (
        lambda config: config.compilers_slice.endswith(".slice"),
        "compilers_slice must name a slice unit",
    ),
)


def _coerce(key: str, annotation: object, value: object) -> object:
    """Check and convert one TOML value to its field's type.

    Returns
    -------
    object
        The converted value.

    Raises
    ------
    ConfigError
        If the value does not have the field's type.
    """
    # Python 3.14 evaluates annotations lazily, so dataclass fields carry the
    # field's type object rather than its source text. A ``X | None`` field
    # carries a ``types.UnionType``, which is not a ``type``, so membership is
    # asked of the table rather than gated on ``isinstance``.
    converter = _CONVERTERS.get(annotation)
    converted = converter(value) if converter is not None else None
    if converted is None:
        msg = f"setting {key!r} has the wrong type"
        raise ConfigError(msg)
    return converted


def _is_number(value: object) -> bool:
    """Report whether a TOML value is a number; TOML booleans are not.

    Returns
    -------
    bool
        Whether the value is an integer or a float.
    """
    return isinstance(value, int | float) and not isinstance(value, bool)


# Each converter returns None for a value of the wrong type; no setting is
# ever None, so None cannot be confused with a converted value. The key type is
# ``object`` because a ``X | None`` field's annotation is a union object.
_CONVERTERS: dict[object, cabc.Callable[[object], object]] = {
    Path: lambda value: Path(value) if isinstance(value, str) else None,
    Path | None: lambda value: Path(value) if isinstance(value, str) else None,
    int: lambda value: value if _is_number(value) and isinstance(value, int) else None,
    float: lambda value: float(typ.cast("float", value)) if _is_number(value) else None,
    str: lambda value: value if isinstance(value, str) else None,
}
