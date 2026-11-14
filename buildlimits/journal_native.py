"""Best-effort native journal datagrams, without memfd or binary fields."""

import socket
import typing as typ
from pathlib import Path

if typ.TYPE_CHECKING:
    import collections.abc as cabc

MAX_DATAGRAM_BYTES = 8192
DEFAULT_SOCKET = Path("/run/systemd/journal/socket")


class JournalSink:
    """Send bounded ASCII fields through one lazy non-blocking socket.

    Parameters
    ----------
    path : Path
        The injected journal socket path.
    identifier : str | None
        The optional journal identifier; fixed by the composition root.

    Notes
    -----
    A missing socket sends nothing and reports the record as undelivered. The
    next attempt probes again, so a journal socket which appears later can
    receive records, and a caller is never told a record arrived when none did.
    """

    def __init__(
        self, path: Path = DEFAULT_SOCKET, *, identifier: str | None = None
    ) -> None:
        """Bind a path without opening any descriptor."""
        self.path = path
        self.identifier = identifier
        self._socket: socket.socket | None = None

    def emit(self, record: cabc.Mapping[str, str]) -> bool:
        """Serialize and send one record, reconnecting after socket errors.

        Parameters
        ----------
        record : Mapping[str, str]
            Validated prefixed fields, without journal-owned names.

        Returns
        -------
        bool
            Whether the datagram was sent. A host without a journal socket
            reports ``False`` rather than looking like a delivered record, so
            a caller that summarizes emissions can tell the loss apart.

        Notes
        -----
        The native transport can raise ``OSError`` for serialization, size
        validation or sending failures. It performs no blocking retry.
        """
        return self._emit(
            record,
            message=f"build-limits metric {record.get('BUILD_LIMITS_EVENT', '')}",
        )

    def emit_trace(self, record: cabc.Mapping[str, str]) -> None:
        """Send one validated trace span through the same non-blocking socket.

        Parameters
        ----------
        record : Mapping[str, str]
            Bounded trace fields, without journal-owned names.
        """
        message = "build-limits trace " + " ".join(
            f"{field}={record.get(name, 'unknown')}"
            for name, field in (
                ("BUILD_LIMITS_TRACE_OPERATION", "operation"),
                ("BUILD_LIMITS_TRACE_OUTCOME", "outcome"),
                ("BUILD_LIMITS_TRACE_FAILURE_CATEGORY", "failure_category"),
                ("BUILD_LIMITS_TRACE_STOP_MODE", "stop_mode"),
                ("BUILD_LIMITS_TRACE_RETRY_ATTEMPT", "retry_attempt"),
            )
        )
        self._emit(record, message=message)

    def close(self) -> None:
        """Release the lazy socket, allowing the next record to reopen it."""
        conn, self._socket = self._socket, None
        if conn is not None:
            conn.close()

    def _emit(self, record: cabc.Mapping[str, str], *, message: str) -> bool:
        """Send one bounded record and reconnect after socket errors.

        Returns
        -------
        bool
            Whether the datagram was sent; ``False`` only when this host has no
            journal socket at the configured path.

        Raises
        ------
        OSError
            If serialization, connection or sending fails.
        """
        try:
            payload = self._serialize_message(record, message)
            if self._socket is None:
                if not self.path.exists():
                    return False
                self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                self._socket.settimeout(0.0)
                self._socket.connect(str(self.path))
            self._socket.send(payload)
        except OSError:
            self.close()
            raise
        return True

    def _serialize_message(self, record: cabc.Mapping[str, str], message: str) -> bytes:
        """Encode validated fields with the supplied bounded journal message.

        Returns
        -------
        bytes
            A bounded native journal datagram.

        Raises
        ------
        OSError
            If a field name or value fails validation.
        """
        fields = {
            "MESSAGE": message,
            "PRIORITY": "6",
        }
        if self.identifier is not None:
            fields["SYSLOG_IDENTIFIER"] = self.identifier
        if any(not _valid_key(key) for key in record):
            msg = "invalid native journal field name"
            raise OSError(msg)
        fields.update(record)
        return _encode_fields(fields)


def _encode_fields(fields: cabc.Mapping[str, str]) -> bytes:
    """Validate final text fields and encode one bounded native datagram.

    Returns
    -------
    bytes
        One bounded ASCII datagram in field insertion order.

    Raises
    ------
    OSError
        If final values or the datagram size are invalid.
    """
    if any(not _valid_value(value) for value in fields.values()):
        msg = "invalid native journal field value"
        raise OSError(msg)
    payload = "".join(f"{key}={value}\n" for key, value in fields.items()).encode(
        "ascii"
    )
    if len(payload) > MAX_DATAGRAM_BYTES:
        msg = "native journal record exceeds datagram limit"
        raise OSError(msg)
    return payload


def _valid_key(key: str) -> bool:
    """Allow only authored uppercase ASCII buildlimits field names.

    Returns
    -------
    bool
        Whether the name can be sent without authoring journal-owned fields.
    """
    return (
        key.startswith("BUILD_LIMITS_")
        and key.isascii()
        and all(
            character in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789" for character in key
        )
    )


def _valid_value(value: str) -> bool:
    """Refuse newline, binary and non-ASCII values in the text protocol.

    Returns
    -------
    bool
        Whether the value fits the text-only protocol.
    """
    return value.isascii() and not any(character in value for character in "\n\r\0")
