"""Shared command-line options for reading retained outcome history.

The replay and dataset commands read history with the same three options, so
they share one definition. ``--salt-id`` and ``--all-salts`` select a domain
and imply history mode; ``--history`` alone reads the newest domain across
archived, sealed and active segments. Without any of them a command keeps
reading only the active store file, which is the pre-archive behaviour.
"""

import typing as typ

from buildlimits.outcome_history import HistorySelection

if typ.TYPE_CHECKING:
    import argparse

__all__ = ["add_history_arguments", "selection_from"]


def add_history_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the history-domain options to one command parser.

    Parameters
    ----------
    parser : argparse.ArgumentParser
        The parser that gains ``--history``, ``--salt-id`` and ``--all-salts``.
    """
    parser.add_argument(
        "--history",
        action="store_true",
        help="read archived and sealed segments as well as the active store",
    )
    domain = parser.add_mutually_exclusive_group()
    domain.add_argument(
        "--salt-id",
        metavar="ID",
        help="read only this salt domain; implies --history",
    )
    domain.add_argument(
        "--all-salts",
        action="store_true",
        help="read every salt domain in append order; implies --history",
    )


def selection_from(options: argparse.Namespace) -> HistorySelection | None:
    """Read the requested domain from parsed options.

    Parameters
    ----------
    options : argparse.Namespace
        Parsed options carrying the history flags.

    Returns
    -------
    HistorySelection | None
        None when the caller asked for the active file only, otherwise the
        selected domain.

    Examples
    --------
    ``--salt-id abc`` reads that domain, while no flag at all returns None and
    leaves the command reading the active store alone.
    """
    salt_id = options.salt_id
    all_salts = options.all_salts
    names_a_domain = all_salts or salt_id is not None
    if not (names_a_domain or options.history):
        return None
    return HistorySelection(salt_id=salt_id, all_salts=all_salts)
