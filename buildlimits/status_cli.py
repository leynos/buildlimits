"""The operator command, ``build-limits-status``.

The command fetches the status document through
:func:`buildlimits.status_client.query` and prints it, either rendered by
:func:`buildlimits.status_render.render` or as raw JSON. Its exit status says
whether the supervisor answered and is healthy. The optional ``--ready``
contract additionally reports whether admission can make progress.
"""

import json
import sys
import typing as typ

from buildlimits import status_client
from buildlimits.document_cli import document_parser
from buildlimits.status_render import render

if typ.TYPE_CHECKING:
    import collections.abc as cabc


_READY_HELP = (
    "require admission readiness. With --ready, exit 0 means healthy and ready; "
    "exit 1 means a query or health failure; exit 3 means not ready or not "
    "reported. Without --ready, exit 0 means healthy and exit 1 means query or "
    "health failure."
)


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
        Without ``--ready``, 0 when healthy and 1 otherwise. With ``--ready``,
        0 when healthy and admission-ready, 1 on query or health failure, and
        3 when readiness is false or not reported.
    """
    parser = document_parser("build-limits-status")
    parser.add_argument(
        "--ready",
        action="store_true",
        help=_READY_HELP,
    )
    options = parser.parse_args(argv)
    try:
        state = query()
    except status_client.StatusQueryError as error:
        print(f"build-limits-status: {error}", file=sys.stderr)
        return 1
    print(json.dumps(state, indent=2) if options.json else render(state))
    return _ready_exit(state) if options.ready else (0 if state.get("healthy") else 1)


def _ready_exit(state: dict[str, object]) -> int:
    """Map one status document to the readiness exit contract.

    Parameters
    ----------
    state : dict[str, object]
        The status document returned by the supervisor.

    Returns
    -------
    int
        1 for unhealthy status, 0 when ready, or 3 when not ready or unreported.

    Examples
    --------
    >>> _ready_exit({"healthy": False})
    1
    >>> _ready_exit({"healthy": True, "admission": {"ready": True}})
    0
    >>> _ready_exit({"healthy": True, "admission": {"ready": False}})
    3
    """
    if not state.get("healthy"):
        return 1
    admission = state.get("admission")
    match admission:
        case {"ready": bool() as ready}:
            return 0 if ready else 3
        case _:
            print(
                "build-limits-status: admission readiness is not reported",
                file=sys.stderr,
            )
            return 3
