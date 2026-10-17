"""The operator command, ``build-limits-status``.

The command fetches the status document through
:func:`buildlimits.status_client.query` and prints it, either rendered by
:func:`buildlimits.status_render.render` or as raw JSON. Its exit status says
whether the supervisor answered and is healthy, so scripts can test it.
"""

import argparse
import json
import sys
import typing as typ

from buildlimits import status_client
from buildlimits.status_render import render

if typ.TYPE_CHECKING:
    import collections.abc as cabc


def main(
    argv: cabc.Sequence[str] | None = None,
    query: cabc.Callable[[], dict[str, object]] = status_client.query,
) -> int:
    """Print the supervisor's status.

    Parameters
    ----------
    argv : Sequence[str] | None
        Command-line arguments; ``sys.argv[1:]`` when ``None``.
    query : Callable[[], dict[str, object]]
        Fetches the status document.

    Returns
    -------
    int
        0 when the supervisor answered and is healthy, 1 otherwise.
    """
    parser = argparse.ArgumentParser(prog="build-limits-status")
    parser.add_argument("--json", action="store_true", help="print the raw document")
    options = parser.parse_args(argv)
    try:
        state = query()
    except status_client.StatusQueryError as error:
        print(f"build-limits-status: {error}", file=sys.stderr)
        return 1
    print(json.dumps(state, indent=2) if options.json else render(state))
    return 0 if state.get("healthy") else 1
