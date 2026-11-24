"""Combine and close the effects one recorder transition produced.

Every recorder transition returns an ``Outputs`` value, and several transitions
build one from effects produced in more than one step: a reducer step, the
port's response to a command it issued, and the span that records the reading's
terminal state. Keeping those combinations here leaves
``outcome_recorder_outputs`` to the window-to-row mapping itself.
"""

import dataclasses as dc
import typing as typ

from buildlimits.outcome_recorder_types import (
    CloseQuery,
    Idle,
    Outputs,
    QueryFinished,
    Querying,
    RecorderState,
)

if typ.TYPE_CHECKING:
    from buildlimits.outcome_record_format import StoreRecord
    from buildlimits.outcome_recorder_types import (
        OutcomeEvent,
        PollResult,
        Progress,
        SpanTransition,
    )
    from buildlimits.outcome_vocabulary import OutcomeReason


def _emission(
    events: tuple[OutcomeEvent, ...],
    records: tuple[StoreRecord, ...],
    spans: tuple[SpanTransition, ...],
    reason: OutcomeReason,
) -> Outputs:
    """Create a terminal reading output with its reason in one place.

    Returns
    -------
    Outputs
        The supplied effects with exactly one reading reason.

    Examples
    --------
    An empty emission for a baseline read carries reason ``"none"``.
    """
    return Outputs(
        events=events, records=records, spans=spans, reading_reasons=(reason,)
    )


def _join_outputs(first: Outputs, second: Outputs) -> Outputs:
    """Combine effects from one reducer transition and its port response.

    Returns
    -------
    Outputs
        A new value containing each effect in original order.

    Examples
    --------
    Joining two empty outputs returns ``Outputs()``.
    """
    return Outputs(
        commands=(*first.commands, *second.commands),
        events=(*first.events, *second.events),
        records=(*first.records, *second.records),
        spans=(*first.spans, *second.spans),
        reading_reasons=(*first.reading_reasons, *second.reading_reasons),
    )


def _with_reading_reason(outputs: Outputs, reason: OutcomeReason) -> Outputs:
    """Attach a reading result when its event and record builder omitted it.

    Returns
    -------
    Outputs
        The existing output, or a copy with the supplied reason.

    Examples
    --------
    Adding ``"timeout"`` to an empty output sets its reading reason.
    """
    if outputs.reading_reasons:
        return outputs
    return dc.replace(outputs, reading_reasons=(reason,))


def _finish_state(
    state: Querying,
    progress: Progress,
    event: PollResult,
    emitted: Outputs,
) -> tuple[RecorderState, Outputs]:
    """Close a terminal ticket and join its span and classification effects.

    Returns
    -------
    tuple[RecorderState, Outputs]
        The idle state and terminal outputs, including a closed ticket.

    Raises
    ------
    RuntimeError
        If the terminal reading did not produce a reason.

    Examples
    --------
    Finishing a ticket with reason ``"none"`` closes it with that span reason.
    """
    if not emitted.reading_reasons:
        msg = "a terminal reading must carry its reason"
        raise RuntimeError(msg)
    reason = emitted.reading_reasons[-1]
    commands = () if state.ticket is None else (CloseQuery(state.ticket),)
    output = _join_outputs(
        emitted,
        Outputs(
            commands=commands,
            spans=(QueryFinished(event.now, reason),),
        ),
    )
    return Idle(progress), output
