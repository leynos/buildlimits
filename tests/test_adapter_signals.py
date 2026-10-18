"""Invocation-local signal handlers restore the caller's dispositions."""

import contextlib
import signal
import typing as typ

import pytest

from buildlimits.adapter_signals import SIGNALS, observe_signals

if typ.TYPE_CHECKING:
    import collections.abc as cabc
    import types


@pytest.fixture
def known_handler() -> cabc.Iterator[
    cabc.Callable[[int, types.FrameType | None], None]
]:
    """Install known signal dispositions and restore the test process's originals.

    Yields
    ------
    Callable[[int, FrameType | None], None]
        The known disposition installed for every observed signal.
    """
    previous = {number: signal.getsignal(number) for number in SIGNALS}

    def handler(_number: int, _frame: types.FrameType | None) -> None:
        """Provide a recognizable disposition without delivering any signal."""

    try:
        for number in SIGNALS:
            signal.signal(number, handler)
        yield handler
    finally:
        for number, original in previous.items():
            signal.signal(number, original)


@pytest.mark.parametrize("exceptional", [False, True], ids=["normal", "exception"])
def test_observe_signals_restores_handlers(
    exceptional: bool,
    known_handler: cabc.Callable[[int, types.FrameType | None], None],
) -> None:
    """Temporary handlers restore known dispositions on either context exit."""
    expected_exit = (
        pytest.raises(RuntimeError, match="invocation failed")
        if exceptional
        else contextlib.nullcontext()
    )
    with expected_exit, observe_signals(lambda: None):
        assert all(
            callable(handler := signal.getsignal(number))
            and handler is not known_handler
            for number in SIGNALS
        ), "every observed signal has a temporary cancellation handler"
        if exceptional:
            msg = "invocation failed"
            raise RuntimeError(msg)
    assert all(signal.getsignal(number) is known_handler for number in SIGNALS), (
        "the caller's known signal handlers are restored"
    )
