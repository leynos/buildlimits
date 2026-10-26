"""Keep lock checks ahead of every local and CI project synchronization."""

import pathlib
import subprocess
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAKEFILE = ROOT / "Makefile"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _fake_uv(
    tmp_path: pathlib.Path, *, fail_lock_check: bool = False
) -> tuple[pathlib.Path, pathlib.Path]:
    """Create a uv stand-in that records calls and can reject a stale lock."""
    executable = tmp_path / "uv"
    call_log = tmp_path / "uv-calls.txt"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "from pathlib import Path\n"
        "arguments = sys.argv[1:]\n"
        f"with Path({str(call_log)!r}).open('a', encoding='utf-8') as calls:\n"
        "    calls.write('\\t'.join(arguments) + '\\n')\n"
        f"if {fail_lock_check!r} and arguments == ['lock', '--check']:\n"
        "    raise SystemExit(17)\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return executable, call_log


def _uv_calls(call_log: pathlib.Path) -> list[list[str]]:
    """Read the ordered argument vectors recorded by the uv stand-in."""
    return [
        line.split("\t") for line in call_log.read_text(encoding="utf-8").splitlines()
    ]


def _run_make(
    target: str, cwd: pathlib.Path, executable: pathlib.Path
) -> subprocess.CompletedProcess[str]:
    """Run one Makefile target in the supplied isolated directory."""
    return subprocess.run(
        [
            "make",
            "--no-print-directory",
            "-f",
            str(MAKEFILE),
            f"UV={executable}",
            target,
        ],
        cwd=cwd,
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    ("target", "expected_calls"),
    [
        pytest.param("check-lock", (("lock", "--check"),), id="check-lock"),
        pytest.param(
            "build-release",
            (
                ("lock", "--check"),
                ("run", "python", "-m", "build", "--sdist", "--wheel"),
            ),
            id="build-release",
        ),
    ],
)
def test_targets_check_lock_before_uv_work(
    tmp_path: pathlib.Path, target: str, expected_calls: tuple[tuple[str, ...], ...]
) -> None:
    """Lock and release targets check freshness before other uv operations."""
    executable, call_log = _fake_uv(tmp_path)

    result = _run_make(target, tmp_path, executable)

    assert result.returncode == 0, f"make {target} must accept the valid fake lock"
    calls = tuple(tuple(call) for call in _uv_calls(call_log))
    assert calls == expected_calls, (
        f"make {target} must preserve the expected ordered uv commands"
    )


def test_build_stops_before_sync_when_lock_check_fails(
    tmp_path: pathlib.Path,
) -> None:
    """A stale lock prevents build from reaching the mutable project sync."""
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'lock-gate-contract'\nversion = '0.1.0'\n",
        encoding="utf-8",
    )
    executable, call_log = _fake_uv(tmp_path, fail_lock_check=True)

    result = _run_make("build", tmp_path, executable)

    calls = _uv_calls(call_log)
    assert result.returncode != 0, "a failed lock check must fail the build"
    assert calls[0] == ["lock", "--check"], "the freshness gate must run first"
    assert ["sync", "--group", "dev"] not in calls, (
        "a stale lock must fail before uv sync can repair it"
    )


def test_ci_checks_lock_before_installing_code() -> None:
    """CI runs the lock check after installing uv and before project setup."""
    workflow = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict), "CI workflow must have a mapping root"
    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict), "CI workflow must define jobs"
    lint_test = jobs.get("lint-test")
    assert isinstance(lint_test, dict), "CI workflow must define the lint-test job"
    steps = lint_test.get("steps")
    assert isinstance(steps, list), "lint-test must define ordered steps"
    named_steps = [step.get("name") for step in steps if isinstance(step, dict)]
    install_uv = named_steps.index("Install uv")
    check_lock = named_steps.index("Check lockfile")
    install_code = named_steps.index("Install code")

    assert install_uv < check_lock < install_code, (
        "CI must check lock freshness after uv install and before code setup"
    )
    check_step = steps[check_lock]
    assert isinstance(check_step, dict), (
        "the check-lock workflow step must be a mapping"
    )
    assert check_step.get("run") == "make check-lock", (
        "CI must invoke the dedicated Makefile lock check"
    )
