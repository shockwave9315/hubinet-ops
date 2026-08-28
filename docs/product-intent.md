# Hubinet Ops 0.5 — current product intent

Status: **ACTIVE PRODUCT INTENT** (operator-stated product target). Recorded 2026-08-28.

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
useful operations, but with the **guest inventory and its Home Assistant
representation no longer statically enumerated**:

```text
STATIC 0.4.x
  -> PVE AUTODISCOVERY
  -> DYNAMIC BACKEND INVENTORY
  -> DYNAMIC HOME ASSISTANT RESOURCES / UI
  -> SAFE OPERATOR-DRIVEN UPDATE WORKFLOW
```

0.4.x already had statically enumerated VM/LXC targets, update scanning,
pre-update snapshots, update execution, health checking, rollback, and a Home
Assistant representation. **0.5 is not reinventing those operations.** It is
making the *inventory* dynamic, identity-safe, and operator-governed.

### What "dynamic" does and does not mean

**Dynamic** means, specifically:

- VM/LXC target inventory is no longer statically enumerated anywhere;
- the Home Assistant resource representation follows dynamic discovery;
- adding or removing a supported PVE guest never requires editing a static VMID
  list, or creating/removing a static Home Assistant card.

It does **not** mean every Hubinet configuration value becomes dynamic. Source
configuration, credentials and credential references, operational settings,
scheduling, and retention settings remain ordinary, legitimately configured
values. This document does not redesign configuration.

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
8. **A recovery snapshot reference is scoped to the exact job and the exact
   current guest it was created for.** This is a Hubinet fail-closed rule, stated
   independently of what any particular PVE storage backend physically does:
   - if that guest is destroyed or confirmed absent, the Hubinet recovery
     snapshot reference becomes unusable/stale and must fail closed;
   - it must never be rebound to a later guest that happens to reuse the same
     VMID;
   - Hubinet never treats an old storage artifact as a recovery point for a new
     occupant merely because the locator matches.

   Hubinet's safety here must not depend on assumptions about ZFS, LVM, RBD, or
   any other backend's deletion semantics. The rule holds regardless of backend
   details.
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
- Resources confirmed absent are **no longer shown as current/active resources**
  in the dynamic Home Assistant view, according to the designed
  lifecycle/reconciliation model.

  This is a statement about the *current view*, not about registry cleanup. It
  does not by itself prescribe destructive Device/Entity Registry deletion: the
  accepted architecture may legitimately retain historical backend resource
  identity and leave a Home Assistant registry entity present-but-unavailable.
  Registry and history lifecycle remain governed by the accepted inventory/HA
  architecture (ADR 0001, ADR 0002, ADR 0004, `0.5-inventory-model.md`), which
  this document does not redesign.

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

**Blocker B remains OPEN as its own separate question.**

### CURRENT AUTHORITY

Under the currently ACCEPTED architecture (ADR 0005 §26, ADR 0006):

```text
Phase 1C:                     BLOCKED
mutation authority:           NONE
Blocker B:                    OPEN
security_continuity=trusted:  GRANTED NOWHERE
```

Nothing in this document changes any of those. Read-only discovery, current
Home Assistant visibility, read-only package scanning, and package-detail
presentation are not gated on Blocker B in the first place — they are read-only
and belong to the existing accepted discovery model. **Update execution,
snapshots, jobs, health checking as a job step, and rollback are all Phase 1C
and remain BLOCKED. None of them is authorized to begin.**

### OPERATOR TARGET

The operator's stated direction is deliberately narrower than "unblock
mutation": Blocker B **should not remain a blanket prerequisite** for a future,
operator-reviewed, job-scoped path of exactly this shape:

```text
plan -> approval -> fresh job-owned snapshot -> update -> healthcheck
     -> same-job rollback
```

Whether that narrow path can be authorized **without** first closing persistent
Blocker B is a **NEW ARCHITECTURAL DECISION**. It requires its own ADR,
separately reviewed and accepted.

This document records the operator's intent to pursue that question. It does not
decide it, does not unblock anything, and must not be read by an implementation
agent as present authorization. Until that ADR exists and is ACCEPTED, the
CURRENT AUTHORITY block above is the only operative one.

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
