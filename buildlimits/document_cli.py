"""Shared argument setup for commands that print status documents."""

import argparse


def document_parser(prog: str) -> argparse.ArgumentParser:
    """Build a status-document parser with the shared raw JSON option.

    The status and recovery commands both print one structured document. This
    helper owns only their common program name and ``--json`` switch; each
    command adds its own operational options.

    Parameters
    ----------
    prog : str
        The command name shown in help and usage.

    Returns
    -------
    argparse.ArgumentParser
        A parser with ``--json`` enabled.

    Examples
    --------
    >>> parser = document_parser("build-limits-status")
    >>> parser.prog, parser.parse_args(["--json"]).json
    ('build-limits-status', True)
    """
    parser = argparse.ArgumentParser(prog=prog)
    parser.add_argument("--json", action="store_true", help="print the raw document")
    return parser
