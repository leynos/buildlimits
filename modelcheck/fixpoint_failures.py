"""The evidence a failed fixpoint comparison carries.

The matrix's judgements are written to the JSON artefact before the fixpoint
check starts, so a comparison run that fails afterwards is not in that file. A
bare exception would leave the command exiting ``2`` with no saved record of
which configuration failed, at which bound, or what the run said. These values
are that record: the comparison layer fills one per failed run, and the caller
writes it into the artefact before reporting the failure.

They live beside :mod:`modelcheck.fixpoint` rather than in it so each module
keeps one concern, and both stay under the repository's file-length limit.
Nothing here opens a file, starts a process or reads the clock: a failure is a
value, like every other result the package judges.
"""

import dataclasses as dc
import typing as typ

from modelcheck.domain import MaterializeError

if typ.TYPE_CHECKING:
    from modelcheck.domain import CheckResult
    from modelcheck.fixpoint import Fixpoint

__all__ = ["FixpointError", "FixpointFailure", "Stage"]

type Stage = typ.Literal["declared", "doubled"]
"""Which of a configuration's two comparison runs failed."""

_ACTION_BOUND = "max_actions"
"""The frontmatter bound the fixpoint check doubles."""


@dc.dataclass(frozen=True, slots=True)
class FixpointFailure:
    """A comparison run that did not pass, kept as the evidence it produced.

    Attributes
    ----------
    configuration : str
        The configuration compared.
    stage : Stage
        Whether the declared-bound or the doubled-bound run failed.
    bound : int
        The ``max_actions`` value that run was given.
    fairness : tuple[str, ...]
        Every fairness tag the run carried, as the matrix records it.
    result : modelcheck.domain.CheckResult
        What the run returned. It is not a ``Passed``, so it carries either a
        counterexample's path or the diagnostic explaining why it proved
        nothing.
    """

    configuration: str
    stage: Stage
    bound: int
    fairness: tuple[str, ...]
    result: CheckResult


class FixpointError(MaterializeError):
    """One or more comparison runs did not pass.

    It carries the failures and the comparisons that did complete, rather than
    only a sentence, because the caller writes both into the artefact before
    the exit status is decided. The message names each failed configuration and
    the bound it was given; the result itself is in :attr:`failures`.

    Attributes
    ----------
    comparisons : tuple[Fixpoint, ...]
        The comparisons that completed before the failures.
    failures : tuple[FixpointFailure, ...]
        Every comparison run that did not pass, in configuration order.
    """

    def __init__(
        self,
        comparisons: tuple[Fixpoint, ...],
        failures: tuple[FixpointFailure, ...],
    ) -> None:
        """Record the completed and failed comparisons."""
        self.comparisons = comparisons
        self.failures = failures
        super().__init__(_failure_message(failures))


def _failure_message(failures: tuple[FixpointFailure, ...]) -> str:
    """Return the sentence a :class:`FixpointError` is raised with.

    Parameters
    ----------
    failures : tuple[FixpointFailure, ...]
        The comparison runs that did not pass.

    Returns
    -------
    str
        One clause per failure, naming the configuration and the bound it was
        given. The result itself is not quoted: it is in the error's
        ``failures`` and in the artefact, and a checker transcript can be long.
    """
    reported = "; ".join(
        f"configuration {failure.configuration!r} did not pass at its "
        f"{failure.stage} bound ({_ACTION_BOUND}={failure.bound})"
        for failure in failures
    )
    return f"{reported}, so its state count cannot be compared"
