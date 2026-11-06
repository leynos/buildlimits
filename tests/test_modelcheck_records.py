"""The JSON artefact is read by machines, so its fields are a contract.

The report and the artefact are the same statements in two notations, but only
the artefact is consumed by something other than a reader. A field that says
*what kind* of failure a comparison produced, or how long the harness waited
for a run that reported no duration of its own, is the difference between a
record a tool can classify and one it has to guess at.

These are tested against the renderers directly, because the facts come from
results the checker is not guaranteed to produce: a live FizzBee run rarely
times out, so an end-to-end test could not pin the field that records it.
"""

import math
import typing as typ

from modelcheck import domain, records
from modelcheck.fixpoint_failures import FixpointFailure
from modelcheck.trace import Ending, Trace


def _failure(
    result: domain.CheckResult, *, stage: typ.Literal["declared", "doubled"] = "doubled"
) -> FixpointFailure:
    """Build one failed comparison carrying a result.

    Parameters
    ----------
    result : modelcheck.domain.CheckResult
        What the comparison failed with.
    stage : str
        Which of the two comparison runs failed.

    Returns
    -------
    modelcheck.fixpoint_failures.FixpointFailure
        The failure, at the bound the stage implies.
    """
    return FixpointFailure("head", stage, 16, (), result)


def test_a_failed_run_names_the_kind_of_result_it_produced() -> None:
    """A reader must not have to infer the failure's kind from its fields.

    An errored run and a counterexample both fail the comparison, and they mean
    very different things: one says the checker proved nothing, the other says
    it proved the model wrong. The record says which.
    """
    record = records.fixpoint_record(_failure(domain.Errored("no verdict line")))

    assert record["verdict"] == "failed", "the record still says it failed"
    assert record["result"] == "errored", "and names the result that failed"


def test_a_counterexample_is_named_as_one() -> None:
    """A violation is reported by kind, because it carries a path and a shape."""
    trace = Trace({"n": 0}, (), Ending.LASSO, 0)
    violation = domain.Counterexample(
        "L3", domain.CounterexampleKind.LASSO, trace, 4, 0.1
    )

    record = records.fixpoint_record(_failure(violation))

    assert record["result"] == "counterexample", "the result kind is explicit"
    assert record["kind"] == "lasso", "and the shape is kept"
    assert record["property"] == "L3", "with the assertion it violated"


def test_a_harness_time_is_kept_apart_from_the_checker_time() -> None:
    """The two durations measure different things and are not interchanged.

    ``elapsed_seconds`` is the exploration duration the checker reported.
    ``harness_seconds`` is how long the adapter waited for a run that reported
    none. Writing the second under the first name would claim an exploration
    time the checker never gave.
    """
    record = records.fixpoint_record(
        _failure(domain.Errored("no verdict line", harness_seconds=12.5))
    )

    harness_seconds = typ.cast("float", record["harness_seconds"])
    assert math.isclose(harness_seconds, 12.5), "the harness time is recorded"
    assert "elapsed_seconds" not in record, "and never as the checker's duration"


def test_a_run_with_no_harness_time_omits_the_field() -> None:
    """A caller that measured nothing writes no field, rather than a zero."""
    record = records.fixpoint_record(_failure(domain.Stopped("interrupted")))

    assert record["detail"] == "interrupted", "the reason is still recorded"
    assert "harness_seconds" not in record, "but no duration is invented"


def test_a_configuration_that_never_ran_carries_no_bound() -> None:
    """A materialization failure is recorded without a bound it never had.

    Writing a number there would describe a run that never started, which is
    exactly the confusion the stage name exists to prevent.
    """
    failure = FixpointFailure(
        "tail", "materialize", None, (), domain.Errored("no max_actions to double")
    )

    record = records.fixpoint_record(failure)

    assert record["stage"] == "materialize", "the stage says it never became a run"
    assert record["bound"] is None, "and no bound is claimed"
    assert record["result"] == "errored", "with the refusal as its result"
