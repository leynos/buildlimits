"""Path resolution and file-salt privacy contracts for cache outcomes."""

import hashlib
import hmac
import stat
from pathlib import Path
from unittest import mock

import pytest

from buildlimits.outcome_key import Salt
from buildlimits.outcome_ports import StoreFailure
from buildlimits.outcome_store import file_salt, store_path

_SALT_NAME = "cache-outcomes.salt"


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        (
            {"XDG_STATE_HOME": "/state", "HOME": "/home/test"},
            Path("/state/buildlimits/cache-outcomes.jsonl"),
        ),
        (
            {"HOME": "/home/test"},
            Path("/home/test/.local/state/buildlimits/cache-outcomes.jsonl"),
        ),
        (
            {"XDG_STATE_HOME": "relative", "HOME": "/home/test"},
            Path("/home/test/.local/state/buildlimits/cache-outcomes.jsonl"),
        ),
    ],
    ids=("xdg-state-home", "default", "relative-xdg-is-ignored"),
)
def test_store_path_uses_xdg_state_home_or_the_home_default(
    environment: dict[str, str], expected: Path
) -> None:
    """Resolve a stable per-user store path without reading process state."""
    assert store_path(environment) == expected, "the selected state root is explicit"


def test_file_salt_creates_private_stable_key_and_identifier(tmp_path: Path) -> None:
    """Create one 32-byte salt and derive its non-secret HMAC identifier."""
    salt_path = tmp_path / "buildlimits" / _SALT_NAME

    first = file_salt(salt_path)
    second = file_salt(salt_path)

    assert isinstance(first, Salt), "salt creation succeeds"
    assert second == first, "a later read keeps the original key"
    assert len(first.key) == 32, "the per-host key has the fixed length"
    assert (
        first.salt_id
        == hmac.new(first.key, b"buildlimits-salt-id", hashlib.sha256).hexdigest()
    ), "the public identifier is a keyed digest"
    assert stat.S_IMODE(salt_path.stat().st_mode) == 0o600, "the salt is private"
    assert stat.S_IMODE(salt_path.parent.stat().st_mode) == 0o700, (
        "the state directory is private"
    )


def test_file_salt_does_not_replace_a_key_installed_during_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent initializer's winning salt remains authoritative."""
    salt_path = tmp_path / "buildlimits" / _SALT_NAME
    winning_key = b"w" * 32

    def link_winner(_temporary: Path, destination: Path) -> None:
        """Model the atomic no-clobber link finding an existing winner."""
        destination.write_bytes(winning_key)
        destination.chmod(0o600)
        raise FileExistsError(destination)

    monkeypatch.setattr("buildlimits.outcome_store_salt.os.link", link_winner)

    result = file_salt(salt_path)

    expected_id = hmac.new(
        winning_key, b"buildlimits-salt-id", hashlib.sha256
    ).hexdigest()
    assert result == Salt(winning_key, expected_id), "the competing key wins creation"
    assert salt_path.read_bytes() == winning_key, "the installed key is not replaced"


def test_file_salt_does_not_regenerate_a_malformed_existing_key(
    tmp_path: Path,
) -> None:
    """A malformed existing key fails closed and remains untouched."""
    salt_path = tmp_path / _SALT_NAME
    salt_path.write_bytes(b"short")

    result = file_salt(salt_path)

    assert result == StoreFailure("salt_unavailable"), "bad key material is unavailable"
    assert salt_path.read_bytes() == b"short", "an unreadable key is never replaced"


def test_file_salt_does_not_regenerate_a_key_when_read_is_refused(
    tmp_path: Path,
) -> None:
    """A read error leaves the original salt file in place."""
    salt_path = tmp_path / _SALT_NAME
    original = b"s" * 32
    salt_path.write_bytes(original)

    with mock.patch(
        "buildlimits.outcome_store_salt.os.open",
        side_effect=PermissionError("read refused"),
    ):
        result = file_salt(salt_path)

    assert result == StoreFailure("salt_unavailable"), "a refused read fails closed"
    assert salt_path.read_bytes() == original, "the inaccessible key is preserved"


def test_file_salt_rejects_a_directory_at_the_salt_path(tmp_path: Path) -> None:
    """A non-file salt path is not repaired by generating a new key."""
    salt_path = tmp_path / _SALT_NAME
    salt_path.mkdir()

    result = file_salt(salt_path)

    assert result == StoreFailure("salt_unavailable"), (
        "the invalid salt stays unavailable"
    )
    assert salt_path.is_dir(), "the existing object is preserved"


def test_file_salt_does_not_replace_a_dangling_symlink(tmp_path: Path) -> None:
    """An existing unreadable filesystem object is not mistaken for no salt."""
    salt_path = tmp_path / _SALT_NAME
    salt_path.symlink_to(tmp_path / "missing-key")

    result = file_salt(salt_path)

    assert result == StoreFailure("salt_unavailable"), "the link is unavailable"
    assert salt_path.is_symlink(), "the existing link is not replaced"


def test_file_salt_rejects_a_relative_path_without_changing_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A relative path cannot make salt creation chmod the current directory."""
    monkeypatch.chdir(tmp_path)
    current_mode = stat.S_IMODE(tmp_path.stat().st_mode)

    result = file_salt(Path("cache-outcomes.salt"))

    assert result == StoreFailure("salt_unavailable"), "relative paths fail closed"
    assert stat.S_IMODE(tmp_path.stat().st_mode) == current_mode, (
        "the caller's working directory mode is unchanged"
    )
