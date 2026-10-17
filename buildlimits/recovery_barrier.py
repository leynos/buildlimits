"""The restart barrier that keeps the managed cache down while it is unaccounted.

The managed cache unit carries
``ConditionPathExists=!%t/buildlimits/cache-recovery``, so this file decides
whether a queued start reaches the service at all. Every operation therefore
has two outcomes the recovery transaction must tell apart: the file changed,
or it did not. Reporting which of the two happened is all this module does;
what a refusal means for a token stays the transaction's business, which is
why nothing here decides whether a fault is fatal.
"""

import dataclasses as dc
import typing as typ

if typ.TYPE_CHECKING:
    from pathlib import Path


@dc.dataclass(frozen=True, slots=True)
class RecoveryBarrier:
    """The restart barrier file, and the operations that move it.

    Parameters
    ----------
    path : Path
        The barrier's location in the supervisor's runtime directory. The
        managed unit's own condition reads the same path.
    """

    path: Path

    def write(self) -> None:
        """Create the barrier and its directory before the cache is stopped.

        Raises
        ------
        OSError
            If the barrier cannot be created: the cache could then come back
            while its work is still unaccounted.
        """  # ruff: ignore[docstring-extraneous-exception] - mkdir and write_text raise it.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("recovering\n", encoding="ascii")

    def clear(self) -> None:
        """Remove the barrier, leaving no reason for the unit to stay down.

        Raises
        ------
        OSError
            If the barrier survives the removal, which happens when its
            runtime directory or filesystem is no longer writable. A caller
            that only wants the barrier gone must not report a start that the
            unit's own condition would then skip.
        """  # ruff: ignore[docstring-extraneous-exception] - unlink raises it.
        self.path.unlink(missing_ok=True)
