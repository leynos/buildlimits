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

if typ.TYPE_CHECKING:
    import collections.abc as cabc

CACHE_MODES = ("sccache", "direct")
CACHE_RECOVERY_POLICIES = ("manual", "starved")
CACHE_OUTCOMES = ("off", "record")


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
        or ``direct``.
    sccache_executable : str
        The sccache client, when ``cache_mode`` is ``sccache``.
    sccache_unit : str
        The managed server's user unit.
    sccache_socket : Path
        The managed server's socket.
    cache_recovery : str
        ``manual`` to recover the managed server only when an operator asks,
        or ``starved`` to also recover it when a waiting request cannot
        progress because the server retains detached work.
    cache_outcomes : str
        ``off`` to leave outcome recording disabled, or ``record`` to enable
        it when the build supports the recording integration.
    recovery_marker : Path
        The restart barrier. The managed service refuses to start while this
        path exists, so recovery cannot leave the cache running with its
        detached work unaccounted.
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
    cache_outcomes: str = "off"
    recovery_marker: Path = dc.field(
        default_factory=lambda: runtime_dir() / "cache-recovery"
    )


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
    # _coerce has converted each value to its field's declared type.
    return SupervisorConfig(**typ.cast("dict[str, typ.Any]", values))


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
    (
        lambda config: config.cache_mode in CACHE_MODES,
        f"cache_mode must be one of {', '.join(CACHE_MODES)}",
    ),
    (
        lambda config: config.cache_recovery in CACHE_RECOVERY_POLICIES,
        (f"cache_recovery must be one of {', '.join(CACHE_RECOVERY_POLICIES)}"),
    ),
    (
        lambda config: config.cache_outcomes in CACHE_OUTCOMES,
        f"cache_outcomes must be one of {', '.join(CACHE_OUTCOMES)}",
    ),
    (
        lambda config: (
            config.cache_outcomes != "record" or config.cache_mode == "sccache"
        ),
        'cache_outcomes="record" requires cache_mode="sccache"',
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
    # field's type object rather than its source text.
    converter = _CONVERTERS.get(annotation) if isinstance(annotation, type) else None
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
# ever None, so None cannot be confused with a converted value.
_CONVERTERS: dict[type, cabc.Callable[[object], object]] = {
    Path: lambda value: Path(value) if isinstance(value, str) else None,
    int: lambda value: value if _is_number(value) and isinstance(value, int) else None,
    float: lambda value: float(typ.cast("float", value)) if _is_number(value) else None,
    str: lambda value: value if isinstance(value, str) else None,
}
