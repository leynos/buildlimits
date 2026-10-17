"""Compatibility import for the canonical requester lifecycle record.

Pending remains owned by Requests; new callers use pending_request directly.
"""

from buildlimits.pending_request import Pending as Pending
