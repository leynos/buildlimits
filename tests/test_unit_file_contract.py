"""The unit directives a graceful restart relies on are pinned in three places.

The provisioned units are managed outside this repository, so the restart's
safety rests on directives this repository cannot enforce. The user's guide
lists the ones it depends on; the fixture units that every real-systemd
scenario runs mirror the role's template. This test ties the two together, so a
change to one that the other does not follow fails here instead of on a host.
"""

import re
from pathlib import Path

import pytest
from cached_unit_files import cache_unit_text, supervisor_unit_text

GUIDE = Path(__file__).parents[1] / "docs" / "users-guide.md"
RUNTIME = Path("/run/user/1000")
type Unit = dict[str, dict[str, list[str]]]


def parse_unit(text: str) -> Unit:
    """Parse unit-file text into sections of key to ordered values.

    Returns
    -------
    dict[str, dict[str, list[str]]]
        Each section's directives; a repeated key keeps every value.
    """
    unit: Unit = {}
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            unit.setdefault(section, {})
            continue
        key, _, value = line.partition("=")
        unit.setdefault(section, {}).setdefault(key, []).append(value)
    return unit


def documented_units() -> list[Unit]:
    """Read the guide's ``ini`` blocks under "Units the restart relies on".

    Returns
    -------
    list[Unit]
        One parsed unit per block, in document order.
    """
    text = GUIDE.read_text(encoding="utf-8")
    heading = "### Units the restart relies on"
    section = text.split(heading, 1)[1]
    section = re.split(r"\n#{1,3} ", section, maxsplit=1)[0]
    return [
        parse_unit(block)
        for block in re.findall(r"```ini\n(.*?)```", section, re.DOTALL)
    ]


@pytest.fixture
def supervisor() -> Unit:
    """Render the fixture supervisor unit as every scenario installs it."""
    return parse_unit(
        supervisor_unit_text(
            sccache_unit="c.service", parent_slice="p.slice", exec_start='"/x"'
        )
    )


@pytest.fixture
def cache() -> Unit:
    """Render the fixture cache unit as every scenario installs it."""
    return parse_unit(
        cache_unit_text(
            supervisor_unit="s.service",
            parent_slice="p.slice",
            runtime=RUNTIME / "buildlimits",
            exec_start='"/y"',
        )
    )


def test_the_guide_documents_a_service_block_and_a_barrier_block() -> None:
    """The guide lists both what the supervisor and what the cache must carry."""
    service, barrier = documented_units()
    assert set(service) == {"Service"}, "the first block is the supervisor's service"
    assert service["Service"]["TimeoutStopSec"] == ["infinity"], (
        "the guide requires that systemd waits as long as running groups take"
    )
    assert service["Service"]["KillMode"] == ["mixed"], (
        "the guide requires that a stop signals only the supervisor, so a helper "
        "process of a scope placement is not killed mid-call"
    )
    assert set(barrier) == {"Unit"}, "the second block is the cache's barrier"


def test_the_fixture_supervisor_carries_every_documented_directive(
    supervisor: Unit,
) -> None:
    """Whatever the guide says the restart needs, the fixture unit has."""
    documented = documented_units()[0]["Service"]
    for key, values in documented.items():
        assert supervisor["Service"].get(key) == values, (
            f"{key} in the guide differs from the fixture supervisor unit"
        )


def test_the_documented_barrier_is_the_fixture_caches_condition(cache: Unit) -> None:
    """The cache stays down while the barrier exists, as the guide says."""
    documented = documented_units()[1]["Unit"]["ConditionPathExists"]
    expected = [value.replace("%t", str(RUNTIME)) for value in documented]
    assert cache["Unit"]["ConditionPathExists"] == expected, (
        "the guide's barrier path is the one the fixture cache honours"
    )


def test_the_cache_unit_keeps_the_default_kill_mode(cache: Unit) -> None:
    """The cache's stop is the recovery's stop, which wants its group emptied."""
    assert "KillMode" not in cache.get("Service", {}), (
        "control-group signals the server and its detached compile children "
        "together, which is what the stopped-and-empty proof relies on"
    )


def test_no_reload_verb_exists_to_bypass_the_graceful_stop(supervisor: Unit) -> None:
    """The restart is the one command; there is no second, unsafe way."""
    assert "ExecReload" not in supervisor["Service"], "no reload hook is declared"


@pytest.mark.parametrize(
    "key", ["PartOf", "BindsTo", "Requires", "Requisite", "BoundBy", "Upholds"]
)
def test_restarting_the_supervisor_never_restarts_the_cache_implicitly(
    supervisor: Unit, cache: Unit, key: str
) -> None:
    """The cache is ordered after the supervisor but never bound to it."""
    assert key not in cache["Unit"], f"the cache must not carry {key}"
    assert key not in supervisor["Unit"], f"the supervisor must not carry {key}"
    assert supervisor["Unit"]["Wants"] == ["c.service"], "the supervisor only wants it"
