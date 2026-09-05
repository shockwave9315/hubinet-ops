"""The closed refusal taxonomy for explicit package-update job issuance.

Production activation gives an operator one authenticated control that starts
the currently approved update for one resource.  That control must be able to
tell an operator *which* precondition failed -- "nobody has approved a plan"
and "another update owns the global slot" are different problems with
different remedies -- without the HTTP layer re-deciding anything the
authority already decided.

So the decision and its machine-readable name are committed in the same
place: :meth:`InventoryAuthority.issue_package_update_job` raises
:class:`PackageUpdateIssuanceRefused` carrying one member of the closed set
below, and the HTTP boundary only renders it.  Adding a refusal here means
adding it at the authority that actually refuses, never at a caller that
guessed.

This module is deliberately data and one exception type: no SQL, no host I/O,
and no second copy of any eligibility rule.
"""

from __future__ import annotations

from .models import AuthorityConflict


#: Every reason explicit issuance may refuse an operator's start request.
#: Closed on purpose -- an unclassified refusal is a bug, not a fallback.
PACKAGE_UPDATE_ISSUANCE_REFUSALS: frozenset[str] = frozenset(
    {
        #: The resource exists but is not the current, present, active,
        #: bound incarnation this backend may act on.
        "resource_not_current",
        #: The resource is current but its type is not one this product
        #: updates packages for.
        "resource_unsupported",
        #: No exact plan approval exists for this resource at all.
        "no_current_approval",
        #: An approval exists, but it is not the one this backend currently
        #: holds for the resource.
        "approval_not_current",
        #: An approval exists and is current, but it no longer describes the
        #: current exact plan or its source context -- `PRODUCT.md` rule 2.
        "plan_not_approved",
        #: The current exact plan has no package rows.  A job with nothing to
        #: install is not an update.
        "plan_empty",
        #: The resource has declared no health contract.  Absence is not
        #: health, so no job may be issued -- `PRODUCT.md`, "What healthy
        #: means".
        "health_contract_unconfigured",
        #: A contract exists but at least one probe is not structurally
        #: representable by the exact executor, so its success criterion
        #: could never be evaluated.
        "health_contract_not_executable",
        #: Another package-update job owns the one global destructive slot.
        "another_job_active",
        #: The supplied ``request_id`` was already used for a different
        #: resource or approval.
        "request_id_conflict",
        #: Current plan authority is genuinely undecidable right now: a newer
        #: package scan is still running, or a required post-update refresh is
        #: unclaimed/linked and not yet terminal. This is temporary rather
        #: than proven stale; the operator may ask again once the scan ends.
        "source_authority_unavailable",
        #: A Hubinet PRODUCT update holds the exclusive maintenance fence.
        #: Starting a workload update now would let a product update replace
        #: the backend and its privileged helpers underneath an in-flight
        #: host operation.  Retryable once the product update completes or is
        #: rolled back -- see `app/inventory/product_update_fence.py`.
        "product_update_in_progress",
    }
)


class PackageUpdateIssuanceRefused(AuthorityConflict):
    """Issuance refused, with the exact reason the authority decided.

    A subclass of :class:`AuthorityConflict` on purpose: every existing
    ``except AuthorityConflict`` around issuance keeps working unchanged, and
    only callers that want the machine-readable name have to know this type
    exists.
    """

    __slots__ = ("reason",)

    def __init__(self, reason: str, message: str) -> None:
        if reason not in PACKAGE_UPDATE_ISSUANCE_REFUSALS:
            raise ValueError(
                "package update issuance refusal reason is outside the closed set"
            )
        super().__init__(message)
        self.reason = reason
