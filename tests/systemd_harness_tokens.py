"""Read-only token observations for the real-manager test harness."""

import array
import fcntl
import termios
import typing as typ


class TokenHarness(typ.Protocol):
    """Harness fields needed to inspect the already-open token FIFO."""

    _holder: int | None


class TokenObservationMixin:
    """Expose FIFO availability without taking or returning a token."""

    def free_tokens(self: TokenHarness) -> int:
        """Read the pool without taking a token, as an observer would.

        Returns
        -------
        int
            Bytes waiting in the FIFO. A held token is absent from this count.
        """
        if self._holder is None:
            return 0
        pending = array.array("i", [0])
        # FIONREAD's fourth argument is the kernel's all-or-nothing flag.
        fcntl.ioctl(
            self._holder,
            termios.FIONREAD,
            pending,
            True,  # ruff: ignore[boolean-positional-value-in-call] - kernel flag.
        )
        return int(pending[0])
