"""Shared test-only helpers for EP-M3 cache-outcome tests.

Keep lazy application-module imports and stable HMAC expectations here; the
helpers are scoped to the key, parser, record and window tests in this milestone.
"""

import hashlib
import hmac
import importlib
import typing as typ

from hypothesis import event

if typ.TYPE_CHECKING:
    from types import ModuleType


def outcome_module(name: str) -> ModuleType:
    """Load an outcome module lazily so red tests collect before implementation.

    Returns
    -------
    ModuleType
        The requested ``buildlimits`` module.

    Example:
        ``outcome_module("outcome_key").__name__`` is
        ``"buildlimits.outcome_key"`` once the implementation exists.
    """
    return importlib.import_module(f"buildlimits.{name}")


def expected_hmac(value: str, key: bytes) -> str:
    """Return the SHA-256 HMAC expected for a private path input.

    Returns
    -------
    str
        The lowercase hexadecimal HMAC digest.

    Example:
        A 32-byte salt produces a 64-character SHA-256 digest for a path.
    """
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


def record_generated_case(name: str) -> None:
    """Record a Hypothesis event that proves a generated branch was reached.

    Example:
        ``record_generated_case("target:path")`` adds that reached branch to
        Hypothesis' generated-case statistics.
    """
    event(name)
