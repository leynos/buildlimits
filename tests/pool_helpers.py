"""Scripted token-pool double shared by supervisor pressure tests."""

import dataclasses as dc
import os
import typing as typ

from buildlimits import pool as pool_module

if typ.TYPE_CHECKING:
    import threading

    import pytest

    from buildlimits.pool import PoolEvent, TokenPool


class _PoolOwner(typ.Protocol):
    """Anything that holds the token pool the supervisor reads."""

    pool: TokenPool | None


@dc.dataclass(slots=True)
class FakePool:
    """A scripted token pool that never touches a real device.

    Parameters
    ----------
    wake_fd : int
        Descriptor registered with the selector, or -1 when unused.
    same_device : bool
        Result of device-identity checks.
    give_back_error : OSError or None
        Optional fault injected when a token is returned.
    returned : list[bytes]
        Tokens successfully returned by the supervisor.
    requested : int
        Number of token requests.
    device_checks : int
        Number of device-identity checks.
    scripted : list[PoolEvent]
        Events consumed on the next poll.
    capacity : int
        The configured slot count a token-conservation assertion reads.
    outstanding : int
        Tokens borrowed but not yet returned, so a test can observe the
        maximum the supervisor ever held at once.
    max_held : int
        The largest number of tokens ever outstanding at the same time.
    """

    wake_fd: int
    same_device: bool = True
    give_back_error: OSError | None = None
    returned: list[bytes] = dc.field(default_factory=list)
    requested: int = 0
    device_checks: int = 0
    scripted: list[PoolEvent] = dc.field(default_factory=list)
    capacity: int = 0
    outstanding: int = 0
    max_held: int = 0
    is_closed: bool = False

    def close(self) -> None:
        """Record that the owner released the pool; the double holds nothing."""
        self.is_closed = True

    def events(self) -> list[PoolEvent]:
        """Return and clear the scripted events.

        Returns
        -------
        list[PoolEvent]
            Events owned by this poll, consumed exactly once.
        """
        events, self.scripted = self.scripted, []
        return events

    def request(self) -> None:
        """Count a token request and the borrow it is about to receive.

        A request yields exactly one token through :meth:`events`, so counting
        the borrow here keeps the outstanding balance in step with the returns
        a supervisor records, and the maximum is what a conservation assertion
        compares against the configured capacity.
        """
        self.requested += 1
        self.outstanding += 1
        self.max_held = max(self.max_held, self.outstanding)

    def give_back(self, token: bytes) -> None:
        """Record a return, or raise the scripted failure.

        Parameters
        ----------
        token : bytes
            Pool token returned by admission.

        Raises
        ------
        OSError
            If the configured return fault is active.
        """  # ruff: ignore[docstring-extraneous-exception] - the injected fault is typed OSError.
        if self.give_back_error is not None:
            raise self.give_back_error
        self.returned.append(token)
        self.outstanding -= 1

    def is_same_device(self) -> bool:
        """Count a device check and report the scripted identity.

        Returns
        -------
        bool
            Whether the device still refers to the original pool.
        """
        self.device_checks += 1
        return self.same_device


def attach_pool(owner: _PoolOwner) -> FakePool:
    """Give ``owner`` a scripted pool with no wake descriptor.

    Parameters
    ----------
    owner : _PoolOwner
        The supervisor, or any holder of its pool, that should read the double.

    Returns
    -------
    FakePool
        The attached double, for scripting events and reading returns.
    """
    pool = FakePool(wake_fd=-1)
    owner.pool = typ.cast("TokenPool", pool)
    return pool


def announce_reads(
    patch: pytest.MonkeyPatch, device: int, entered: threading.Event
) -> None:
    """Set ``entered`` when the pool's reader thread calls ``os.read`` on ``device``.

    The reader announces a read only after it has passed its closing check, so a
    test that waits on the event knows the thread is inside, or about to enter,
    the blocking call rather than guessing at thread timing.

    Parameters
    ----------
    patch : pytest.MonkeyPatch
        Installs the wrapper and removes it at the end of the test.
    device : int
        The pool's device descriptor; reads on other descriptors are untouched.
    entered : threading.Event
        Set on the first read of ``device``.
    """
    real_read = os.read

    def announcing_read(fd: int, length: int) -> bytes:
        """Tell the test the reader is past its closing check, then really read.

        Returns
        -------
        bytes
            What the real read returned.
        """
        if fd == device:
            entered.set()
        return real_read(fd, length)

    patch.setattr(pool_module.os, "read", announcing_read)
