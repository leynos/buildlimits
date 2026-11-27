"""Render one maintenance report as JSON or as bounded operator lines.

The command's only user-facing surface is its report. Keeping the rendering
here means the report model stays a plain value, the timer's journal lines are
reviewed as data, and the command module is left with argument parsing, the
configuration and the closed exit status.

No line names a filesystem path or a raw exception, so a timer's journal stays
free of host-specific detail.
"""

import json
import typing as typ

from buildlimits.outcome_archive_codec import report_dict
from buildlimits.outcome_archive_evictions import eviction_counts
from buildlimits.utc_text import utc_text

if typ.TYPE_CHECKING:
    from buildlimits.outcome_archive_report import RunReport

__all__ = ["write_report"]


def write_report(report: RunReport, *, as_json: bool) -> None:
    """Render one report as JSON or as bounded operator lines.

    Parameters
    ----------
    report : RunReport
        The run or inspection result.
    as_json : bool
        Whether to print the stable JSON document instead of prose.
    """
    if as_json:
        print(json.dumps(report_dict(report), sort_keys=True))
        return
    print(f"operation: {report.operation}")
    print(f"status: {report.status}")
    if report.operation == "retain":
        print("retain: sealed history is rewritten in place, never compressed")
    if report.reason is not None:
        print(f"reason: {report.reason}")
    print(f"started: {utc_text(report.started_at)}")
    print(f"elapsed: {_elapsed(report)}")
    print(f"retained: {report.retained_bytes} of {report.budget_bytes} logical bytes")
    if report.fallback_bytes:
        print(
            f"retained fallback: {report.fallback_bytes} bytes from physical size; "
            f"{report.unmeasured_entries} entries unmeasured"
        )
    _write_history(report)
    print(f"active: {report.active_bytes} logical bytes")
    print(f"cutoff: {_cutoff_text(report)}")
    _write_clock(report)
    _write_limits(report)
    _write_plan(report)
    _write_actions(report)
    _write_evictions(report)
    _write_diagnostics(report)
    _write_maintenance(report)


def _write_limits(report: RunReport) -> None:
    """Print the effective policy in the same units the JSON report uses."""
    limits = report.limits
    if limits is None:
        return
    age = f"{limits.max_age_days} days" if limits.age_eviction else "disabled"
    logical = str(limits.max_bytes) if limits.byte_eviction else "unlimited"
    records = "none" if limits.max_records is None else str(limits.max_records)
    mode = "segmented" if limits.segmented else "in-place"
    print(
        f"limits: age {age}, {logical} logical bytes, {records} record cap, "
        f"{limits.headroom_bytes} headroom bytes, {mode}"
    )


def _write_evictions(report: RunReport) -> None:
    """Print how many trims and deletions each bound caused."""
    counts = eviction_counts(report.actions)
    if not any(counts.values()):
        return
    # Only the bounds that removed something are printed, so a run under an age
    # limit alone does not read as though a byte or record bound acted.
    performed = ", ".join(
        f"{cause.replace('_and_', ' and ')} {count}"
        for cause, count in counts.items()
        if count
    )
    print(f"evictions: {performed}")


def _write_maintenance(report: RunReport) -> None:
    """Print a persistence failure an operator may need to act on.

    A run that never expected to install state, such as a dry run, carries no
    maintenance reason and is already labelled above; only a fault is printed.
    The clock baseline is reported separately because the store's own state can
    be intact beside it.
    """
    maintenance = report.maintenance
    if maintenance.reason is None:
        # A report that never expected to install state, such as a dry run,
        # says so already; only a fault is printed.
        return
    if not maintenance.state_written:
        print(f"state: not written ({maintenance.reason})")
    elif not maintenance.baseline_written:
        print(f"clock baseline: not written ({maintenance.reason})")


def _write_clock(report: RunReport) -> None:
    """Print the clock-guard decision when it withheld the age bound.

    A skewed reading means the wall clock moved independently of boot time, so
    this firing did not expire history by age. The next undisturbed firing
    applies the real bound.
    """
    if report.clock_skew:
        print("clock skew: age eviction withheld")


def _elapsed(report: RunReport) -> str:
    """Return the wall duration the report spans, to the millisecond.

    Returns
    -------
    str
        The finish minus the start, so a journal reader can tell a slow firing
        from a stalled one. A host clock that steps can make the difference
        negative, which is printed as measured rather than hidden.
    """
    seconds = (report.finished_at - report.started_at).total_seconds()
    return f"{seconds:.3f}s"


def _write_history(report: RunReport) -> None:
    """Print the retained rows and the span their observations cover."""
    span = "unknown"
    if report.observed_from is not None and report.observed_until is not None:
        span = f"{utc_text(report.observed_from)}..{utc_text(report.observed_until)}"
    unmeasured = (
        ""
        if report.unmeasured_entries == 0
        else f", {report.unmeasured_entries} unmeasured"
    )
    print(f"history: {report.retained_records} records{unmeasured}, observed {span}")


def _cutoff_text(report: RunReport) -> str:
    """Describe the age cutoff a timer should display.

    Returns
    -------
    str
        The UTC cutoff, a marker when the clock guard withheld it, or a marker
        when age eviction is disabled.
    """
    if report.cutoff is None:
        if report.clock_skew:
            return "withheld by clock skew"
        return "age eviction disabled"
    return report.cutoff.isoformat()


def _write_plan(report: RunReport) -> None:
    """Print the plan's counts when the report carries one."""
    plan = report.plan
    if plan is None:
        return
    print(
        "plan: "
        f"archive {plan.archive}, reconcile {plan.reconcile}, heal {plan.heal}, "
        f"sealed repair {plan.sealed_repair}, stale marker {plan.stale_marker}, "
        f"orphan {plan.orphan}, trim {plan.trim}, delete {plan.delete}"
    )
    if plan.retention_deferred:
        # The plan's trim and delete counts are provisional until a settled
        # pass has measured the store a budget applies to.
        print("retention: deferred; trims and deletes are decided once settled")
    if report.dry_run:
        print("dry run: no file was changed")


def _write_actions(report: RunReport) -> None:
    """Print one line per action name with how often it occurred."""
    counts: dict[str, int] = {}
    for action in report.actions:
        counts[action.action] = counts.get(action.action, 0) + 1
    for name in sorted(counts):
        print(f"action {name}: {counts[name]}")


def _write_diagnostics(report: RunReport) -> None:
    """Print the integrity flags an operator may need to act on."""
    _print_sequences("gaps", report.gaps)
    if report.gaps_truncated:
        print(f"gaps truncated: more than {len(report.gaps)} sequences are missing")
    _print_sequences("missing sidecar", report.missing_sidecars)
    _print_sequences("corrupt sidecar", report.corrupt_sidecars)
    _print_sequences("orphan sidecar", report.orphan_sidecars)
    _print_sequences("unfinished sealed trim", report.sealed_replacing)
    for sequence, reason in report.failures:
        print(f"failure: sequence {sequence}: {reason}")


def _print_sequences(label: str, sequences: tuple[int, ...]) -> None:
    """Print one comma-separated integrity line when it is not empty.

    Parameters
    ----------
    label : str
        The closed diagnostic name.
    sequences : tuple[int, ...]
        The sequences the diagnostic names.
    """
    if sequences:
        joined = ", ".join(str(sequence) for sequence in sequences)
        print(f"{label}: {joined}")
