"""The supervisor's decision to compose new groups with the cache.

:mod:`test_server_watch` covers the drain and fault-kind questions the same
watch answers. This module covers the third question -- whether new groups may
be composed with the cache -- in both halves: which port answers it for each
mode, and the one-bit memory that keeps a lone slow answer from withdrawing
composition. A sibling module keeps both under the repository's module-size
ceiling.
"""

import pytest
from test_server_watch import FakeServer, _watch

from buildlimits.admission import Admission
from buildlimits.sccache import SccacheError
from buildlimits.server_watch import UNREACHABLE_POLLS_BEFORE_DIRECT


@pytest.mark.parametrize(
    ("reachable", "mode", "cached"),
    [
        (True, "sccache", True),
        (False, "sccache", False),
        (SccacheError("cannot probe s.sock: EACCES"), "sccache", False),
        (True, "kache", True),
        (False, "kache", False),
        (SccacheError("cannot probe /run/kache/kache.sock"), "kache", False),
        (True, "direct", False),
    ],
    ids=[
        "reachable",
        "stopped",
        "probe-fault",
        "kache",
        "kache-unavailable",
        "kache-fault",
        "direct",
    ],
)
def test_new_groups_cache_only_through_a_reachable_server(
    reachable: bool | SccacheError, mode: str, cached: bool
) -> None:
    """A probe fault falls back to direct compilation like a stopped server.

    The probe is asked of whichever backend the mode selected, so an in-scope
    cache is composed on its own health rather than on a server this supervisor
    does not own. The managed server is scripted to the opposite answer, so a
    reading taken from the wrong port cannot produce the expected result.

    The state starts *against* the expected answer, which is what makes each
    row a transition: withdrawing composition needs the two consecutive
    failures :data:`UNREACHABLE_POLLS_BEFORE_DIRECT` requires, while restoring
    it takes the first reachable reading.
    """
    admission = Admission(cached=not cached)
    watch = _watch(
        FakeServer(reachable=reachable is not True),
        admission,
        mode,
        backend=FakeServer(reachable=reachable),
    )
    for _ in range(UNREACHABLE_POLLS_BEFORE_DIRECT):
        watch.refresh_cache_mode()
    assert admission.cached is cached, "the cache mode follows the probe"


def test_one_slow_probe_does_not_withdraw_composition() -> None:
    """A lone timeout is not evidence that the daemon has stopped.

    The probe is bounded at half a second, so a daemon answering slowly under
    load is indistinguishable from a stopped one in a single reading. Acting
    on that reading would clear ``admission.cached`` and every group composed
    before the next poll -- up to five seconds -- would compile directly with
    the fallback notice. So the reading it settles on stays cached.
    """
    server = FakeServer(reachable=True)
    watch = _watch(server)
    watch.refresh_cache_mode()
    assert watch.admission.cached, "a reachable daemon composes"

    server.reachable = False
    watch.refresh_cache_mode()
    assert watch.admission.cached, "one timeout has not yet withdrawn composition"


def test_repeated_unreachable_polls_do_withdraw_composition() -> None:
    """A daemon that keeps not answering is treated as stopped.

    Without this the debounce would be a cache that never falls back. Two
    consecutive failures is the smallest number that tells a lone slow answer
    from a daemon that has stopped, and it bounds the fallback to two poll
    intervals.
    """
    server = FakeServer(reachable=True)
    watch = _watch(server)
    watch.refresh_cache_mode()

    server.reachable = False
    for _ in range(UNREACHABLE_POLLS_BEFORE_DIRECT - 1):
        watch.refresh_cache_mode()
    assert watch.admission.cached, "the failures have not reached the threshold yet"

    watch.refresh_cache_mode()
    assert not watch.admission.cached, "the last failure withdrew composition"


def test_a_reachable_probe_restores_composition_immediately() -> None:
    """Coming back is never the unsafe direction, so it needs no second reading.

    A debounce that applied symmetrically would keep compiling directly for a
    further poll after the daemon answered again, which is lost caching rather
    than protection.
    """
    server = FakeServer(reachable=False)
    watch = _watch(server)
    for _ in range(UNREACHABLE_POLLS_BEFORE_DIRECT):
        watch.refresh_cache_mode()
    assert not watch.admission.cached, "the daemon is not answering"

    server.reachable = True
    watch.refresh_cache_mode()
    assert watch.admission.cached, "the first reachable reading restores composition"
