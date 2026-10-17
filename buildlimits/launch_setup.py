"""Bounded child-launch diagnostics that require no descriptor allocation."""

import contextlib
import os
import resource
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.attributes import AttributePlan

# Linux PIPE_BUF is at least 4096; leave room below it and avoid allocations
# involving /proc, libc error reporting or external commands under EMFILE.
MAX_DIAGNOSTIC_BYTES = 2048


class Setup:
    """Track launch stage and the error channel through partial relocation.

    Parameters
    ----------
    writer : int
        Close-on-exec reporting descriptor.
    targets : Sequence[int]
        Compiler descriptor numbers, used only as bounded numeric context.
    open_count : int
        Known descriptors remaining after child-side reclamation.
    """

    def __init__(
        self, writer: int, targets: cabc.Sequence[int], open_count: int
    ) -> None:
        """Start tracking before any setup operation can fail."""
        self.kept = [writer]
        self.stage = "close-inherited"
        self.limits: tuple[int | None, int | None] = (None, None)
        self.targets = len(targets)
        self.highest_target = max(targets, default=-1)
        self.expected_count = open_count
        self.open_count: int | None = None
        self.required_soft = 0

    def report(self, program: str, error: BaseException) -> None:
        """Report a setup failure through the current channel, ignoring I/O errors.

        Parameters
        ----------
        program : str
            Executable name; escaped and length bounded before display.
        error : BaseException
            Setup exception; only its type and numeric errno are displayed.
        """
        number = error.errno if isinstance(error, OSError) else None
        display = ascii(Path(program).name[:160])[1:-1]
        kind = ascii(type(error).__name__[:80])[1:-1]
        payload = (
            f"cannot start {display}: stage={self.stage} errno={number} "
            f"error={kind} nofile={self.limits[0]}:{self.limits[1]} "
            f"open_before_relocation={self.open_count} "
            f"required_soft={self.required_soft} "
            f"targets={self.targets} highest_target={self.highest_target}"
        ).encode("ascii", "backslashreplace")[:MAX_DIAGNOSTIC_BYTES]
        with contextlib.suppress(OSError):
            while payload:
                written = os.write(self.kept[0], payload)
                if written == 0:
                    break
                payload = payload[written:]

    def apply_attributes(self, plan: AttributePlan) -> None:
        """Apply caller attributes while naming each potentially failing stage.

        Parameters
        ----------
        plan : AttributePlan
            Validated caller attribute contract.
        """
        self.stage = "umask"
        os.umask(plan.umask)
        self.stage = "niceness"
        os.setpriority(os.PRIO_PROCESS, 0, plan.nice)
        for name, pair in plan.rlimits.items():
            self.stage = f"rlimit:{name}"
            resource.setrlimit(getattr(resource, name), pair)
            if name == "RLIMIT_NOFILE":
                self.limits = pair
