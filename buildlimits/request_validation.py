"""Query compile-request validity without taking socket or descriptor ownership.

Only Requests uses this boundary to plan launch attributes or select a bounded
refusal. Request recording, cleanup and compiler-token accounting remain with
their existing owners.
"""

import dataclasses as dc
import typing as typ

from buildlimits import attributes
from buildlimits.attributes import AttributeRefusalError

if typ.TYPE_CHECKING:
    import collections.abc as cabc

    from buildlimits.attributes import AttributePlan
    from buildlimits.protocol import CompileRequest


@dc.dataclass(frozen=True, slots=True)
class CompileContext:
    """Explicit process sample used to plan reachable compiler attributes.

    Attributes
    ----------
    current_nice : int
        Supervisor niceness sampled by the requester adapter.
    current_limits : Mapping[str, tuple[int, int]]
        Sampled portable limits, including RLIMIT_NOFILE for target placement.
    """

    current_nice: int
    current_limits: cabc.Mapping[str, tuple[int, int]]


@dc.dataclass(frozen=True, slots=True)
class CompileRefusal:
    """Admission-policy decision awaiting conversion to a requester reply.

    Attributes
    ----------
    reason : str
        Stable admission outcome understood by the requester adapter.
    message : str
        Bounded explanation without request arguments or environment values.
    """

    reason: str
    message: str


@dc.dataclass(frozen=True, slots=True)
class DescriptorHeadroom:
    """Descriptor retention ceilings used by one admission decision.

    Attributes
    ----------
    available : int
        Budget remaining for the request socket and received rights.
    budget : int
        Total policy budget, independent of descriptors temporarily retained.
    """

    available: int
    budget: int


def total_budget_refusal(rights_count: int, budget: int) -> CompileRefusal | None:
    """Refuse requests that provably exceed a known total policy ceiling.

    Parameters
    ----------
    rights_count : int
        Complete received count or a validated complete request declaration.
    budget : int
        Total request-owned descriptor budget, or zero when headroom is unknown.

    Returns
    -------
    CompileRefusal | None
        A permanent refusal when rights plus their socket exceed a known budget.
        Zero budgets remain recoverable through an explicit idle recheck.
    """
    needed = 1 + rights_count
    if needed > budget > 0:
        return CompileRefusal(
            "descriptors",
            f"request needs {needed} descriptors; total budget is {budget}",
        )
    return None


def prepare_compile(
    request: CompileRequest,
    received_count: int,
    headroom: DescriptorHeadroom,
    *,
    context: CompileContext,
) -> AttributePlan | CompileRefusal:
    """Validate received rights and plan reachable caller attributes.

    Parameters
    ----------
    request : CompileRequest
        Decoded request whose resources remain owned by its caller.
    received_count : int
        Number of successfully received descriptors.
    headroom : DescriptorHeadroom
        Available and total descriptor retention ceilings for this request.
    context : CompileContext
        Explicit supervisor process sample; this query never samples the OS.

    Returns
    -------
    AttributePlan | CompileRefusal
        Validated attribute plan or bounded admission refusal. This query
        neither closes descriptors nor changes admission state.
    """
    if received_count != len(request.fd_targets):
        return CompileRefusal("bad-request", "descriptor count mismatch")
    hard = context.current_limits["RLIMIT_NOFILE"][1]
    if hard != -1 and any(fd >= hard for fd in request.fd_targets):
        message = f"descriptor target reaches supervisor hard limit {hard}"
        return CompileRefusal("descriptors", message)
    refusal = total_budget_refusal(received_count, headroom.budget)
    if refusal is not None:
        return refusal
    if 1 + received_count > headroom.available:
        return CompileRefusal(
            "busy", "queued descriptor budget exhausted; retry admission"
        )
    try:
        return attributes.plan_attributes(
            request.attributes,
            context.current_nice,
            context.current_limits,
        )
    except AttributeRefusalError as error:
        return CompileRefusal("attributes", str(error))
