"""The independent completion proof for one real package mutation.

`apt-get` exiting zero is evidence about a command, never proof that the
exact approved package mutation is durably complete. This module holds the
proof that is: a pure, side-effect-free comparison of the guest's own dpkg
status database, read independently on both sides of the mutation, against
the job's IMMUTABLE frozen approved rows.

It is deliberately pure and lives under `app/inventory/` so
`InventoryAuthority` itself computes the verdict inside the transaction that
would commit `mutation_completed`. A caller supplies *parsed evidence*; it
never supplies a verdict, so there is no way to advance a job to
`mutation_completed` by asserting success.

What the proof requires, all of it, or the mutation is not complete:

1. **Every approved row landed exactly.** For each frozen
   ``(package_name, architecture)``, dpkg now reports that exact binary
   package installed at exactly the approved *candidate* version -- not
   newer, not older, not missing.
2. **Nothing else moved at all.** The complete set of installed
   ``(package_name, architecture) -> version`` differences between the
   pre-mutation and post-mutation readings is exactly the approved set. An
   extra package upgraded, downgraded, added, or gone is a materially
   different mutation from the approved plan, and fails closed.
3. **Every approved row started where the plan said.** For each frozen row,
   the pre-mutation reading showed exactly the approved *installed*
   version, so the proven transition really is the approved one.
4. **dpkg is not mid-transaction.** No package is left in `half-installed`,
   `unpacked`, `half-configured`, `triggers-awaited`, or `triggers-pending`.
   Any of those makes "the package state is now exactly X" ambiguous.

Two legal-but-refused cases are called out deliberately rather than
tolerated, because the product's one real package command can in principle
produce them, and this stage must account for every additional workload
package state its own command can legally change:

- **A disappeared package.** dpkg may drop a package whose every file was
  overwritten by another package during an upgrade (apt reports these as
  "The following packages disappeared from your system"). It is a real,
  legitimate outcome, and it is still a workload package state change the
  operator never approved, so it fails rule 2 and the job stays fenced with
  its rollback authority rather than being called complete.
- **A held-back or partially applied plan.** If the package command upgraded
  only some of the approved rows before failing, rule 1 fails for the rest.
  The job
  keeps its snapshot, its global destructive ownership, and its rollback
  authority; it is never retried blindly and never called succeeded.

Failing this proof is never "the mutation failed". It is "the mutation's
completion is not proven", which is why every failing path retains
ownership instead of terminalizing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .models import AuthorityInvariantError


#: Bound on how much post-state divergence detail is retained in a refusal
#: reason. The reason is diagnostics; the durable evidence is the host
#: journal and the append-only event log.
_MAX_REPORTED_DIFFERENCES = 5


@dataclass(frozen=True, slots=True)
class PackageMutationPostState:
    """Independently parsed dpkg evidence from both sides of one mutation.

    Both readings come from the guest's own fixed, argument-less
    ``dpkg-query -W`` inventory (see `app.package_scan`), never from APT's
    view of itself:

    - ``pre_installed`` is the reading the host boundary took immediately
      before it launched the package command, inside the same operation and
      under the same per-VMID lease, so it is the true baseline the mutation
      started from rather than a possibly-minutes-old scan;
    - ``post_installed``/``post_unfinished`` are the reading taken after the
      command reached its terminal result.
    """

    pre_installed: Mapping[tuple[str, str], str]
    post_installed: Mapping[tuple[str, str], str]
    post_unfinished: Sequence[tuple[str, str, str]]


@dataclass(frozen=True, slots=True)
class PackageMutationCompletionProof:
    """The verdict of :func:`prove_package_mutation_completion`.

    ``proved_material`` is the exact frozen material set the verdict was
    computed over. :meth:`InventoryAuthority.complete_package_update_mutation`
    re-reads the job's own immutable rows and requires them to equal this,
    so a proof computed over some other job's -- or some other version of
    this job's -- material can never complete a job.
    """

    proven: bool
    proved_material: frozenset[tuple[str, str, str, str]]
    reason: str | None = None


def _describe(items: Sequence[str]) -> str:
    shown = list(items[:_MAX_REPORTED_DIFFERENCES])
    if len(items) > _MAX_REPORTED_DIFFERENCES:
        shown.append(f"and {len(items) - _MAX_REPORTED_DIFFERENCES} more")
    return ", ".join(shown)


def prove_package_mutation_completion(
    *,
    frozen_material: frozenset[tuple[str, str, str, str]],
    post_state: PackageMutationPostState,
) -> PackageMutationCompletionProof:
    """Prove, or refuse to prove, that one exact approved mutation completed.

    Pure: no I/O, no clock, no randomness. ``frozen_material`` is the job's
    IMMUTABLE approved rows as
    ``(package_name, architecture, installed_version, candidate_version)``.
    """

    if not isinstance(post_state, PackageMutationPostState):
        raise AuthorityInvariantError(
            "package mutation completion requires parsed dpkg post-state"
        )
    if not frozen_material:
        raise AuthorityInvariantError(
            "package mutation completion requires non-empty approved material"
        )

    def refuse(reason: str) -> PackageMutationCompletionProof:
        return PackageMutationCompletionProof(
            proven=False, proved_material=frozen_material, reason=reason[:500]
        )

    if post_state.post_unfinished:
        unfinished = _describe(
            [
                f"{name}:{architecture} ({status})"
                for name, architecture, status in post_state.post_unfinished
            ]
        )
        return refuse(
            f"dpkg reports unfinished package state after the mutation: {unfinished}"
        )

    pre = dict(post_state.pre_installed)
    post = dict(post_state.post_installed)

    approved: dict[tuple[str, str], tuple[str, str]] = {}
    for package_name, architecture, installed_version, candidate_version in (
        frozen_material
    ):
        approved[(package_name, architecture)] = (
            installed_version,
            candidate_version,
        )

    # 3. Every approved row must have started exactly where the plan said.
    wrong_start = sorted(
        f"{name}:{architecture}"
        for (name, architecture), (installed_version, _) in approved.items()
        if pre.get((name, architecture)) != installed_version
    )
    if wrong_start:
        return refuse(
            "approved packages were not installed at their approved pre-update "
            f"version before the mutation: {_describe(wrong_start)}"
        )

    # 1. Every approved row must now be at exactly its approved candidate.
    wrong_end = sorted(
        f"{name}:{architecture}"
        for (name, architecture), (_, candidate_version) in approved.items()
        if post.get((name, architecture)) != candidate_version
    )
    if wrong_end:
        return refuse(
            "approved packages are not installed at their exact approved "
            f"candidate version after the mutation: {_describe(wrong_end)}"
        )

    # 2a. No installed binary package may appear or disappear.
    appeared = sorted(
        f"{name}:{architecture}" for name, architecture in post.keys() - pre.keys()
    )
    if appeared:
        return refuse(
            "packages were installed that this plan never approved: "
            f"{_describe(appeared)}"
        )
    disappeared = sorted(
        f"{name}:{architecture}" for name, architecture in pre.keys() - post.keys()
    )
    if disappeared:
        return refuse(
            "packages present before the mutation are no longer installed: "
            f"{_describe(disappeared)}"
        )

    # 2b. The complete set of version changes must be exactly the approved set.
    changed = {
        identity for identity, version in post.items() if pre[identity] != version
    }
    unapproved = sorted(
        f"{name}:{architecture}" for name, architecture in changed - approved.keys()
    )
    if unapproved:
        return refuse(
            "packages outside the approved plan changed version during the "
            f"mutation: {_describe(unapproved)}"
        )

    # Every approved row was proven to move from its approved installed
    # version to its approved candidate version above, so `changed` cannot be
    # missing one unless the two versions were identical -- which
    # `_validate_package_plan` already refuses at issuance. Assert it rather
    # than assume it.
    missing = sorted(
        f"{name}:{architecture}" for name, architecture in approved.keys() - changed
    )
    if missing:
        return refuse(
            "approved packages did not change version during the mutation: "
            f"{_describe(missing)}"
        )

    return PackageMutationCompletionProof(
        proven=True, proved_material=frozen_material, reason=None
    )
