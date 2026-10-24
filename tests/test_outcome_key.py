"""Input normalization and HMAC privacy for cache-outcome keys."""

import pytest
from outcome_test_helpers import expected_hmac, outcome_module

pytestmark = pytest.mark.xfail(
    strict=True, reason="EP-M3 red tests precede the outcome key implementation"
)


def _salt() -> object:
    """Return a fixed test salt without importing a future module at collection."""
    return outcome_module("outcome_key").Salt(key=b"s" * 32, salt_id="fixed-salt")


def test_crate_name_accepts_separated_and_equals_forms() -> None:
    """Read both rustc spellings of the crate name from a request argv."""
    key = outcome_module("outcome_key")

    assert key.crate_name(("/toolchain/bin/rustc", "--crate-name", "serde")) == (
        "serde"
    ), "the separated crate-name form is recognized"
    assert key.crate_name(("/toolchain/bin/rustc", "--crate-name=serde")) == (
        "serde"
    ), "the equals crate-name form is recognized"


def test_crate_name_is_unavailable_when_missing_or_path_valued() -> None:
    """Missing names and path-like names cannot put a local path in the key."""
    key = outcome_module("outcome_key")

    assert key.crate_name(("/toolchain/bin/rustc", "src/lib.rs")) is None, (
        "a missing crate name is unavailable"
    )
    assert (
        key.crate_name((
            "/toolchain/bin/rustc",
            "--crate-name=/private/project",
            "src/lib.rs",
        ))
        is None
    ), "a path-like crate name is unavailable"


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("1.2.3-alpha.1", "registry:serde-json@1.2.3-alpha.1"),
        ("2.0.0-rc.3+build.4", "registry:serde-json@2.0.0-rc.3+build.4"),
    ],
)
def test_registry_origin_preserves_hyphenated_prerelease_versions(
    version: str, expected: str
) -> None:
    """Registry names come from Cargo metadata and retain semantic versions."""
    key = outcome_module("outcome_key")
    env = {
        "HOME": "/home/alice",
        "CARGO_MANIFEST_DIR": (
            f"/home/alice/.cargo/registry/src/index/serde-json-{version}"
        ),
        "CARGO_PKG_NAME": "serde-json",
        "CARGO_PKG_VERSION": version,
    }

    assert key.crate_origin(env, _salt()) == expected, (
        "the registry origin preserves the package version"
    )


def test_registry_version_mismatch_falls_back_to_a_salted_path() -> None:
    """A directory cannot claim registry identity with different metadata."""
    key = outcome_module("outcome_key")
    salt = _salt()
    manifest = "/home/alice/.cargo/registry/src/index/serde-1.2.3"
    env = {
        "HOME": "/home/alice",
        "CARGO_MANIFEST_DIR": manifest,
        "CARGO_PKG_NAME": "serde",
        "CARGO_PKG_VERSION": "9.9.9",
    }
    expected = expected_hmac(manifest, b"s" * 32)

    assert key.crate_origin(env, salt) == f"path:{expected}", (
        "mismatched registry metadata falls back to the salted path"
    )


def test_git_and_local_origins_never_retain_the_manifest_path() -> None:
    """Git checkouts normalize to a revision while other paths are HMACed."""
    key = outcome_module("outcome_key")
    salt = _salt()
    git_manifest = "/home/alice/.cargo/git/checkouts/serde-abcdef0123456789/rev-a"
    path_manifest = "/work/private/project"

    assert (
        key.crate_origin(
            {
                "HOME": "/home/alice",
                "CARGO_MANIFEST_DIR": git_manifest,
                "CARGO_PKG_NAME": "serde",
            },
            salt,
        )
        == "git:serde@rev-a"
    ), "the Git checkout origin uses its revision"
    path_origin = key.crate_origin({"CARGO_MANIFEST_DIR": path_manifest}, salt)
    assert path_origin is not None and path_origin.startswith("path:"), (
        "an unrecognized manifest is keyed"
    )
    assert path_manifest not in path_origin, "the local path is not retained"


def test_origin_uses_non_default_cargo_home_and_missing_manifest_is_unavailable() -> (
    None
):
    """Cargo home selection is explicit and a missing manifest has no origin."""
    key = outcome_module("outcome_key")
    env = {
        "CARGO_HOME": "/var/cache/cargo",
        "CARGO_MANIFEST_DIR": "/var/cache/cargo/registry/src/index/serde-1.2.3",
        "CARGO_PKG_NAME": "serde",
        "CARGO_PKG_VERSION": "1.2.3",
    }

    assert key.crate_origin(env, _salt()) == "registry:serde@1.2.3", (
        "the configured Cargo home identifies the registry origin"
    )
    assert key.crate_origin({"HOME": "/home/alice"}, _salt()) is None, (
        "a missing manifest has no origin"
    )


def test_target_uses_host_name_or_salted_json_path() -> None:
    """Target names remain readable while a JSON target path stays private."""
    key = outcome_module("outcome_key")
    salt = _salt()
    target_path = "/private/toolchains/custom-target.json"
    expected = expected_hmac(target_path, b"s" * 32)

    assert key.target(("/toolchain/bin/rustc",), salt) == "host", (
        "an omitted target selects the host"
    )
    assert (
        key.target(("/toolchain/bin/rustc", "--target=x86_64-unknown-linux-gnu"), salt)
        == "x86_64-unknown-linux-gnu"
    ), "a named target remains readable"
    assert (
        key.target(("/toolchain/bin/rustc", "--target", target_path), salt)
        == f"path:{expected}"
    ), "a JSON target path is represented by its salted digest"


def test_argument_digest_applies_sccache_removals_and_cfg_ordering() -> None:
    """Ignored flags and cfg ordering do not split the normalized digest."""
    key = outcome_module("outcome_key")
    salt = _salt()
    first = (
        "/toolchain/bin/rustc",
        "--crate-name=serde",
        "--cfg",
        "feature=alpha",
        "--cfg=feature=beta",
        "src/lib.rs",
        "--out-dir",
        "target/one",
        "--extern",
        "serde=target/one.rlib",
        "-L",
        "dependency=target/one",
        '--check-cfg=cfg(feature, values("alpha"))',
        "--diagnostic-width=80",
    )
    second = (
        "/another/rustc",
        "--crate-name=serde",
        "--cfg=feature=beta",
        "--cfg",
        "feature=alpha",
        "src/lib.rs",
        "--out-dir",
        "target/two",
        "--extern",
        "serde=target/two.rlib",
        "-L",
        "dependency=target/two",
        '--check-cfg=cfg(feature, values("beta"))',
        "--diagnostic-width=120",
    )

    assert key.argument_digest(first, {}, salt) == key.argument_digest(
        second, {}, salt
    ), "paths, removed flags and cfg order do not change the argument digest"


def test_argument_digest_includes_filtered_cargo_environment_under_hmac() -> None:
    """Relevant Cargo variables change the digest without exposing values."""
    key = outcome_module("outcome_key")
    salt = _salt()
    argv = ("/toolchain/bin/rustc", "--crate-name=serde", "src/lib.rs")
    baseline = {
        "CARGO_PKG_VERSION": "1.2.3",
        "CARGO_MAKEFLAGS": "make -j1",
        "CARGO_BUILD_JOBS": "1",
        "CARGO_REGISTRIES_MIRROR": "registry-setting",
    }

    first = key.argument_digest(argv, baseline, salt)
    ignored_changes = dict(
        baseline,
        CARGO_MAKEFLAGS="make -j8",
        CARGO_BUILD_JOBS="8",
        CARGO_REGISTRIES_MIRROR="other-registry-setting",
    )

    assert first == key.argument_digest(argv, ignored_changes, salt), (
        "the documented Cargo exclusions do not enter the digest"
    )
    assert first != key.argument_digest(
        argv, dict(baseline, CARGO_PKG_VERSION="9.9.9"), salt
    ), "a hashed Cargo variable changes the digest"
    assert "registry-setting" not in first, "environment values are never stored"


def test_toolchain_identity_hmacs_compiler_and_rustup_values() -> None:
    """Identity changes with its inputs but never contains compiler paths."""
    key = outcome_module("outcome_key")
    salt = _salt()
    identity = key.toolchain_identity(
        "/private/toolchain/bin/rustc", {}, "0.14.0", salt
    )

    assert identity.compiler_digest != "/private/toolchain/bin/rustc", (
        "the compiler path is not stored in clear text"
    )
    assert identity.rustup_toolchain_digest == expected_hmac("", b"s" * 32), (
        "an absent rustup toolchain value has a stable empty digest"
    )
    assert identity.cache_version == "6", "the identity records cache format version 6"
    assert identity.server_version == "0.14.0", "the identity records sccache version"
    assert (
        key.toolchain_identity(
            "/private/toolchain/bin/rustc",
            {"RUSTUP_TOOLCHAIN": "/private/rustup/stable"},
            "0.14.0",
            salt,
        ).rustup_toolchain_digest
        != identity.rustup_toolchain_digest
    ), "a changed rustup toolchain value changes its digest"
