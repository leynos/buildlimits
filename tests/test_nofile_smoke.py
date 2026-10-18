"""Verify the standalone pressure fixture without launching compiler children."""

import ast
import importlib.util
import typing as typ
from pathlib import Path
from unittest import mock

import pytest

if typ.TYPE_CHECKING:
    from types import ModuleType


@pytest.fixture
def fixture() -> ModuleType:
    """Load the standalone fixture with isolated module state.

    Returns
    -------
    ModuleType
        The fixture imported without executing its command-line entry point.
    """
    path = Path(__file__).parent / "fixtures" / "nofile_smoke.py"
    spec = importlib.util.spec_from_file_location("nofile_smoke", path)
    assert spec is not None and spec.loader is not None, "fixture is importable"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("release", "nice"),
    [
        (Path("/fixture/release"), 1),
        (Path("/fixture/quoted 'release'"), -5),
        (Path("/fixture/both \"quotes\" and 'spaces'"), 19),
    ],
    ids=["plain", "single-quotes", "mixed-quotes"],
)
def test_child_program_preserves_source(
    fixture: ModuleType, release: Path, nice: int
) -> None:
    """Release-path escaping and niceness preserve the original child source."""
    expected = (
        "import os, resource, time; from pathlib import Path; "
        f"gate=Path({str(release)!r}); "
        "deadline=time.monotonic()+60; "
        "\nwhile not gate.exists() and time.monotonic()<deadline: "
        "time.sleep(0.02)\n"
        "assert gate.exists(); "
        "assert resource.getrlimit(resource.RLIMIT_NOFILE)==(1024,4096); "
        "assert os.umask(0)==0o027; "
        f"assert os.getpriority(os.PRIO_PROCESS,0)=={nice}; "
        "[os.fstat(fd) for fd in range(7)]"
    )
    source = fixture._child_script(release, nice)
    assert source == expected, "the generated child program is unchanged"
    # Parse only: executing the assertions would modify this test's umask.
    ast.parse(source)


@pytest.mark.parametrize(
    ("queued", "expected"),
    [
        ([None] * 158, True),
        ([None] * 157, False),
        ([None] * 159, False),
        ((None,) * 158, False),
        (None, False),
        ({"queued": 158}, False),
        (158, False),
    ],
    ids=["exact", "short", "long", "tuple", "none", "mapping", "integer"],
)
def test_full_queue_requires_an_exact_list(
    fixture: ModuleType, queued: object, *, expected: bool
) -> None:
    """Only the protocol's exact waiting-list size satisfies queue readiness."""
    assert fixture._has_full_queue(queued) is expected, "queue shape is validated"


def test_short_queue_does_not_access_held_slots(fixture: ModuleType) -> None:
    """A short queue rejects pressure before inspecting the held-slot key."""
    assert fixture._has_pressure({"queued": []}, 1025) is False, (
        "queue readiness short-circuits held-slot access"
    )


def test_pressure_waits_for_every_condition(
    fixture: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queue size, held slots and descriptor headroom must coincide."""
    statuses = [
        {"healthy": True, "queued": [], "held": 2},
        {"healthy": True, "queued": [None] * 158, "held": 1},
        {"healthy": True, "queued": [None] * 158, "held": 2},
        {"healthy": True, "queued": [None] * 158, "held": 2},
    ]
    query = mock.Mock(side_effect=statuses)
    enumeration = mock.Mock(
        side_effect=[iter(range(n)) for n in (1025, 1025, 1024, 1026)]
    )
    sleep = mock.Mock()
    monkeypatch.setattr(fixture, "query", query)
    monkeypatch.setattr(Path, "iterdir", enumeration)
    monkeypatch.setattr(fixture.time, "monotonic", mock.Mock(return_value=0))
    monkeypatch.setattr(fixture.time, "sleep", sleep)
    result = fixture._wait_for_pressure(42, Path("/disposable/socket"))
    assert result == 1026, "1024 descriptors do not exceed the old ceiling"
    assert query.call_count == 4, "all three pressure conditions are necessary"
    assert sleep.call_args_list == [mock.call(0.05)] * 3, "polling interval is retained"
    query.assert_called_with(env={"BUILD_LIMITS_SOCKET": "/disposable/socket"})


@pytest.mark.parametrize("healthy", [False, True], ids=["unhealthy", "timeout"])
def test_pressure_failure_preserves_observation_and_polling(
    fixture: ModuleType, monkeypatch: pytest.MonkeyPatch, *, healthy: bool
) -> None:
    """Unhealthy status fails immediately; timeout retains the last observation."""
    # Omit queue keys when unhealthy to prove health is checked first.
    status = (
        {"healthy": True, "queued": [], "held": 2} if healthy else {"healthy": False}
    )
    query = mock.Mock(return_value=status)
    sleep = mock.Mock()
    monkeypatch.setattr(fixture, "query", query)
    monkeypatch.setattr(Path, "iterdir", mock.Mock(return_value=iter(range(1024))))
    monkeypatch.setattr(fixture.time, "monotonic", mock.Mock(side_effect=[10, 10, 40]))
    monkeypatch.setattr(fixture.time, "sleep", sleep)
    expected = (
        f"did not retain >1024 descriptors: count=1024, status={status}"
        if healthy
        else f"unhealthy supervisor: {status}"
    )
    with pytest.raises(RuntimeError, match=r"supervisor|did not retain") as failure:
        fixture._wait_for_pressure(42, Path("/disposable/socket"))
    assert str(failure.value) == expected, "failure preserves the original observation"
    query.assert_called_once()
    assert sleep.call_args_list == ([mock.call(0.05)] if healthy else []), (
        "only a healthy non-ready observation sleeps before its deadline"
    )


@pytest.mark.parametrize("phase", ["construction", "submission", "pressure"])
def test_smoke_releases_children_when_setup_or_pressure_fails(
    fixture: ModuleType, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    """Setup and observation failures release children before temporary cleanup."""
    released: list[Path] = []
    failure = RuntimeError("controlled fixture failure")
    construct = mock.Mock(
        return_value="unused", side_effect=failure if phase == "construction" else None
    )
    submit = mock.Mock(
        return_value=[], side_effect=failure if phase == "submission" else None
    )
    pressure = mock.Mock(side_effect=failure)
    readers = mock.Mock()
    monkeypatch.setattr(fixture, "_child_script", construct)
    monkeypatch.setattr(fixture, "_submit_requests", submit)
    monkeypatch.setattr(fixture, "_wait_for_pressure", pressure)
    monkeypatch.setattr(fixture, "ThreadPoolExecutor", readers)
    monkeypatch.setattr(
        Path, "touch", mock.create_autospec(Path.touch, side_effect=released.append)
    )
    with pytest.raises(RuntimeError, match="controlled fixture failure"):
        fixture.smoke(Path("/disposable/socket"), 42)
    assert len(released) == 1 and released[0].name == "release", (
        "finally releases children"
    )
    assert not released[0].parent.exists(), "temporary directory still owns cleanup"
    readers.assert_not_called()
