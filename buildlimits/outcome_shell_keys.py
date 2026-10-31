"""Build privacy-safe cache keys from one cached compiler launch."""

import typing as typ

from buildlimits import outcome_key

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_key import CacheKey, Salt

_SUPPORTED_SERVER_VERSION = "0.14.0"


def cache_key(
    argv: cabc.Sequence[str], env: cabc.Mapping[str, str], salt: Salt | None
) -> CacheKey | None:
    """Construct a salted key only when crate identity and salt are available.

    Returns
    -------
    CacheKey | None
        A privacy-safe key, or None until local salt and crate identity exist.
    """
    name = outcome_key.crate_name(argv)
    if salt is None or name is None:
        return None
    return outcome_key.CacheKey(
        crate_name=name,
        crate_origin=outcome_key.crate_origin(env, salt),
        target=outcome_key.target(argv, salt),
        argument_digest=outcome_key.argument_digest(argv, env, salt),
        toolchain_identity=outcome_key.toolchain_identity(
            argv[0], env, _SUPPORTED_SERVER_VERSION, salt
        ),
    )
