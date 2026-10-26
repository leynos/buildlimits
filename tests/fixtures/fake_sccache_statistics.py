"""Statistics protocol and counters for the fake sccache process tree."""

import json
import pathlib
import socket
import sys
import threading
import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc


class ServerState(typ.TypedDict):
    """Persistent fake-server counters and scripted-result position."""

    counters: dict[str, int]
    script_index: int
    statistics_queries: int


COUNTER_NAMES = (
    "compile_requests",
    "requests_unsupported_compiler",
    "requests_not_compile",
    "requests_not_cacheable",
    "requests_executed",
    "cache_hits",
    "cache_misses",
    "cache_errors",
    "non_cacheable_compilations",
    "compilations",
    "compile_fails",
)
STATE_LOCK = threading.Lock()


def write_statistics() -> int:
    """Write the pinned 0.14.0 fixture when no server can be queried.

    Returns
    -------
    int
        Zero after writing the fixture.
    """
    fixture = pathlib.Path(__file__).with_name("sccache_0_14_0_stats.json")
    sys.stdout.write(fixture.read_text(encoding="utf-8"))
    return 0


def statistics(counters: cabc.Mapping[str, int]) -> dict[str, object]:
    """Build the exact counter shape accepted by the 0.14 parser.

    Returns
    -------
    dict[str, object]
        Counter values with nested cache-counter objects.
    """
    values: dict[str, object] = dict(counters)
    for name in ("cache_hits", "cache_misses", "cache_errors"):
        count = counters[name]
        values[name] = {"counts": {"Rust": count}, "adv_counts": {"rust": count}}
    return values


def statistics_delay(
    socket_path: str,
    state: ServerState,
    *,
    save_state: cabc.Callable[[str, ServerState], None],
    configuration: cabc.Callable[[str], dict[str, object]],
) -> float:
    """Count an in-flight query and consume one configured delay.

    Returns
    -------
    float
        The requested delay, or zero when no delayed query remains.
    """
    path = pathlib.Path(socket_path).with_name("fake-sccache-config.json")
    with STATE_LOCK:
        state["statistics_queries"] += 1
        save_state(socket_path, state)
        options = configuration(socket_path)
        remaining = options.get("statistics_delay_count", 0)
        delay = options.get("statistics_delay_seconds", 0.0)
        if isinstance(remaining, int) and remaining > 0:
            options["statistics_delay_count"] = remaining - 1
            path.write_text(json.dumps(options), encoding="utf-8")
            return float(delay) if isinstance(delay, (int, float)) else 0.0
    return 0.0


def request_statistics(env: cabc.Mapping[str, str]) -> int:
    """Query one running fake server, falling back to the pinned fixture.

    Returns
    -------
    int
        The status from the server query or fixture output.
    """
    socket_path = env.get("SCCACHE_SERVER_UDS")
    if socket_path is None:
        return write_statistics()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            conn.connect(socket_path)
            with conn.makefile("rwb") as stream:
                stream.write(json.dumps({"operation": "statistics"}).encode() + b"\n")
                stream.flush()
                reply = json.loads(stream.readline())
    except OSError:
        return write_statistics()
    sys.stdout.write(json.dumps(reply))
    return 0
