"""What a ``modelcheck check`` run leaves behind for a maintainer to read.

The command line parses arguments and decides an exit status. This module is
the other half of that adapter: the artefact the run writes and the traces it
keeps. They are together rather than beside the parsing because both are
written twice — once from the matrix and once from the fixpoint comparison —
and a reader of either needs the other: the artefact says what was found, and
the traces are the paths that let the finding be reproduced.

The module is a driving-adapter concern, not a pure one. It creates
directories, writes files and turns a normalized trace into JSON, so it lives
outside the package's pure-module allowlist beside :mod:`modelcheck.cli`.
"""

import collections.abc as cabc
import dataclasses as dc
import json
import sys
import typing as typ

from modelcheck import domain, records
from modelcheck.adapters.fizzbee_replacements import WITHHELD
from modelcheck.expectations import subject

if typ.TYPE_CHECKING:
    from pathlib import Path

    from modelcheck.domain import Judgement
    from modelcheck.fixpoint import Fixpoint
    from modelcheck.fixpoint_failures import FixpointFailure
    from modelcheck.trace import Trace

__all__ = ["Artefact", "Outputs", "Traces"]


@dc.dataclass(frozen=True, slots=True)
class Artefact:
    """The matrix's machine-readable record: where it goes and what it covers.

    One value rather than a path and a sequence of rows, because the success
    path and the failure path write the same rows to the same place; keeping
    them together is what stops the two writes from drifting apart.
    """

    path: Path | None
    """The ``--json`` destination, or ``None`` when the run asked for none."""

    chosen: tuple[Judgement, ...]
    """The judgements the artefact describes, after ``--only`` narrowed them."""

    def write(
        self,
        failures: tuple[FixpointFailure, ...] = (),
        comparisons: tuple[Fixpoint, ...] = (),
    ) -> None:
        """Write the records, adding the fixpoint comparisons that ran.

        Parameters
        ----------
        failures : tuple[FixpointFailure, ...]
            The comparisons that did not pass, in the order they ran. Empty
            for a run that did not fail one.
        comparisons : tuple[Fixpoint, ...]
            The comparisons that completed, stable or growing. Written as well
            as the failures, because a growth result also makes the command
            exit ``1`` and an artefact that omitted it would describe a clean
            run the status contradicts.

        Notes
        -----
        Writing is a no-op when the run asked for no artefact, so both callers
        write unconditionally rather than repeating the check.
        """
        if self.path is None:
            return
        self.path.write_text(
            records.render_records(self.chosen, failures, comparisons),
            encoding="utf-8",
        )


@dc.dataclass(frozen=True, slots=True)
class Traces:
    """Where a counterexample's normalized path is kept.

    A trace is written only for a counterexample, because only a
    counterexample has one. The file name is the configuration and the subject,
    so a re-run replaces its own trace rather than accumulating a directory
    full of timestamps, and a review of the change sees the paths that moved.

    A file is replaced only when its configuration still has a counterexample.
    When a configuration that once failed now passes, its old trace stays under
    ``traces/`` and a maintainer removes it by hand: this class writes what the
    run found and deletes nothing it did not write.

    A recorded trace is also kept when the run's own evidence was withheld in
    part. Redaction withholds a decoded string that contains a one-character
    credential, so a host whose environment holds one — a credential-named
    boolean, say — produces a trace that names fewer variables than the model
    declares. Replacing the committed file with it would trade evidence about
    the model for evidence about the host that ran the check, so the file is
    left as it is and the run says which one.

    Comparison traces carry their own stage in the name. The matrix has
    already saved a trace for the same configuration and subject, and a
    comparison run is a different run: without the stage, a failed comparison's
    path would overwrite the matrix's counterexample rather than sit beside it.
    """

    enabled: bool
    """Whether the run asked for traces at all."""

    model_dir: Path
    """The model directory; traces go under its ``traces/``."""

    def save(self, judgements: cabc.Sequence[Judgement]) -> None:
        """Write each matrix counterexample's normalized path.

        Parameters
        ----------
        judgements : collections.abc.Sequence[Judgement]
            The judgements whose counterexamples to keep.
        """
        self._write_counterexamples(
            (
                f"{judgement.configuration}-{subject(judgement.expectation)}",
                judgement.result,
            )
            for judgement in judgements
        )

    def save_comparisons(self, failures: cabc.Sequence[FixpointFailure]) -> None:
        """Write each failed comparison's counterexample path.

        Parameters
        ----------
        failures : collections.abc.Sequence[FixpointFailure]
            The comparison runs that did not pass. Only those that produced a
            counterexample have a path; a run that proved nothing, or a
            configuration that could not be materialized, wrote none.
        """
        self._write_counterexamples(
            (
                f"{failure.configuration}-fixpoint-{failure.stage}",
                failure.result,
            )
            for failure in failures
        )

    def _write_counterexamples(self, found: cabc.Iterable[tuple[str, object]]) -> None:
        """Write the paths of the results among ``found`` that are counterexamples.

        Parameters
        ----------
        found : collections.abc.Iterable[tuple[str, object]]
            Each candidate's file name and its result. The name is computed by
            the caller because only the caller knows how its run is named; the
            filtering is shared because both callers keep a trace exactly when
            the result is a counterexample.

        Notes
        -----
        A run that reported no counterexample has no path to write, and the
        filter is a no-op when the run asked for no traces at all — which is
        why ``enabled`` is checked before the generator is consumed.
        """
        if not self.enabled:
            return
        for name, result in found:
            match result:
                case domain.Counterexample(trace=trace):
                    self._write(name, trace)
                case _:
                    pass

    def _write(self, name: str, trace: Trace) -> None:
        """Write one trace under the model directory's ``traces/``.

        Parameters
        ----------
        name : str
            The file's name, without the ``.json`` suffix.
        trace : modelcheck.trace.Trace
            The normalized path to write.

        Notes
        -----
        An existing file is left alone when the new trace carries withheld
        evidence, because redaction withholds a decoded string that contains a
        one-character credential and the run's environment decides which
        strings those are. Replacing a committed trace with one the host
        shortened would make the evidence describe the machine rather than the
        model. A trace with no file to replace is still written, since nothing
        is lost by recording it, and the marker in it says what was withheld.
        """
        directory = self.model_dir / "traces"
        directory.mkdir(parents=True, exist_ok=True)
        record = records.trace_record(trace)
        path = directory / f"{name}.json"
        if path.exists() and _holds_withheld(record):
            print(
                f"modelcheck: kept {path.as_posix()}: redaction withheld part of"
                " the new trace, so recorded evidence was not replaced",
                file=sys.stderr,
            )
            return
        path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


def _holds_withheld(record: object) -> bool:
    """Return whether a rendered trace carries evidence redaction withheld.

    Parameters
    ----------
    record : object
        One trace rendered by :func:`modelcheck.records.trace_record`: nested
        mappings and sequences of keys, values and scalars.

    Returns
    -------
    bool
        Whether any string in it is the withholding marker, including the
        numbered form a repeated state variable is given.

    Examples
    --------
    >>> _holds_withheld({"halted": WITHHELD})
    True
    >>> _holds_withheld({"halted": False})
    False
    """
    match record:
        case str():
            return record == WITHHELD or record.startswith(f"{WITHHELD} #")
        case cabc.Mapping():
            return any(
                _holds_withheld(key) or _holds_withheld(value)
                for key, value in record.items()
            )
        case list() | tuple():
            return any(_holds_withheld(item) for item in record)
        case _:
            return False


@dc.dataclass(frozen=True, slots=True)
class Outputs:
    """What a run leaves behind for a maintainer to read.

    The artefact and the traces are written from the same judgements and must
    agree about what was found, so the comparison step is handed both together
    rather than as two arguments it could pair with the wrong record.
    """

    artefact: Artefact
    """The machine-readable record of the matrix and of each comparison."""

    traces: Traces
    """Where each counterexample's normalized path is kept."""
