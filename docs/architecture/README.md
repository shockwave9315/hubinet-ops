# Hubinet Ops documentation index

**Start here.** This page tells you what the product is trying to be, which
documents are authoritative, which are historical, and — most importantly —
**what you should not read**.

Read the minimum current material required for your task. Do not recursively
read all architecture and research documents.

---

## 1. Current product direction

Hubinet Ops 0.5 is an evolution of the practical 0.4.x workflow, with the **guest
inventory and its Home Assistant representation no longer statically enumerated**:

```text
STATIC 0.4.x
  -> PVE AUTODISCOVERY
  -> DYNAMIC BACKEND INVENTORY
  -> DYNAMIC HOME ASSISTANT RESOURCES / UI
  -> SAFE OPERATOR-DRIVEN UPDATE WORKFLOW
```

Adding or removing a supported PVE guest must never require editing a static VMID
list or hand-maintaining a Home Assistant card. Source configuration,
credentials, operational settings, and retention settings remain ordinary
configuration.

Hard product rules — full statement in `docs/product-intent.md`:

- Discovery and package/update **scanning may be automatic, but must be
  READ-ONLY**.
- **NO AUTO-UPDATE.** Installing package updates always requires explicit
  operator review and approval of the current plan.
- A **material change to the plan after approval invalidates that approval** —
  fail closed and re-ask.
- Every update run creates a **fresh, job-owned pre-update snapshot**, and a job
  may only roll back to **its own** snapshot.
- Retention/cleanup touches **only Hubinet-managed snapshots**; operator
  snapshots are never touched.
- **Persistent workload-incarnation proof (Blocker B) is not solved** and must
  not be assumed solved. It must never silently transfer long-lived destructive
  authority from a destroyed occupant to its replacement.
- **Family B / B-S1 is not the current implementation path.**

Current implemented state: PVE autodiscovery, dynamic backend inventory, and
dynamic Home Assistant representation are **live and read-only**. Package
scanning, update plans, jobs, snapshots, and rollback are **not implemented**.

### Current authority vs. operator target — do not confuse these

> **CURRENT AUTHORITY.** Under the ACCEPTED ADRs today:
> **Phase 1C is BLOCKED**, **mutation authority is NONE**, **Blocker B is OPEN**,
> and **`security_continuity=trusted` is GRANTED NOWHERE**. Nothing in this index
> or in `docs/product-intent.md` authorizes package scanning, mutation, jobs,
> snapshots, update execution, or rollback. Read-only package/update scanning is
> not gated on Blocker B, but it still needs its own accepted read-only
> evidence/transport contract before implementation may begin — it is not
> already covered by the existing accepted PVE inventory discovery contract.
> No implementation of any of these items may begin.
>
> **OPERATOR TARGET.** The operator's stated direction is narrower than "unblock
> mutation": Blocker B *should not remain a blanket prerequisite* for a future,
> operator-reviewed, job-scoped
> `plan -> approval -> fresh snapshot -> update -> healthcheck -> same-job rollback`.
> Deciding whether that narrow path can be authorized **without** closing
> persistent Blocker B requires a **new, explicit architecture decision** — its
> own ADR, separately reviewed and accepted.
>
> Until that ADR exists and is ACCEPTED, the CURRENT AUTHORITY line above is the
> only operative one. An implementation agent must read the operator target as
> *a question for a future ADR*, never as present authorization.

---

## 2. Authority hierarchy

```text
explicit operator decisions       (see the note directly below — this is not a
                                   licence to bypass an ACCEPTED ADR)
> ACCEPTED ADRs / accepted architecture
> AGENTS.md
> docs/product-intent.md          (ACTIVE PRODUCT INTENT: binding for product
>                                  direction/priority, subordinate to ACCEPTED
>                                  ADRs on any architecture/security invariant)
> docs/architecture/0.5-implementation-status.md   (map, not authority)
> code / tests
> non-normative research
> archived material                (docs/archive/ — authority for nothing)
```

Rules that follow from this:

- **What an operator decision does, and does not, do.** An operator decision sets
  product direction, and authorizes a task or action **where the accepted
  architecture already permits it**. It does **not** silently override an
  ACCEPTED ADR invariant. When an operator wants behavior that conflicts with an
  ACCEPTED ADR, the outcome is an explicit architecture-change / supersession
  process (§7, and `.agents/skills/hubinet-architecture-change`) — not immediate
  permission to ignore the ADR. An operator may of course explicitly *request*
  that architecture change; that request starts the process, it does not skip it.
- An ACCEPTED ADR beats the status document, the skills, the code, and any
  research. If implementation conflicts with an accepted ADR, **stop and report
  the conflict**; do not adapt the architecture to fit.
- `docs/product-intent.md` is **ACTIVE PRODUCT INTENT**: binding for *what to
  build next* and for the product rules the operator has fixed (for example NO
  AUTO-UPDATE), never for architectural or security invariants. Where it and an
  ADR disagree on an invariant, the ADR wins. It authorizes no implementation on
  its own — an unimplemented item there still needs its own accepted
  architecture first.
- **"ACTIVE AUTHORITY" does not mean "read this for every task."** ADR 0005 and
  ADR 0006 are active authority and are also specialized — see §5.

---

## 3. Documentation categories

| Category | Where | Meaning |
| --- | --- | --- |
| **A — Active authority** | `docs/architecture/adr/0001`–`0006`, `0.5-foundation.md`, `0.5-inventory-model.md`, `AGENTS.md` | Normative and binding for architecture and security invariants. |
| **A2 — Active product intent** | `docs/product-intent.md` | Binding for **product direction and priority** — what to build next and what the product must never do. **Not** an ADR and **not** architecture authority: on any architectural or security invariant, an ACCEPTED ADR wins. |
| **B — Active implementation contract** | `docs/architecture/0.5-r0-read-only-runtime-activation.md` | Cited by name and section from live runtime code and tests. |
| **C — Active current status** | `docs/architecture/0.5-implementation-status.md` | The current map. Not authority. |
| **D — Active operator/user doc** | `README.md`, `docs/operations/*`, `deploy/README-*.md`, `CHANGELOG.md` | How to install, deploy, and operate. |
| **E — Specialized reference** | `docs/architecture/research/adr0006-workload-continuity-evidence.md`, `.agents/skills/*`, `custom_components/hubinet_ops/NOTICE.md`, `CLAUDE.md` | Read on demand for a specific kind of work. |
| **F — Completed historical plan** | `docs/archive/project-history/` | Done. Preserved for traceability. |
| **G — Superseded research** | `docs/archive/blocker-b-family-b/` | Abandoned path. **Not a roadmap.** |
| **H — Postmortem/archive** | `docs/archive/postmortems/` | Lessons from stopped work. |

### ADR register

Every ADR below is **ACCEPTED** and none is archived. Acceptance is authority; it is not
a reading obligation.

| ADR | Status | Authority | Who needs to read it |
| --- | --- | --- | --- |
| 0001 — resource identity / incarnation | **ACCEPTED** | normative | anything touching identity, VMID-as-locator, incarnation, binding generations, terminal history |
| 0002 — Proxmox discovery / reconciliation | **ACCEPTED** | normative | discovery, reconciliation, presence, absence, provider/transport contracts |
| 0003 — source binding / attestation | **ACCEPTED** | normative | source binding, attestation, endpoint activation/failover questions |
| 0004 — confirmed removal / operator absence | **ACCEPTED** | normative | confirmed removal, authoritative absence proof |
| 0005 — workload continuity enrollment | **ACCEPTED**, *scoped*: the negative stock-PVE trust boundary and R0 safety decision only. Does not close Blocker B, does not authorize WAVE B1, grants `trusted` nowhere | normative within that scope | continuity/trust questions; anyone proposing mutation authority |
| 0006 — workload continuity, stronger proof | **ACCEPTED**, *scoped*: negative/unresolved research record plus normative requirements for any future positive mechanism. Selects no mechanism | normative within that scope | continuity-proof research; anyone proposing a Blocker-B mechanism |
| future positive Blocker-B mechanism ADR | **NOT STARTED** — a different, later ADR | — | — |

There is no PROPOSED or SUPERSEDED ADR in this repository.

---

## 4. Default agent reading set

### ALWAYS READ (every task)

1. `AGENTS.md` — binding repository rules.
2. `docs/architecture/README.md` — this file.
3. `docs/product-intent.md` — what the product is supposed to become.
4. `docs/architecture/0.5-implementation-status.md` — what exists today.

That is the whole default set. `CLAUDE.md` additionally describes where things
live and how to run them; read it when you need commands or the repository map.

### READ ONLY WHEN RELEVANT

See the task matrix in §5.

### DO NOT READ BY DEFAULT

- `docs/archive/blocker-b-family-b/` — abandoned Family B / B-S1 research.
- PR #52 / PR #53 branch material (`research/family-b-13-*`) — unmerged,
  abandoned implementation plans.
- `docs/archive/project-history/` — completed R0 activation chronology,
  dogfood narratives, corrective-review history.
- `docs/archive/postmortems/` — read only when re-entering the covered area.
- `docs/architecture/research/adr0006-workload-continuity-evidence.md` — only
  for continuity-proof research.

---

## 5. Task-to-document matrix

| Task | Read | Do not read |
| --- | --- | --- |
| **General product work** | the always-read set | anything in `docs/archive/` |
| **Package scanning / update plan** (not implemented — package scanning needs its own accepted read-only evidence/transport contract before implementation, not gated by Blocker B) | always-read set; `docs/product-intent.md` §2–§5; ADR 0002 for what discovery may and may not assert; `AGENTS.md` mutation-boundary section | Family B research; the R0 chronology |
| **Inventory / reconciliation / identity** | ADR 0001, ADR 0002, `0.5-inventory-model.md`; `.agents/skills/hubinet-phase-boundary` | ADR 0003–0006 unless the change touches attestation/removal/continuity |
| **Home Assistant integration** | `0.5-inventory-model.md` (snapshot contract), `0.5-foundation.md` "Dynamiczny model Home Assistant", `.agents/skills/hubinet-phase-boundary`, `docs/operations/0.5-ha-clean-break.md` | backend ADR detail beyond the snapshot contract |
| **R0 runtime, PVE transport, scheduler, config, installer** | `docs/architecture/0.5-r0-read-only-runtime-activation.md` (the code cites it by section) | the R0 activation chronology |
| **Mutation / jobs / snapshots** (Phase 1C — blocked) | `AGENTS.md` "Mutation and security boundaries"; ADR 0005 §26; `docs/product-intent.md` §4–§5; `.agents/skills/hubinet-architecture-change` | Family B research — it does not unblock this |
| **Source attestation / endpoint binding** | ADR 0003 | ADR 0004–0006 |
| **Confirmed removal / absence proof** | ADR 0004 | ADR 0003, 0005, 0006 |
| **Persistent continuity / Blocker B** | ADR 0005, ADR 0006, then `docs/architecture/research/adr0006-workload-continuity-evidence.md`, then `docs/archive/postmortems/blocker-b-family-b-13.md` | the four archived Family B research documents, unless you have a specific reason |
| **Deployment / operations** | `deploy/README-bootstrap-proxmox-0.5.md`, `deploy/README-0.5-firewall.md`, `docs/operations/0.5-r0-operational-activation.md`; for a deployment/runtime implementation change, also the AGENTS.md-required architecture baseline (`0.5-foundation.md`, `0.5-inventory-model.md`, ADR 0001, ADR 0002) | ADR 0003–0006 unless the change touches attestation/removal/continuity; archive/history unless specifically needed |
| **Code review** | `.agents/skills/hubinet-contract-review`, `AGENTS.md` "Code Review Rules", the status document | broad architecture/research sweeps |
| **Declaring work done / merge-safe** | `.agents/skills/hubinet-test-gate` | — |
| **Historical research question** ("why did X stop?") | `docs/archive/postmortems/`, then `docs/archive/` | — |

---

## 6. Archive policy

Archived material lives under `docs/archive/`. See `docs/archive/README.md`.

A document is archived when **all** of these hold: it is not an ACCEPTED ADR; no
ACCEPTED ADR references it; it is not needed for current implementation, review,
or operations work; and it records a completed, superseded, or abandoned path or
is primarily historical narrative.

A document is **not** archived merely because it is large, old, or about a
blocked area. An ACCEPTED ADR is never archived just because it is off the
immediate product critical path.

Archiving is done with `git mv` so history is preserved, archived files stay
tracked, and archiving changes **no** architecture authority and **no** ADR
status. Deleting historical material is a separate operator decision.

### Warning

> **Archived and superseded research must never be treated as the current
> implementation direction.** These documents are long, detailed, and carefully
> reviewed, which makes them read like plans. They are not. If a document under
> `docs/archive/` appears to describe work to do, it does not.

---

## 7. Acceptance and supersession (the ratchet rule)

The project previously allowed accepted-stage reasoning to be silently re-opened
by later review. The rule is:

1. **Committed artifacts are immutable historical facts.** A commit, a merged
   PR, an accepted ADR revision, and a recorded review verdict happened. They
   are never edited to look like they did not.
2. **A later witness may falsify a load-bearing claim.** Acceptance is not
   permanent immunity. If concrete new evidence contradicts a claim an accepted
   document depends on, that is a real finding, not an out-of-scope objection.
3. **Supersession is explicit, never implicit.** An accepted document is
   superseded or revoked only by an explicit later decision that names it, states
   the falsifying witness, and records the new status. There is no silent
   downgrade.
4. **Downstream work stops and reassesses.** When a load-bearing claim falls,
   work that depended on it halts pending the reassessment — it does not continue
   on the strength of the old acceptance.
5. **History is not rewritten.** Superseded text is not edited to pretend it was
   never accepted. It is marked superseded, moved to `docs/archive/`, or clearly
   indexed as historical, with the superseding decision named.
6. **The reverse rule is explicitly rejected.** "A later finding cannot affect an
   old accepted claim" is not the rule and must never be adopted — it would let
   known-false architecture stay load-bearing.

Applied to the current state: ADR 0005 and ADR 0006 remain **ACCEPTED**. The
Family B / B-S1 *research path* was stopped and archived, which changed no ADR
status and closed no blocker. Blocker B remains **OPEN**.

---

## 8. Directory map

```text
AGENTS.md                                  binding agent rules
CLAUDE.md                                  repository map + how to run things
README.md                                  product/user-facing overview
docs/
  product-intent.md                        current product target (operator)
  architecture/
    README.md                              this index
    0.5-foundation.md                      ACCEPTED Phase 0 decisions
    0.5-inventory-model.md                 ACCEPTED inventory model
    0.5-implementation-status.md           current implementation map
    0.5-r0-read-only-runtime-activation.md live R0 design contract
    adr/0001..0006                         ACCEPTED ADRs
    research/
      adr0006-workload-continuity-evidence.md   non-normative, ADR-referenced
  operations/
    0.5-r0-operational-activation.md       real-host activation runbook
    0.5-ha-clean-break.md                  HA 0.4->0.5 clean-break plan
  archive/                                 NON-AUTHORITATIVE HISTORY
    blocker-b-family-b/                    abandoned Family B / B-S1 research
    postmortems/                           concise postmortems
    project-history/                       verbatim historical narrative
```
