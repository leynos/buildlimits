"""Run CrossHair over the symbolic contracts in ``crosshair_contracts``."""

import ast
import importlib.metadata
import re
import subprocess
import sys
from pathlib import Path

import pytest

CONTRACTS = Path(__file__).with_name("crosshair_contracts.py")

BOUNDED_PROOFS = (
    "unanimous_hit_windows_keep_the_hit_label",
    "cache_errors_are_not_dropped_from_a_solo_footprint",
    "repeated_cache_errors_are_not_unanimous",
    "decoding_never_labels_mixed",
)
"""The attribution contracts this branch claims as completed bounded proofs."""

KNOWN_INCONCLUSIVE = ("planned_limits_are_reachable",)
"""A descriptor-limit contract this module checks but does not claim to prove.

Its integer arguments are unbounded, so an exhaustive confirmation is not
expected. It is exercised by the descriptor tests instead, and naming it here
keeps an unrelated inconclusive result from being mistaken for a regression in
the four attribution proofs.
"""

CONFIRMED = "Confirmed over all paths."

# The per-condition timeout bounds each contract, not the run: the total grows
# with the number of contracts. The whole run gets its own bound, set below
# the test's so the child is reaped rather than orphaned when it stalls.
RUN_TIMEOUT_SECONDS = 100

_RESULT_LINE = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+): (?P<kind>info|error): (?P<message>.+)$"
)


@pytest.mark.timeout(RUN_TIMEOUT_SECONDS + 20)
def test_crosshair_confirms_each_bounded_proof() -> None:
    """Every claimed bounded proof is confirmed, with no counterexample.

    ``crosshair check`` exits zero when it merely fails to find a
    counterexample, and its default output hides inconclusive results. The run
    therefore asks for every postcondition's result and requires a positive
    confirmation for each claimed contract, so a missing or inconclusive target
    cannot pass as a completed proof.
    """
    command = _command()
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        cwd=CONTRACTS.parent,
        timeout=RUN_TIMEOUT_SECONDS,
    )
    report = _results(result.stdout + result.stderr)
    evidence = _evidence(command, result.returncode, report)
    print(evidence)
    assert result.returncode == 0, evidence

    contradicted = {
        name: message for name, (kind, message) in report.items() if kind == "error"
    }
    assert not contradicted, f"no target reports a counterexample: {contradicted}"

    inconclusive = {
        name: report.get(name, ("info", "no result reported"))[1]
        for name in BOUNDED_PROOFS
        if report.get(name, ("info", None))[1] != CONFIRMED
    }
    assert not inconclusive, (
        f"each claimed proof is confirmed over all paths: {inconclusive}"
    )

    unclaimed = {
        name: message
        for name, (_kind, message) in report.items()
        if message != CONFIRMED
        and name not in BOUNDED_PROOFS
        and name not in KNOWN_INCONCLUSIVE
    }
    assert not unclaimed, f"no other target is silently inconclusive: {unclaimed}"


def _command() -> list[str]:
    """Return the CrossHair argv that reports every postcondition's result.

    Returns
    -------
    list[str]
        The interpreter, module and check arguments for one run.
    """
    return [
        sys.executable,
        "-m",
        "crosshair",
        "check",
        "--analysis_kind=asserts",
        "--per_condition_timeout=20",
        "--report_all",
        str(CONTRACTS),
    ]


def _results(output: str) -> dict[str, tuple[str, str]]:
    """Map each reported contract to its severity and message.

    CrossHair reports one line per function at the line of its ``def``, so the
    module is parsed to recover the names that the line numbers address.

    Parameters
    ----------
    output : str
        The combined standard output and standard error of one run.

    Returns
    -------
    dict[str, tuple[str, str]]
        The severity and message reported for each contract by name.
    """
    names = _definitions()
    found: dict[str, tuple[str, str]] = {}
    for line in output.splitlines():
        match = _RESULT_LINE.match(line)
        if match is None:
            continue
        name = names.get(int(match.group("line")))
        if name is not None:
            found[name] = (match.group("kind"), match.group("message"))
    return found


def _definitions() -> dict[int, str]:
    """Return the line of each top-level contract's ``def`` by name.

    Returns
    -------
    dict[int, str]
        A line number mapped to the function defined there.
    """
    tree = ast.parse(CONTRACTS.read_text(encoding="utf-8"))
    return {
        node.lineno: node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }


def _evidence(
    command: list[str], returncode: int, report: dict[str, tuple[str, str]]
) -> str:
    """Render the verifier version, command and per-target results.

    Parameters
    ----------
    command : list[str]
        The argv that produced the results.
    returncode : int
        The verifier's exit status.
    report : dict[str, tuple[str, str]]
        The severity and message reported for each contract by name.

    Returns
    -------
    str
        A block that records what ran and what it concluded per target.
    """
    version = importlib.metadata.version("crosshair-tool")
    rows = "\n".join(
        f"  {name}: {kind}: {message}"
        for name, (kind, message) in sorted(report.items())
    )
    return (
        f"crosshair-tool {version}\n"
        f"command: {' '.join(command)}\n"
        f"exit status: {returncode}\n"
        f"per-target results:\n{rows}"
    )
