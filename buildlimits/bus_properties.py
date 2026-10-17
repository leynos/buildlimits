"""Public user-bus fallback for manager load queries.

The fallback returns properties only after every requested value is validated;
a transport failure or malformed response must never become an idle server.
"""

import json
import os
import typing as typ

if typ.TYPE_CHECKING:
    import subprocess

    from buildlimits.systemd import Runner

SERVICE = "org.freedesktop.systemd1"
ROOT = "/org/freedesktop/systemd1"


class BusQueryError(Exception):
    """The public user bus cannot establish the unit's complete property set."""


def busctl_argv(*arguments: str) -> list[str]:
    """Build one bounded ``busctl`` query against the owner's user bus.

    The address names the owner's session bus explicitly rather than relying on
    ``DBUS_SESSION_BUS_ADDRESS``, because a supervisor started before the
    variable was set would otherwise fall back to the system bus and read a
    different manager's units. The three bounds in front are fixed for every
    caller; the arguments are the subcommand and its operands.

    Kept separate from the call so a test can assert the whole argument vector
    a caller produces without a runner in the way: the installed ``busctl``
    rejects unknown options outright, and a vector this module only ever
    passes to an injected runner would stay unexercised without that seam.

    Parameters
    ----------
    *arguments : str
        The subcommand and its operands.

    Returns
    -------
    list[str]
        The argument vector.
    """
    return [
        "busctl",
        "--timeout=3s",
        "--json=short",
        f"--address=unix:path=/run/user/{os.getuid()}/bus",
        *arguments,
    ]


def _value(runner: Runner, *arguments: str) -> dict[str, object]:
    """Run a typed JSON bus query, retaining only bounded fault context.

    Returns
    -------
    dict[str, object]
        Typed JSON object from the bounded manager response.

    """
    result = runner(busctl_argv(*arguments))
    _require_success(result)
    return _decoded_object(result.stdout)


def _require_success(result: subprocess.CompletedProcess[str]) -> None:
    """Reject failed transport responses using only bounded escaped fault context.

    Raises
    ------
    BusQueryError
        If the public manager command failed.
    """
    if result.returncode:
        msg = f"public user bus: {ascii(result.stderr.strip()[:240])[1:-1]}"
        raise BusQueryError(msg)


def _decoded_object(text: str) -> dict[str, object]:
    """Decode a JSON object independently of command and D-Bus type validation.

    Returns
    -------
    dict[str, object]
        Decoded typed response object.

    Raises
    ------
    BusQueryError
        If response JSON is malformed or does not encode an object.
    """
    try:
        value = json.loads(text)
    except (ValueError, TypeError) as error:
        msg = "public user bus returned invalid JSON"
        raise BusQueryError(msg) from error
    if not isinstance(value, dict):
        msg = "public user bus returned no typed property"
        raise BusQueryError(msg)
    return value


def _snapshot(runner: Runner, path: str, interface: str) -> dict[str, object]:
    """Read one typed property snapshot without logging its contents.

    Returns
    -------
    dict[str, object]
        Validated property map from one manager response.

    Raises
    ------
    BusQueryError
        If the response has no typed property map.
    """  # ruff: ignore[docstring-extraneous-exception] - _value and _single_typed raise it.
    response = _value(
        runner,
        "call",
        SERVICE,
        path,
        "org.freedesktop.DBus.Properties",
        "GetAll",
        "s",
        interface,
    )
    return _single_typed(
        response, "a{sv}", dict, "public user bus returned an invalid property snapshot"
    )


def _unit_path(runner: Runner, unit: str) -> str:
    """Resolve a service within the manager's unit object namespace.

    Returns
    -------
    str
        Validated systemd unit object path.

    Raises
    ------
    BusQueryError
        If the manager response has no valid unit object path.
    """
    response = _value(
        runner, "call", SERVICE, ROOT, SERVICE + ".Manager", "GetUnit", "s", unit
    )
    path = _single_path(response)
    if not path.startswith(ROOT + "/unit/"):
        msg = "public user bus returned an invalid unit object path"
        raise BusQueryError(msg)
    return path


def _single_path(response: dict[str, object]) -> str:
    """Validate the typed single object path before checking its namespace.

    Returns
    -------
    str
        The manager's object path.

    Raises
    ------
    BusQueryError
        If the response does not contain exactly one typed string path.
    """  # ruff: ignore[docstring-extraneous-exception] - _single_typed raises it.
    return _single_typed(
        response, "o", str, "public user bus returned an invalid unit object path"
    )


def service_properties(
    runner: Runner, unit: str, properties: tuple[str, ...]
) -> dict[str, str]:
    """Query sccache service load properties through the public manager bus.

    Parameters
    ----------
    runner : Runner
        Injectable fixed-argv command runner.
    unit : str
        Service unit whose properties are needed.
    properties : tuple[str, ...]
        MainPID, ControlGroup and/or ActiveState; unsupported values fail explicitly.

    Returns
    -------
    dict[str, str]
        Validated properties in systemctl's string form.

    Raises
    ------
    BusQueryError
        If a property is unsupported, a response malformed, or the bus refuses.
    """
    types = {"MainPID": "u", "ControlGroup": "s", "ActiveState": "s"}
    if any(prop not in types for prop in properties):
        msg = "public user-bus fallback supports only service load properties"
        raise BusQueryError(msg)
    path = _unit_path(runner, unit)
    # MainPID/cgroup come from one service snapshot. Unit state is sampled
    # afterwards: a newly active service cannot be mistaken for a stopped one.
    entries = _snapshot(runner, path, SERVICE + ".Service")
    if "ActiveState" in properties:
        entries["ActiveState"] = _snapshot(runner, path, SERVICE + ".Unit").get(
            "ActiveState"
        )
    return {
        prop: _property_value(prop, entries.get(prop), types[prop])
        for prop in properties
    }


def _property_value(prop: str, response: object, expected: str) -> str:
    """Validate one typed service property before converting it to systemctl form.

    Returns
    -------
    str
        Validated property value.

    Raises
    ------
    BusQueryError
        If a property is missing, malformed or has the wrong D-Bus type.
    """
    if not isinstance(response, dict):
        msg = f"public user bus omitted {prop}"
        raise BusQueryError(msg)
    data = response.get("data")
    valid = _valid_pid(data) if prop == "MainPID" else isinstance(data, str)
    if response.get("type") != expected or not valid:
        msg = f"public user bus returned an invalid {prop}"
        raise BusQueryError(msg)
    return str(data)


def _valid_pid(data: object) -> bool:
    """Require a non-negative integer PID, excluding JSON booleans.

    Returns
    -------
    bool
        Whether the value satisfies the manager PID contract.
    """
    return isinstance(data, int) and not isinstance(data, bool) and data >= 0


def control_unit(runner: Runner, verb: str, unit: str, mode: str = "replace") -> None:
    """Queue a unit control job on the public user bus.

    ``StartUnit``/``StopUnit`` are queued by the manager itself: the method
    returns the job's object path as soon as the job is accepted, rather than
    waiting for the unit to reach the requested state. That reply is therefore
    both the evidence this call needs and the reason no ``--no-block``
    equivalent is passed — ``busctl`` has no such option, and
    ``--expect-reply=no`` would discard the very job path validated below.
    An accepted job is not proof that the unit reached the requested state, so
    callers must still verify the unit's properties.

    Parameters
    ----------
    runner : Runner
        Injectable fixed-argv command runner.
    verb : str
        The control verb, ``stop`` or ``start``.
    unit : str
        The unit to control.
    mode : str
        The job mode; ``replace`` supersedes a conflicting pending job.

    Raises
    ------
    BusQueryError
        If the bus refuses the job or the response is not a job object path.
    """  # ruff: ignore[docstring-extraneous-exception] - _value raises it indirectly.
    response = _value(runner, *control_unit_argv(verb, unit, mode))
    _single_path(response)


def control_unit_argv(verb: str, unit: str, mode: str = "replace") -> list[str]:
    """Build the ``busctl`` call that queues one unit control job.

    Parameters
    ----------
    verb : str
        The control verb, ``stop`` or ``start``.
    unit : str
        The unit to control.
    mode : str
        The job mode; ``replace`` supersedes a conflicting pending job.

    Returns
    -------
    list[str]
        The ``call`` operands, without the leading ``busctl`` options.
    """
    return [
        "call",
        SERVICE,
        ROOT,
        SERVICE + ".Manager",
        f"{verb.title()}Unit",
        "ss",
        unit,
        mode,
    ]


def _single_entry(response: dict[str, object], signature: str, message: str) -> object:
    """Require the manager's typed single-element D-Bus result envelope.

    Returns
    -------
    object
        The single result, ready for property-specific value validation.

    Raises
    ------
    BusQueryError
        If the envelope has the wrong signature, shape or result count.
    """
    entries = response.get("data")
    if response.get("type") != signature or not isinstance(entries, list):
        raise BusQueryError(message)
    if len(entries) != 1:
        raise BusQueryError(message)
    return entries[0]


def _single_typed[T](
    response: dict[str, object], signature: str, kind: type[T], message: str
) -> T:
    """Require a typed single-element result whose value is a ``kind``.

    Returns
    -------
    T
        The single result, checked to be an instance of ``kind``.

    Raises
    ------
    BusQueryError
        If the envelope is malformed or its value has another type.
    """
    entry = _single_entry(response, signature, message)
    if not isinstance(entry, kind):
        raise BusQueryError(message)
    return entry
