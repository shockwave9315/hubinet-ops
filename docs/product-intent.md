# Hubinet Ops 0.5 — current product intent

Status: **ACTIVE PRODUCT INTENT** (operator-stated product target). Recorded 2026-08-28;
threat model reset 2026-08-28.

This document records **what the operator wants the product to do**. It is the
authority for *product direction and priority*.

It is **not** an ADR. Where it and an ACCEPTED ADR (0001, 0002) disagree about
an architectural invariant, **the ADR wins** and the conflict must be reported,
not quietly resolved.

It **does** authorize the roadmap below as ordinary implementation work. An item
that is not yet built is unbuilt, not forbidden: no new ADR is required merely
because a feature touches a managed guest. See `AGENTS.md`, "When to write an
ADR".

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
9. **A failed, partial, or unavailable package scan is never "zero updates."**
   Absence of evidence is reported as unknown, never as "nothing to do".

## 3. What the operator wants to see

Discovery and presentation are the visible half of the product.

- Resources discovered in PVE appear dynamically in Hubinet and in Home
  Assistant, with no repository or config change.
- A successful, complete discovery/reconciliation scan that no longer contains a
  CT/VM may establish current absence, per the accepted discovery/reconciliation
  architecture (ADR 0002).
- **A failed, partial, or unavailable PVE scan is never resource deletion.**
- Resources confirmed absent are **no longer shown as current/active resources**
  in the dynamic Home Assistant view, according to the designed
  lifecycle/reconciliation model.

  This is a statement about the *current view*, not about registry cleanup. It
  does not by itself prescribe destructive Device/Entity Registry deletion: the
  accepted architecture may legitimately retain historical backend resource
  identity and leave a Home Assistant registry entity present-but-unavailable.
  Registry and history lifecycle remain governed by the accepted inventory/HA
  architecture (ADR 0001, ADR 0002, `0.5-inventory-model.md`), which this
  document does not redesign.

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

## 4. Threat model and the VMID-reuse case

Hubinet Ops targets a **trusted, self-administered** Proxmox environment. The
binding statement is `AGENTS.md`, "Threat model": the Proxmox administrator/root,
the Proxmox host, root inside a managed LXC, the Hubinet operator, and normal
`apt`/`dpkg` behavior are TRUSTED. Defending against a hostile administrator of
the environment being managed is **out of scope**, and no feature is gated on a
proof that survives full administrative compromise.

The VMID-reuse case is still handled, but as an **ordinary correctness concern**,
not a security proof:

```text
scan sees occupant A at VMID 112
A is destroyed between scans
occupant B is recreated as VMID 112
next scan sees 112 again
```

The accepted identity model (ADR 0001, ADR 0002) already covers this the right
way: a VMID is a reusable slot locator, durable identity is the opaque backend
`resource_id`, and a resource incarnation returning after an observation gap is
marked `quarantined`/`uncertain` rather than being silently treated as
continuous. Long-lived policy and destructive authority are never transferred
across that gap by default. That is enough for a trusted environment.

**Blocker B — a persistent cryptographic workload-incarnation proof — is no
longer a prerequisite for anything on this roadmap.** The architecture that
existed to close it is superseded and archived under
`docs/archive/superseded-security-model/`.

### What is implemented, and what is next

```text
implemented, read-only:   PVE autodiscovery, dynamic backend inventory,
                          dynamic Home Assistant representation
next:                     read-only package/update scanning and presentation
then:                     operator-reviewed update plan -> approval
                          -> fresh job-owned snapshot -> update
                          -> healthcheck -> same-job rollback
```

Each step is ordinary implementation work under the accepted inventory
architecture and the hard rules in §2. It does not need a new ADR merely because
it touches a guest. What each step **does** need is real design, real tests, and
the §2 rules honored exactly — in particular **NO AUTO-UPDATE**.

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

- Designing a replacement workload-incarnation proof, reviving Family B / B-S1,
  or researching pmxcfs/task-history completeness. All of it belonged to a
  threat model this product no longer has.
- Building defenses against a hostile Proxmox root, a hostile root inside a
  managed guest, or an administrator replacing Hubinet-owned state.
- New attestation concepts or a new security-evidence taxonomy.
- Broadening the PVE credential beyond `{Sys.Audit, VM.Audit}` without a
  concrete, stated need for the exact additional privilege.

## 7. Where this fits

- `AGENTS.md` — binding agent rules; links here for product direction.
- `docs/architecture/README.md` — documentation index, authority hierarchy,
  default reading set.
- `docs/architecture/0.5-implementation-status.md` — what is actually
  implemented today.
