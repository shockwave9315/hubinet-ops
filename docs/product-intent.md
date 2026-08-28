# Hubinet Ops 0.5 — current product intent

Status: **OPERATOR-STATED PRODUCT TARGET.** Recorded 2026-08-28.

This document records **what the operator wants the product to do**. It is the
authority for *product direction and priority*.

It is **not** an ADR and **not** an architecture authority:

- It does not amend, weaken, or override any ACCEPTED ADR. Where it and an
  ACCEPTED ADR disagree about an architectural or security invariant, **the ADR
  wins** and the conflict must be reported, not quietly resolved.
- It authorizes **no** implementation. Every item below that is not already
  implemented still needs its own accepted architecture before code exists.
- It grants `security_continuity=trusted` nowhere and does not close Blocker B.

## 1. The product in one line

Hubinet Ops 0.5 is an evolution of the practical 0.4.x workflow — the same
useful operations, but with nothing statically configured:

```text
STATIC 0.4.x
  -> PVE AUTODISCOVERY
  -> DYNAMIC BACKEND INVENTORY
  -> DYNAMIC HOME ASSISTANT RESOURCES / UI
  -> SAFE OPERATOR-DRIVEN UPDATE WORKFLOW
```

0.4.x already had statically configured VM/LXC targets, update scanning,
pre-update snapshots, update execution, health checking, rollback, and a Home
Assistant representation. **0.5 is not reinventing those operations.** It is
making them dynamic, identity-safe, and operator-governed.

## 2. Hard product rules

These are requirements, not suggestions.

1. **NO AUTO-UPDATE.** The application must never install package updates on its
   own. "99 updates available" is never permission to run them.
2. **Discovery and scanning may be automatic, but must be READ-ONLY.** Package
   and update scanning may run on a schedule without asking; it may only read.
3. **Installing updates always requires explicit operator review and approval of
   the current update plan.**
4. **A material change to the update plan after approval invalidates that
   approval.** Fail closed and ask for approval of the new plan; never execute a
   different plan than the one approved.
5. **Every update run creates a fresh, job-owned pre-update snapshot**, uniquely
   named for that job (e.g. `hubinet-preupdate-<job_uuid>`).
6. **A job may only roll back to the recovery snapshot created by that same
   job.** Never to an arbitrary older snapshot, and never days later.
7. **Snapshot retention/cleanup touches only Hubinet-managed snapshots.**
   Manually created or operator-owned snapshots must never be deleted merely
   because Hubinet can see them.
8. **Snapshots belong to the actual current PVE guest.** If the CT/VM is
   destroyed, its PVE snapshot goes with it. Hubinet must never keep a detached
   historical snapshot and later apply it to a newly created guest that happens
   to reuse the same VMID.
9. **Persistent workload-incarnation proof is not solved and must not be assumed
   solved.** See §4.

## 3. What the operator wants to see

Discovery and presentation are the visible half of the product.

- Resources discovered in PVE appear dynamically in Hubinet and in Home
  Assistant, with no repository or config change.
- A successful, complete discovery/reconciliation scan that no longer contains a
  CT/VM may establish current absence, per the accepted discovery/reconciliation
  architecture (ADR 0002, ADR 0004).
- **A failed, partial, or unavailable PVE scan is never resource deletion.**
- Resources confirmed absent disappear from the current Home Assistant view
  according to the designed lifecycle/reconciliation model.

For packages/updates, the operator wants to see, where the information can be
obtained reliably:

- package name;
- installed version;
- candidate version;
- repository/origin;
- package description;
- other useful package/update metadata;
- security-update classification **only where it can be established reliably**;
- reboot-required or equivalent information **where reliable**.

Where a field cannot be established reliably, it must be reported as unknown,
never guessed.

## 4. The unresolved incarnation question (Blocker B)

The theoretical case that remains unsolved:

```text
scan sees occupant A at VMID 112
A is destroyed between scans
occupant B is recreated as VMID 112
next scan sees 112 again
Hubinet never observed ABSENT
```

Without a stronger primitive, Hubinet cannot prove A and B are the same
incarnation. **This must never silently transfer long-lived destructive
authority or policy from A to B.**

Operator position: this unresolved question must no longer be *assumed* to block
ordinary discovery, current Home Assistant visibility, read-only package
scanning, package-detail presentation, explicitly operator-approved current
update work, creation of a fresh job-owned snapshot, health checking, and
same-job rollback to that snapshot.

**Blocker B remains OPEN as its own separate question.**

Architectural consequence, stated honestly: under currently ACCEPTED
architecture (ADR 0005 §26, ADR 0006), Phase 1C — policy, jobs, and mutation
authority — is **BLOCKED**. Whether a narrower, same-job, approval-scoped
update/snapshot/rollback path can be authorized *without* first closing
Blocker B is a **NEW ARCHITECTURAL DECISION**. It requires its own ADR and its
own review. This document records the operator's intent to pursue that
question; it does not decide it, and it does not unblock anything.

## 5. Intended update workflow (target shape, not yet designed)

```text
current live state
  -> current update plan shown to operator
  -> operator approval
  -> final preflight / plan revalidation
  -> Hubinet-owned job-scoped pre-update snapshot
  -> execute approved update
  -> health check
  -> success OR controlled rollback decision
```

On health-check failure, the operator may approve rollback, or a separately
configured **same-job** compensation policy may roll back to that job's own
fresh recovery point. This is deliberately different from automatically
selecting an arbitrary old snapshot.

None of this is implemented. None of it is authorized to begin.

## 6. What is explicitly not the current path

- **Family B / B-S1** (proving continuity through complete PVE task/history
  observation) is **not** the current implementation path. It was explored, it
  produced two unmerged research branches (PR #52, PR #53), Experiment #13 was
  never executed, and the recovery review returned NO-GO. See
  `docs/archive/postmortems/blocker-b-family-b-13.md`.
- Reviving that research, executing Experiment #13, or continuing PR #53's
  architecture is not current work.

## 7. Where this fits

- `AGENTS.md` — binding agent rules; links here for product direction.
- `docs/architecture/README.md` — documentation index, authority hierarchy,
  default reading set.
- `docs/architecture/0.5-implementation-status.md` — what is actually
  implemented today.
