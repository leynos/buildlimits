"""Generated privacy and stability properties for normalized outcome keys."""

from types import ModuleType

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from outcome_test_helpers import outcome_module, record_generated_case

_PATH_COMPONENT = st.text(
    alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=8, max_size=16
)


def _salt(module: ModuleType) -> object:
    """Return a fixed salt for deterministic generated comparisons."""
    return module.Salt(key=b"p" * 32, salt_id="property-salt")


@settings(max_examples=60, deadline=None)
@pytest.mark.parametrize("origin_kind", ["registry", "git", "path"])
@given(components=st.lists(_PATH_COMPONENT, min_size=2, max_size=5))
def test_generated_origins_are_classified_and_private(
    origin_kind: str, components: list[str]
) -> None:
    """Generated registry, Git and local origins retain only allowed values."""
    key = outcome_module("outcome_key")
    salt = _salt(key)
    crate_name = f"serde-{components[0]}"
    match origin_kind:
        case "registry":
            version = "1.2.3"
            manifest = f"/home/alice/.cargo/registry/src/index/{crate_name}-{version}"
            environment = {
                "HOME": "/home/alice",
                "CARGO_MANIFEST_DIR": manifest,
                "CARGO_PKG_NAME": crate_name,
                "CARGO_PKG_VERSION": version,
            }
            expected = f"registry:{crate_name}@{version}"
            is_path_origin = False
        case "git":
            revision = components[1]
            manifest = (
                f"/home/alice/.cargo/git/checkouts/{crate_name}-abcdef0123456789/"
                f"rev-{revision}"
            )
            environment = {
                "HOME": "/home/alice",
                "CARGO_MANIFEST_DIR": manifest,
                "CARGO_PKG_NAME": crate_name,
            }
            expected = f"git:{crate_name}@rev-{revision}"
            is_path_origin = False
        case "path":
            manifest = "/" + "/".join(components)
            environment = {"CARGO_MANIFEST_DIR": manifest}
            expected = "path:"
            is_path_origin = True
        case _:
            pytest.fail(f"unrecognized generated origin kind: {origin_kind}")

    origin = key.crate_origin(environment, salt)

    if is_path_origin:
        assert origin is not None and origin.startswith(expected), (
            "a local directory receives a path-keyed origin"
        )
        assert manifest not in origin, "the full manifest path is hidden"
        for component in components:
            assert component not in origin, "no generated path component is retained"
    else:
        assert origin == expected, f"{origin_kind} metadata maps to its stable origin"
    record_generated_case(f"origin:{origin_kind}")


@settings(max_examples=60, deadline=None)
@given(compiler=_PATH_COMPONENT, rustup=_PATH_COMPONENT)
def test_toolchain_path_components_are_hidden(compiler: str, rustup: str) -> None:
    """Neither compiler nor rustup path segments enter the identity value."""
    key = outcome_module("outcome_key")
    salt = _salt(key)
    compiler_path = f"/toolchains/{compiler}/bin/rustc"
    rustup_path = f"/rustup/{rustup}/toolchain"
    identity = key.toolchain_identity(
        compiler_path, {"RUSTUP_TOOLCHAIN": rustup_path}, "0.14.0", salt
    )
    rendered = repr(identity)

    assert compiler not in rendered, "compiler path components stay hidden"
    assert rustup not in rendered, "rustup path components stay hidden"
    assert "/" not in identity.compiler_digest, "compiler digest is path-free"
    assert "/" not in identity.rustup_toolchain_digest, "rustup digest is path-free"
    record_generated_case(
        f"toolchain-paths:{'hidden' if compiler not in rendered and rustup not in rendered else 'leaked'}"
    )


@settings(max_examples=60, deadline=None)
@given(target_path=st.lists(_PATH_COMPONENT, min_size=1, max_size=4))
def test_target_path_output_is_path_free(target_path: list[str]) -> None:
    """A target JSON path contributes only an HMAC digest to its key field."""
    key = outcome_module("outcome_key")
    salt = _salt(key)
    path = "/" + "/".join(target_path) + "/target.json"
    result = key.target(("/toolchain/rustc", "--target", path), salt)

    assert result.startswith("path:"), "a JSON target file has a path-keyed label"
    assert path not in result, "the target path is hidden"
    for component in target_path:
        assert component not in result, "target path components are hidden"
    record_generated_case(f"target:{result.split(':', maxsplit=1)[0]}")
