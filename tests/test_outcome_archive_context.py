"""Contracts for the values one archive run shares across its operations."""

from unittest import mock

import pytest

from buildlimits import outcome_archive_quarantine


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(b"\x00" * 6, "000000000000"), (b"\xff" * 6, "ffffffffffff")],
    ids=["lowest-byte", "highest-byte"],
)
def test_a_quarantine_token_is_the_hex_of_six_random_bytes(
    raw: bytes, expected: str
) -> None:
    """A quarantined file's tag is the encoded bytes, at every byte value.

    The token keeps two faults on one segment from overwriting each other's
    evidence, so its width and character set matter: it is interpolated into a
    file name and must never carry a separator or an uppercase letter. Pinning
    the byte source to both extremes of the range makes the encoding exact
    rather than a sample of whatever the host happened to draw.
    """
    with mock.patch.object(outcome_archive_quarantine.os, "urandom", return_value=raw):
        token = outcome_archive_quarantine.fresh_quarantine_token()

    assert token == expected, "the six bytes are hex-encoded in order"
    assert len(token) == 12, "a token is twelve characters, however it is drawn"
    assert token == token.lower(), "a token is lowercase"
