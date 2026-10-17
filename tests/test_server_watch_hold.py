"""The recovery hold on cache composition, and what it may not do.

A recovery transaction stops the managed server, so between that stop request
and the observed restart no group may be composed with the server. The watch
answers that with a denial deadline, and every property here is about the
asymmetry of that deadline: it can only ever *withhold* composition. It never
proves anything about the server, and it must never stand between a verified
stopped-and-empty reading and the token that reading releases — a hold that
withheld those would deadlock the transaction that takes it, because the
accounting phase waits for exactly those tokens.

:mod:`test_server_watch` covers what each probe answers on its own; this module
covers composition permission across the interval in which no probe is
trustworthy. The one probe that is: the reading the release itself takes, since
the restart it follows is issued as a queued job and acceptance of that job is
no evidence the server came back. A sibling module keeps both under the
module-size ceiling.
"""

import typing as typ

from test_server_watch import FakeServer, _draining, _watch

from buildlimits.admission import ReturnToken
from buildlimits.sccache import SccacheError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.server_watch import ServerWatch


def _held_watch(server: FakeServer | None = None) -> tuple[ServerWatch, list[float]]:
    """Watch a fake server through a clock the test drives.

    The clock is a one-element cell read back by a lambda — the shape the
    sibling server-watch modules already use — so a test writes ``now[0]`` to
    move time rather than carrying a class whose only job is to hand the number
    back.

    Parameters
    ----------
    server : FakeServer or None
        The scripted server, or a reachable idle one.

    Returns
    -------
    tuple[ServerWatch, list[float]]
        The watch and the one-element clock its hold is measured against.
    """
    now = [0.0]
    watch = _watch(server or FakeServer())
    watch.clock = lambda: now[0]
    return watch, now


def test_a_hold_withholds_composition_until_its_deadline() -> None:
    """Composition is denied for the interval the hold names, and no longer."""
    watch, now = _held_watch()
    watch.hold_cache(30.0)

    assert not watch.cache_allowed(), "a fresh hold denies composition at once"
    now[0] = 29.999
    assert not watch.cache_allowed(), "the deadline is not rounding to the second"
    now[0] = 30.0
    assert watch.cache_allowed(), "a lapsed hold permits composition again"


def test_a_hold_cannot_be_shortened_but_may_be_extended() -> None:
    """Only the furthest deadline survives, so no caller can cut another's hold."""
    watch, now = _held_watch()
    watch.hold_cache(60.0)  # a deadline at 60
    watch.hold_cache(10.0)  # a deadline at 10, which must not shorten it
    now[0] = 50.0
    assert not watch.cache_allowed(), "a shorter renewal cannot cancel a live hold"

    watch.hold_cache(60.0)  # a deadline at 110, the furthest renewal
    now[0] = 109.0
    assert not watch.cache_allowed(), "a longer renewal extends the only deadline"
    now[0] = 110.0
    assert watch.cache_allowed(), "the extended deadline is the one that lapses"


def test_releasing_a_hold_decides_composition_on_a_fresh_reading() -> None:
    """The release ends the deadline *and* takes the reading that replaces it.

    The restart is issued as a queued job, so acceptance is not evidence the
    server is back. A release that merely ended the deadline would reinstate
    the reading taken before the stop, and a token granted before the loop's
    next poll — seconds later, and the whole transaction fits inside one poll
    interval — would be composed with a server that is still coming up.
    """
    watch, now = _held_watch(FakeServer(reachable=True))
    watch.hold_cache(600.0)
    watch.release_cache()

    assert watch.cache_allowed(), "a server answering again permits composition"
    now[0] = 1.0
    watch.refresh_cache_mode()
    assert watch.cache_allowed(), "and the next poll agrees with that reading"


def test_a_release_while_the_server_is_still_down_keeps_composition_denied() -> None:
    """Acceptance of the start job is not readiness of the managed server.

    ``cache_allowed`` answers only the deadline clause; composition is that
    clause *and* the reading the watch last took. So the assertion that matters
    is on both: the hold has genuinely lapsed — this is not a deadline winning
    by accident — and the release's own probe is what still says no.
    """
    server = FakeServer(reachable=False)
    watch, now = _held_watch(server)
    watch.hold_cache(600.0)
    watch.release_cache()

    assert watch.cache_allowed(), "the hold has lapsed rather than survived"
    assert watch.admission.cached is False, "the release's probe found no server"

    # And it is the reading, not a deadline, that is holding: the server has to
    # answer before composition resumes, however long that takes.
    now[0] = 10_000.0
    assert not watch.admission.cached, "no deadline expiry can conjure a server"

    # The fake is held directly rather than reached through the watch, whose
    # ``server`` field is typed as the real server.
    server.reachable = True
    watch.refresh_cache_mode()
    assert watch.admission.cached, "the loop's poll lifts the denial on evidence"


def test_a_direct_host_never_composes_with_the_cache() -> None:
    """The hold is one clause of the answer, not the whole of it."""
    server = FakeServer(reachable=True)
    watch = _watch(server, mode="direct")
    watch.clock = lambda: 0.0
    watch.refresh_cache_mode()
    watch.release_cache()

    assert not watch.cache_allowed(), "a host that does not cache never composes"


def test_a_hold_never_withholds_the_token_a_verified_drain_releases() -> None:
    """Deadline denial cannot stand between a proof and the token it returns.

    This is the direction that keeps the hold from deadlocking the transaction
    that takes it: accounting waits precisely for these tokens, so a hold that
    could withhold one would make the stop it protects unending. Time may deny
    composition; it may never deny an accounted return.
    """
    server = FakeServer(stopped=True)
    watch = _watch(server, _draining(b"a"))
    watch.clock = lambda: 0.0
    watch.hold_cache(600.0)

    effects = watch.drained_tokens()
    returned = [effect for effect in effects if isinstance(effect, ReturnToken)]
    assert returned == [ReturnToken(b"a")], (
        "the verified stopped-and-empty reading still returns its token"
    )
    assert not watch.cache_allowed(), "the return changes nothing about the hold"


def test_a_hold_keeps_its_denial_while_the_manager_is_unreadable() -> None:
    """An unreadable server is no reason to shorten a hold that is in force."""
    server = FakeServer(reachable=SccacheError("Connection timed out"))
    watch, now = _held_watch(server)
    watch.hold_cache(120.0)
    watch.refresh_cache_mode()

    assert not watch.cache_allowed(), "the unreachable reading agrees with the hold"
    now[0] = 120.0
    assert watch.cache_allowed(), "the lapsed hold still leaves the probe's answer"

    effects: cabc.Iterable[object] = watch.drained_tokens()
    assert not list(effects), "an unreadable server accounts for nothing"
