"""Replace what describes the runner inside a decoded counterexample path.

The transcript and the error graph are redacted as text, but a trace is not
text: it is the graph's JSON decoded into the states and action labels a replay
uses. Redacting the text alone leaves two gaps. Unless the checker happened to
spell a value exactly as the text needles expect, an escape that survived the
text pass is gone by the time the value is a Python string — and a value the
adapter builds, such as `<redacted>`, would otherwise have to be produced twice
in different representations.

So the decoded path is redacted too, by the same :class:`Redaction
<modelcheck.adapters.fizzbee_redaction.Redaction>` the text pass used. What
this module adds is only the traversal: a state is the checker's own JSON, so a
value may be a string, a number, a boolean, `null`, a list or a nested object,
and only the strings can carry a credential.

:func:`redact_result` is the entry point a run calls. It runs *after* the run
has been classified, on the checker's own document: a credential is replaced in
the evidence, never before the evidence is read. That order matters because a
replacement is not injective — two distinct state values can become the same
placeholder, and a number can become a non-number — so sanitizing first could
change a graph's validity, its ending, or the equality that classifies a lasso.
The verdict is therefore fixed before any value is replaced.

An error's detail carries the parser's refusal, which quotes the document it
refused and can therefore hold a decoded credential, so it is redacted here
too. Every retained string goes through
:meth:`~modelcheck.adapters.fizzbee_replacements.Redaction.retain`, which
withholds a text a credential survived in rather than recording it.

The replacements themselves, and the reasoning behind each needle, stay in
:mod:`modelcheck.adapters.fizzbee_redaction`; this module is separate so both
files stay inside the repository's length limit, and so a reader looking for
*what must not be recorded* has one module to read.
"""

import collections.abc as cabc
import dataclasses as dc
import typing as typ

from modelcheck.domain import Counterexample, Errored

if typ.TYPE_CHECKING:
    from modelcheck.adapters.fizzbee_redaction import Redaction
    from modelcheck.domain import CheckResult
    from modelcheck.trace import Trace

__all__ = ["redact_result", "redact_trace"]


def redact_result(result: CheckResult, redaction: Redaction) -> CheckResult:
    """Return ``result`` with the evidence it carries redacted.

    Parameters
    ----------
    result : modelcheck.domain.CheckResult
        The classified result of one run. It must be classified already: this
        function changes what the result says, never what it means.
    redaction : Redaction
        The substitutions this run's evidence needs.

    Returns
    -------
    modelcheck.domain.CheckResult
        A counterexample with its action labels and state values redacted, an
        error with its detail redacted, and every other result unchanged. An
        error's detail holds the parser's refusal and the document it refused,
        so it can carry a decoded credential even though the adapter has
        already redacted the text it printed.

    Examples
    --------
    An error with nothing to replace is unchanged.

    >>> from modelcheck.adapters.fizzbee_redaction import Redaction
    >>> from modelcheck.domain import Errored
    >>> redact_result(Errored("it broke"), Redaction(())).detail
    'it broke'
    """
    match result:
        case Counterexample(trace=trace):
            return dc.replace(result, trace=redact_trace(trace, redaction))
        case Errored(detail=detail):
            return dc.replace(result, detail=redaction.retain(detail))
        case _:
            return result


def redact_trace(trace: Trace, redaction: Redaction) -> Trace:
    r"""Return ``trace`` with every string it holds redacted.

    Redacting the text the checker wrote is not enough on its own. An error
    graph is JSON, and its evidence is the *decoded* value: a state may hold a
    credential whose escapes have been resolved, or in a spelling neither text
    needle matched. Applying the same replacements to the decoded values is
    what makes the saved trace and the record carry the placeholder rather than
    the value.

    Parameters
    ----------
    trace : modelcheck.trace.Trace
        The normalized path decoded from the run's error graph.
    redaction : Redaction
        The substitutions this run's evidence needs.

    Returns
    -------
    modelcheck.trace.Trace
        The path with every action label and every string in a state replaced.
        A string a credential survived in is withheld whole rather than
        recorded. A number, a boolean or ``null`` is a value the model
        computed, not text the checker wrote around it, and is left as it is.

    Examples
    --------
    >>> from modelcheck.adapters.fizzbee_redaction import Redaction, Replacement
    >>> from modelcheck.trace import Ending, Step, Trace
    >>> redaction = Redaction((Replacement("s3cr3t", "<redacted>"),), ("s3cr3t",))
    >>> trace = Trace({"t": "s3cr3t"}, (Step("Go", {"t": 1}),), Ending.FINITE, None)
    >>> redact_trace(trace, redaction).states[0]
    {'t': '<redacted>'}
    """
    return dc.replace(
        trace,
        initial=_redacted_state(trace.initial, redaction),
        steps=tuple(
            dc.replace(
                step,
                action=redaction.retain(step.action),
                state=_redacted_state(step.state, redaction),
            )
            for step in trace.steps
        ),
    )


def _redacted_state(
    state: cabc.Mapping[str, object], redaction: Redaction
) -> dict[str, object]:
    """Return one decoded state with every string it holds redacted.

    Parameters
    ----------
    state : collections.abc.Mapping[str, object]
        One state the checker recorded.
    redaction : Redaction
        The substitutions this run's evidence needs.

    Returns
    -------
    dict[str, object]
        The state with each key and each string value, however deeply nested,
        replaced.
    """
    return {
        redaction.retain(key): _redacted_value(value, redaction)
        for key, value in state.items()
    }


def _redacted_value(value: object, redaction: Redaction) -> object:
    """Return one decoded JSON value with every string it holds redacted.

    Parameters
    ----------
    value : object
        A value from a state the checker recorded. A state is the checker's own
        JSON, so a value is a string, a number, a boolean, ``null``, a list or
        a nested object; only the strings can carry a credential.
    redaction : Redaction
        The substitutions this run's evidence needs.

    Returns
    -------
    object
        The value with every string replaced and every other value unchanged.
    """
    match value:
        case str():
            return redaction.retain(value)
        case cabc.Mapping():
            return {
                redaction.retain(key): _redacted_value(item, redaction)
                for key, item in value.items()
            }
        case list():
            return [_redacted_value(item, redaction) for item in value]
        case tuple():
            return tuple(_redacted_value(item, redaction) for item in value)
        case _:
            return value
