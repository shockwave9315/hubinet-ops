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
- A failed, partial, or unavailable scan is **never** deletion, and a failed
  package scan is **never** "zero updates".

Current implemented state: PVE autodiscovery, dynamic backend inventory, and
dynamic Home Assistant representation are **live and read-only**. Package
scanning, update plans, jobs, snapshots, and rollback are **not implemented**.

### Threat model

Hubinet Ops is an application for a **trusted, self-administered** Proxmox
environment. The full statement is in `AGENTS.md`, "Threat model". In short:

- **TRUSTED** — the Proxmox administrator/root, the Proxmox host, root inside a
  managed LXC, the Hubinet operator, normal `apt`/`dpkg` behavior.
- **OUT OF SCOPE** — a malicious root inside a managed guest racing or editing
  files to fool Hubinet; a malicious or compromised Proxmox root; an
  administrator deliberately replacing Hubinet-owned state; any security proof
  intended to survive full administrative compromise of the managed environment.

Do not design defenses for the out-of-scope cases. This is **not** a licence to
weaken ordinary application safety — see the KEEP list in `AGENTS.md`.

The former security-proof architecture built for the old hostile-administrator
model (source attestation, attestation epochs, relationship gates,
candidate-endpoint attestation proofs, dual-evidence confirmed removal, and the
Blocker-B workload-incarnation proof) is **superseded and archived** under
`docs/archive/superseded-security-model/`, together with its implementing code,
schema, and tests. **Blocker B is no longer a blanket prerequisite** for the
practical operator-driven roadmap.

### What needs an ADR, and what does not

ADRs remain available for genuinely load-bearing decisions. They are **not** a
prerequisite for ordinary implementation:

- Package scan/update/snapshot/job work does **not** need a new ADR merely
  because it touches a managed guest.
- Write a new ADR when a decision is genuinely architectural and hard to
  reverse — a new persistence/authority owner, a new trust boundary, a new
  external mutation path — not as a routine gate on ordinary work.

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
- **An ADR is not a prerequisite for ordinary implementation.** ADRs record
  genuinely architectural decisions. Routine feature work — including package
  scanning, update plans, jobs, snapshots, and rollback — proceeds under the
  accepted inventory architecture and the product rules, without first minting
  a new ADR.

---

## 3. Documentation categories

| Category | Where | Meaning |
| --- | --- | --- |
| **A — Active authority** | `docs/architecture/adr/0001`–`0002`, `0.5-foundation.md`, `0.5-inventory-model.md`, `AGENTS.md` | Normative and binding for architecture and security invariants. |
| **A2 — Active product intent** | `docs/product-intent.md` | Binding for **product direction and priority** — what to build next and what the product must never do. **Not** an ADR and **not** architecture authority: on any architectural or security invariant, an ACCEPTED ADR wins. |
| **B — Active implementation contract** | `docs/architecture/0.5-r0-read-only-runtime-activation.md` | Cited by name and section from live runtime code and tests. |
| **C — Active current status** | `docs/architecture/0.5-implementation-status.md` | The current map. Not authority. |
| **D — Active operator/user doc** | `README.md`, `docs/operations/*`, `deploy/README-*.md`, `CHANGELOG.md` | How to install, deploy, and operate. |
| **E — Specialized reference** | `.agents/skills/*`, `custom_components/hubinet_ops/NOTICE.md`, `CLAUDE.md` | Read on demand for a specific kind of work. |
| **F — Completed historical plan** | `docs/archive/project-history/` | Done. Preserved for traceability. |
| **G — Superseded research and architecture** | `docs/archive/superseded-security-model/`, `docs/archive/blocker-b-family-b/` | Retired threat model and abandoned research. **Not a roadmap.** |
| **H — Postmortem/archive** | `docs/archive/postmortems/` | Lessons from stopped work. |

### ADR register

| ADR | Status | Authority | Who needs to read it |
| --- | --- | --- | --- |
| 0001 — resource identity / incarnation | **ACCEPTED** | normative | anything touching identity, VMID-as-locator, incarnation, binding generations, terminal history |
| 0002 — Proxmox discovery / reconciliation | **ACCEPTED** | normative | discovery, reconciliation, presence, absence, provider/transport contracts |
| 0003 — source binding / attestation | **SUPERSEDED**, archived | none | nobody, by default |
| 0004 — confirmed removal / operator absence | **SUPERSEDED**, archived | none | nobody, by default |
| 0005 — workload continuity enrollment | **SUPERSEDED**, archived | none | nobody, by default |
| 0006 — workload continuity, stronger proof | **SUPERSEDED**, archived | none | nobody, by default |

ADRs 0003–0006 were superseded by the operator's threat-model reset and moved to
`docs/archive/superseded-security-model/` (see that directory's README for what
they claimed and what replaced them). ADR 0001 and ADR 0002 remain ACCEPTED and
normative: identity, incarnation, discovery, and reconciliation are unchanged.
Where the archived ADRs' text is still cited inside ADR 0001/0002 or the
inventory model, read it as historical cross-reference, not as live authority.

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

- `docs/archive/superseded-security-model/` — the retired attestation/
  confirmed-removal/Blocker-B architecture. Superseded; not a roadmap.
- `docs/archive/blocker-b-family-b/` — abandoned Family B / B-S1 research.
- PR #52 / PR #53 branch material (`research/family-b-13-*`) — unmerged,
  abandoned implementation plans.
- `docs/archive/project-history/` — completed R0 activation chronology,
  dogfood narratives, corrective-review history.
- `docs/archive/postmortems/` — read only when re-entering the covered area.

---

## 5. Task-to-document matrix

| Task | Read | Do not read |
| --- | --- | --- |
| **General product work** | the always-read set | anything in `docs/archive/` |
| **Package scanning / update plan** (not implemented) | always-read set; `docs/product-intent.md` §2–§5; ADR 0002 for what discovery may and may not assert; `AGENTS.md` "Threat model" and mutation-boundary section | `docs/archive/` |
| **Inventory / reconciliation / identity** | ADR 0001, ADR 0002, `0.5-inventory-model.md`; `.agents/skills/hubinet-phase-boundary` | `docs/archive/` |
| **Home Assistant integration** | `0.5-foundation.md` (including "Dynamiczny model Home Assistant"), `0.5-inventory-model.md` (snapshot contract), ADR 0001, ADR 0002, `.agents/skills/hubinet-phase-boundary`, `docs/operations/0.5-ha-clean-break.md` | `docs/archive/` |
| **R0 runtime, PVE transport, scheduler, config, installer** | `docs/architecture/0.5-r0-read-only-runtime-activation.md` (the code cites it by section) | the R0 activation chronology |
| **Update execution / jobs / snapshots** (not implemented) | `AGENTS.md` "Threat model" and "Mutation and security boundaries"; `docs/product-intent.md` §4–§5 | `docs/archive/` |
| **Deployment / operations** | `deploy/README-bootstrap-proxmox-0.5.md`, `deploy/README-0.5-firewall.md`, `docs/operations/0.5-r0-operational-activation.md`; for a deployment/runtime implementation change, also the AGENTS.md-required architecture baseline (`0.5-foundation.md`, `0.5-inventory-model.md`, ADR 0001, ADR 0002) | `docs/archive/` unless specifically needed |
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

Applied to the current state: ADR 0003–0006 were **explicitly superseded** by
the operator's threat-model reset, which names what changed (the environment
being managed is trusted; hostile-administrator resistance is out of scope) and
records the new status. Their text is preserved unedited under
`docs/archive/superseded-security-model/`. ADR 0001 and ADR 0002 are untouched
and remain ACCEPTED.

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
    adr/0001..0002                         ACCEPTED ADRs
  operations/
    0.5-r0-operational-activation.md       real-host activation runbook
    0.5-ha-clean-break.md                  HA 0.4->0.5 clean-break plan
  archive/                                 NON-AUTHORITATIVE HISTORY
    superseded-security-model/             retired ADR 0003-0006 + evidence
    blocker-b-family-b/                    abandoned Family B / B-S1 research
    postmortems/                           concise postmortems
    project-history/                       verbatim historical narrative
```
