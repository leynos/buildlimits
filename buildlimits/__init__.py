"""Machine-wide admission for concurrent Cargo compilation.

The package provides the compiler adapters that Cargo runs through
``RUSTC_WRAPPER`` and ``RUSTDOC``, the per-owner admission supervisor that
holds guildmaster tokens for admitted compiler groups, and the operator
status, recovery and metrics commands. ``docs/users-guide.md`` describes
their use and ``docs/developers-guide.md`` the internal interfaces.

Examples
--------
>>> Admission(cached=False).submit(1)
[NotifyQueued(request_id=1, position=0), RequestToken()]
"""

from buildlimits.admission import Admission, GroupState
from buildlimits.config import SupervisorConfig, load
from buildlimits.effects import Exit
from buildlimits.protocol import PROTOCOL_VERSION, CompileRequest

__all__ = [
    "PROTOCOL_VERSION",
    "Admission",
    "CompileRequest",
    "Exit",
    "GroupState",
    "SupervisorConfig",
    "load",
]
