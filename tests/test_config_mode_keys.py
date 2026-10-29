"""Configuration loading: defaults, overrides and backend-specific keys.

These live apart from the transport ports in ``test_ports.py`` because they
describe the written document rather than a systemd or cache port, and because
the per-backend key rules grow with each backend.
"""

from pathlib import Path

import pytest

from buildlimits import config


def test_defaults_load_without_a_file(tmp_path: Path) -> None:
    """A missing file yields the documented defaults."""
    settings = config.load(tmp_path / "absent.toml")
    assert settings.slots == 2, "the default slot count is 2"
    assert settings.cache_mode == "sccache", "the default cache mode is sccache"
    assert settings.compilers_slice == "buildlimits-compilers.slice", (
        "the default compilers slice matches the documented name"
    )


def test_files_override_defaults(tmp_path: Path) -> None:
    """Every setting can come from the file, with types checked."""
    path = tmp_path / "s.toml"
    path.write_text(
        'slots = 3\ndevice = "/dev/x"\ncancel_grace_seconds = 4\ncache_mode = "direct"\n'
    )
    settings = config.load(path)
    assert (settings.slots, settings.device, settings.cancel_grace_seconds) == (
        3,
        Path("/dev/x"),
        4.0,
    ), "every setting is read from the file"
    assert settings.cache_mode == "direct", (
        "the file's cache mode overrides the default"
    )


def _rejected(tmp_path: Path, text: str, fragment: str) -> None:
    """Write one document and assert loading it is refused as described."""
    path = tmp_path / "s.toml"
    path.write_text(text)
    with pytest.raises(config.ConfigError, match=fragment):
        config.load(path)


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("slots = 0\n", "positive"),
        ('cache_mode = "bypass"\n', "cache_mode"),
        ("slots = true\n", "wrong type"),
        ("colour = 1\n", "unknown setting"),
        ('compilers_slice = "x.service"\n', "slice"),
        ("slots = [\n", "cannot read"),
    ],
)
def test_invalid_files_are_rejected(tmp_path: Path, text: str, fragment: str) -> None:
    """Invalid configuration is reported, never half-applied."""
    _rejected(tmp_path, text, fragment)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ('cache_mode = "kache"\nsccache_unit = "x.service"\n', "sccache_unit"),
        ('cache_mode = "kache"\nsccache_socket = "/x.sock"\n', "sccache_socket"),
        ('cache_mode = "sccache"\nkache_socket = "/x.sock"\n', "kache_socket"),
        ('cache_mode = "sccache"\nkache_cache_dir = "/x"\n', "kache_cache_dir"),
    ],
)
def test_a_setting_the_chosen_mode_cannot_use_is_rejected(
    tmp_path: Path, text: str, key: str
) -> None:
    """A written setting that would do nothing is an error, not a silent no-op.

    Only a clash between two cache backends is rejected: ``direct`` names no
    backend, so a backend's settings are merely inert there.
    """
    _rejected(tmp_path, text, f"{key}.*only valid")


def test_kache_without_a_socket_is_rejected(tmp_path: Path) -> None:
    """A kache group must name the daemon health checks.

    kache resolves a default socket from its own environment, which for the
    group is the requesting build's and for the probe is the supervisor's. An
    unnamed socket lets the probe verify one daemon while the group selects
    another and starts a second one inside the admitted scope.
    """
    _rejected(tmp_path, 'cache_mode = "kache"\n', "kache_socket")
    _rejected(
        tmp_path,
        'cache_mode = "kache"\nkache_executable = "/b/kache"\n',
        "kache_socket",
    )


def test_kache_with_a_socket_loads(tmp_path: Path) -> None:
    """The documented kache configuration is accepted."""
    path = tmp_path / "s.toml"
    path.write_text(
        'cache_mode = "kache"\n'
        'kache_socket = "/run/user/1/buildlimits/kache.sock"\n'
        'kache_cache_dir = "/var/cache/kache"\n'
    )
    settings = config.load(path)
    assert settings.cache_mode == "kache", "the file selects the kache backend"
    assert settings.kache_socket == Path("/run/user/1/buildlimits/kache.sock"), (
        "the named daemon socket is read from the file"
    )


def test_a_backend_setting_is_inert_in_direct_mode(tmp_path: Path) -> None:
    """``direct`` owns no keys, so it does not reject another backend's."""
    path = tmp_path / "s.toml"
    path.write_text('cache_mode = "direct"\nkache_cache_dir = "/x"\n')

    assert config.load(path).cache_mode == "direct", (
        "direct names no backend and so has no key to clash with"
    )
