"""The version 5 correlation group and the shape it must take in a stored row.

A row either names one invocation or none of it. The group is the bounded
attempt identifier together with the supervisor generation whose clock stamped
the row: the attempt alone recurs across supervisor processes, so the pair is
what makes an identity. The launch's own completion moment travels beside it
and stays independently optional, because a group still running when its row
was written has no completion time. Keeping the constants and the two
validators together means the encoder, the decoder and the recorder agree on
one definition, and half a pair is refused rather than written.

The label-availability boundary is not part of this group. It is the row's
``observed_at``, and ``label_time_provenance`` states whether that time is
proof, so nothing here duplicates it.
"""

import typing as typ

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.outcome_record_format import OutcomeRecord

TIMING_FIELDS = frozenset({"completed_at", "attempt", "supervisor_generation"})
"""The three fields a version 5 row adds to the version 4 shape."""

MAX_ATTEMPT_CHARS = 32
MAX_GENERATION_CHARS = 64
_TOKEN_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-"
)


def bounded_token(value: object, maximum: int) -> bool:
    """Check one correlation token for its bound and safe character set.

    Parameters
    ----------
    value : object
        The candidate token.
    maximum : int
        The longest accepted token.

    Returns
    -------
    bool
        Whether the value is a non-empty path-free token within ``maximum``.

    Examples
    --------
    >>> bounded_token("g1", 32), bounded_token("a/b", 32)
    (True, False)
    """
    if not isinstance(value, str) or not 0 < len(value) <= maximum:
        return False
    return all(character in _TOKEN_CHARS for character in value)


def valid_evidence(record: OutcomeRecord) -> bool:
    """Check a record's correlation group.

    ``attempt`` and ``supervisor_generation`` are one identity, so a row carries
    both or neither: half a pair would let a row claim a correlation it cannot
    honour, and the replay would have no way to tell two invocations apart.
    ``completed_at`` is independently optional, because a group that had not
    ended when its row was written has no completion time, and inventing one
    would misuse the moment the label actually became available.

    Returns
    -------
    bool
        Whether the correlation pair is absent, or complete and well formed.
    """
    if record.attempt is None and record.supervisor_generation is None:
        return True
    return all((
        bounded_token(record.attempt, MAX_ATTEMPT_CHARS),
        bounded_token(record.supervisor_generation, MAX_GENERATION_CHARS),
    ))


def valid_fields(value: cabc.Mapping[str, object]) -> bool:
    """Check a decoded row's correlation group.

    The group is absent from a version 1 to 4 row, and a version 5 row may also
    carry it as an explicit null when compaction preserved an older row without
    inventing a correlation. A present pair must be complete and well formed;
    half a pair is not a row the writer can produce, so it is refused as
    corrupt rather than read as a correlation.

    Returns
    -------
    bool
        Whether the correlation pair is absent, or complete and well formed.
    """
    if not any(name in value for name in TIMING_FIELDS):
        return True
    if value.get("attempt") is None and value.get("supervisor_generation") is None:
        return True
    return all((
        bounded_token(value.get("attempt"), MAX_ATTEMPT_CHARS),
        bounded_token(value.get("supervisor_generation"), MAX_GENERATION_CHARS),
    ))


__all__ = [
    "MAX_ATTEMPT_CHARS",
    "MAX_GENERATION_CHARS",
    "TIMING_FIELDS",
    "bounded_token",
    "valid_evidence",
    "valid_fields",
]
