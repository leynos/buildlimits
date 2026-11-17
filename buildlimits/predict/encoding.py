"""Encode one causal feature row for the logistic model.

The vector holds only values known before admission: the origin class, a hashed
identity bucket, the current load and the time since the identity was last
seen. Nothing here reads a label, so encoding a row cannot leak its outcome.
"""

import hashlib
import math
import typing as typ

if typ.TYPE_CHECKING:
    from buildlimits.predict.events import Features

ORIGIN_ORDER = ("registry", "git", "path", "unknown")
IDENTITY_BUCKETS = 8
LOGISTIC_WEIGHT_COUNT = 1 + len(ORIGIN_ORDER) + IDENTITY_BUCKETS + 3


def logistic_features(row: Features) -> tuple[float, ...]:
    """Encode one row as the feature model's causal input vector.

    Returns
    -------
    tuple[float, ...]
        Bias, origin one-hot, identity bucket one-hot, load and age terms.

    Examples
    --------
    The vector has one entry per model weight:

    >>> from types import SimpleNamespace
    >>> row = SimpleNamespace(identity="a" * 64, origin="git", age_seconds=None,
    ...                       load=0)
    >>> len(logistic_features(row)) == LOGISTIC_WEIGHT_COUNT
    True
    """
    origin = [1.0 if row.origin == name else 0.0 for name in ORIGIN_ORDER]
    bucket = _identity_bucket(row.identity)
    identity = [1.0 if index == bucket else 0.0 for index in range(IDENTITY_BUCKETS)]
    has_age = row.age_seconds is not None
    age = math.log1p(row.age_seconds) if row.age_seconds is not None else 0.0
    return (1.0, *origin, *identity, math.log1p(row.load), float(has_age), age)


def _identity_bucket(identity: str) -> int:
    """Map an identity onto one of the model's hashed buckets.

    Returns
    -------
    int
        A bucket index in ``[0, IDENTITY_BUCKETS)``.
    """
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % IDENTITY_BUCKETS
