# ADR 0005: workload continuity enrollment and trust

Status: **PROPOSED**

This ADR is not yet accepted architecture. It does not authorize any schema,
persistence, or runtime implementation by itself. It does not amend ADR 0001,
ADR 0002, ADR 0003, or ADR 0004; where it depends on their invariants it cites
them and adds a new, narrower normative layer on top, exactly as ADR 0003 and
ADR 0004 each added their own layer without changing the others.

## 1. Context / problem

ADR 0001 defines the canonical `security_continuity` axis
(`unverified`/`trusted`/`revoked`) and states, as an accepted but explicitly
unresolved point: "Przyszły enrollment musi zdefiniować continuity proof (oraz
sposób jego odczytu i ochrony) przed nadaniem `trusted`. ADR nie przesądza
jeszcze mechanizmu enrollment." `0.5-inventory-model.md` repeats this as an
explicit runtime-activation gate: "brak zaakceptowanego workload continuity
proof/enrollment model blokuje globalnie `trusted` destructive capabilities i
każdą zależną mutację." This is "Blocker B" — the one capability-specific gate
that, unlike Blocker A (ADR 0004, now accepted) and Blocker C (ADR 0003, now
accepted), has had **no architecture work of any kind** until this ADR.

Blocker B is the highest-stakes of the three: source attestation (Blocker C)
only gates *which trust domain* a source belongs to, and confirmed removal
(Blocker A) only gates a terminal *closure* decision. `security_continuity=
trusted`, by contrast, is the one accepted precondition standing between a
resource and **destructive mutation authority** over a running or existing
workload (ADR 0001: "stored policy allows operation ∩ policy applicability ∩
trusted security continuity ∩ backend capability ∩ ..."). Getting this wrong
in either direction is asymmetric: too weak, and an attacker or an ordinary
accidental collision authorizes destructive action against the wrong
incarnation; too strict, and the whole 0.5 mutation model never has a
legitimate mutation target at all. This ADR's job is to resolve that
correctly, honestly, and completely enough that WAVE B1 has no security
decision left to invent — even if the honest answer turns out to be narrower
than a product team would prefer.

## 2. Scope and non-goals

In scope: the concept, terminology, threat model, evidence audit, and exact
normative semantics of workload/resource continuity enrollment and trust —
what evidence may transition `security_continuity: unverified -> trusted`,
what keeps it `trusted`, what makes it `revoked`, and how all of this
interacts with every existing accepted revision/epoch/fencing mechanism (ADR
0001's `resource_continuity_revision`, ADR 0002's discovery/freshness
discipline, ADR 0003's `source_attestation_epoch`, ADR 0004's confirmed-
removal terminal transition).

Explicitly **not** in scope, and not authorized by this ADR:

- any schema, table, column, trigger, or enum-value implementation;
- any bump of the authority schema version (currently `5`, merged on
  `main`; any future bump is WAVE B1's implementation concern, not this
  ADR's);
- self-acceptance of this ADR by the agent that wrote it;
- any production mechanism that **writes** into a guest's configuration or
  provisions anything inside a guest — that would be a mutation, and no
  mutation authority exists yet (§27);
- any change to production startup, scheduler, HTTP, MQTT, or Home
  Assistant wiring;
- any change to ADR 0001, ADR 0002, ADR 0003, or ADR 0004;
- any policy/approval/job/lock/plan authority of any kind (§26);
- endpoint activation, candidate promotion, or failover (that remains ADR
  0003 §15's separate, still-unauthorized future ADR);
- a tier-2 or tier-3 continuity mechanism design (§11, §12 record only
  that these remain open, exactly as ADR 0003 left its own tier 2/3 open
  where stock PVE could not close them);
- any weakening of the existing fail-closed posture that observational
  continuity never implies security continuity, that discovery alone
  never grants trust, and that a `resource_continuity_revision`/CAS
  mismatch always fails closed.

## 3. Existing accepted invariants that remain unchanged

Restated here only as the fixed floor this ADR builds on:

1. `resource_id` is the immutable backend identity of one inventory
   incarnation; for `unverified` it is explicitly **not** proof of physical
   incarnation continuity (ADR 0001).
2. No stock PVE field — VMID, name, node, config digest, QEMU `vmgenid`,
   `smbios1.uuid`, `meta.ctime`, tags, MAC/disk fingerprints — constitutes
   positive continuity proof for both QEMU and LXC; ADR 0001's own
   candidate audit already reaches this conclusion for plain *identity*
   continuity, which this ADR does not re-litigate (§6 only extends the
   same conclusion to the strictly *stronger* claim `security_continuity=
   trusted` requires).
3. A silent delete/recreate between two identical, complete polling
   observations can be indistinguishable from stock PVE alone; the backend
   may retain the same `resource_id` for read-only/UX continuity in that
   case, but this **never** authorizes destructive mutation (ADR 0001:
   "resource_id bez security_continuity=trusted nigdy nie autoryzuje
   mutacji").
4. `observational_continuity` (`consistent`/`uncertain`/`replaced`) and
   `security_continuity` (`unverified`/`trusted`/`revoked`) are separate
   axes with separate owners; ADR 0001's canonical state matrix already
   forbids inferring one from the other automatically, and this ADR
   introduces no exception.
5. `resource_continuity_revision` is a monotonic per-resource security/
   concurrency token, already defined to increase on every
   `security_continuity` transition, enrollment/proof/anchor revision, and
   trust revocation (ADR 0001) — this ADR reuses it exactly as specified,
   inventing no second resource-level security token (mission requirement).
6. Direct replacement and confirmed removal each already fully define
   their own terminal effect on `security_continuity` (`trusted -> revoked`,
   never-trusted stays `unverified`, no successor inherits anything) —
   ADR 0001 and ADR 0004 respectively; this ADR does not touch either
   transition, only the *positive* `unverified -> trusted` direction and
   its own revocation path.
7. Source attestation (ADR 0003) grants no workload/mutation authority by
   itself and is never sufficient for resource trust (ADR 0003 §28); this
   ADR does not change that, and does not let resource trust silently
   imply source trust either.
8. Node/hostd attestation (ADR 0001's node section, `0.5-inventory-model.md`
   `node_attestations`) is a **separate axis** from resource continuity;
   this ADR does not design node attestation and does not let it stand in
   for resource trust (§24).
9. Discovery is strictly read-only and never grants trust, management,
   maintenance, or destructive capability by itself (ADR 0002, AGENTS.md).
10. The full mutation trust path (HA → API → backend policy/plans/jobs/
    locks/audit → typed host-control → hostd/forced-command → Proxmox)
    remains entirely unimplemented; this ADR's evidence model is a
    necessary precondition for a future mutation-authority decision, never
    itself sufficient (§26).

## 4. Terminology

| Term | What it is | Owned by |
| --- | --- | --- |
| **Observational continuity** | ADR 0001's read-only assessment of whether current facts belong to the same incarnation | `resource_incarnations` (ADR 0001, unchanged) |
| **Security continuity** | ADR 0001's `unverified`/`trusted`/`revoked` axis this ADR defines the *positive* transition for | `resource_incarnations` (ADR 0001, unchanged vocabulary) |
| **Continuity evidence / proof** | one instance of a specific evidence class, read via the trusted reader boundary (§18/§19), that a human operator accepts as the basis for an enrollment decision | **new concept, this ADR** |
| **Enrollment** | the explicit, audited decision binding one exact resource incarnation to one accepted continuity-evidence class/value, at one enrollment revision | **new concept, this ADR** |
| **Physical/logical workload continuity** | whether the actual running guest (its disk state, memory, process) is genuinely the same one enrolled | **not represented by any Hubinet Ops primitive, before or after this ADR** — see §9's tier limitations |
| **Node/host trust** | ADR 0001's separate `node_binding`/`node_attestation` axis | `node_bindings`/`node_attestations` (ADR 0001, unchanged) |

This ADR designs a **three-tier evidence model** for continuity proof (§9),
deliberately mirroring ADR 0003's three-tier source-attestation model — the
same tier vocabulary and the same central discipline apply: **evidence
equality at any tier is not, and must never be treated as, proof of physical
workload identity** (§10, §11).

## 5. Threat model

- an attacker (or an innocent operator mistake) destroys the enrolled
  workload and recreates a different one under the same VMID, hoping stock
  PVE facts alone (name, digest, node) will make the new occupant appear
  already-trusted;
- an attacker who can edit a guest's configuration (a lower privilege bar
  than destructive/PowerMgmt) copies any config-resident marker into a
  guest they control, hoping possession of the marker string alone proves
  continuity;
- backup/restore, disk cloning, or template-based provisioning produces two
  or more guests carrying identical config-resident or disk-resident
  evidence, simultaneously or sequentially;
- an operator's one-time enrollment click is relied upon far later, after
  an invisible destroy/recreate has occurred in between, without any
  re-verification at the actual mutation decision point;
- trust evidence established while a source's trust-domain continuity was
  itself unproven, or later invalidated by an accepted anchor change
  (ADR 0003 §20's authority-eligibility rule), is cited as if still valid;
- a resource legitimately passes through an accepted observational gap
  (`missing`, later `present` again with identical facts) and the system
  is tempted to silently restore prior trust because "nothing looks
  different" (ADR 0001 §3 item 3 already forbids this for read-only
  identity; this ADR must not weaken that for the *stronger* trust claim);
- two operators race to enroll or re-enroll the same resource concurrently;
- a crash or restart occurs mid-enrollment, and stale in-memory state is
  relied upon instead of durable committed state;
- a resource's continuity evidence is duplicated onto a second, unrelated
  resource (same or different source), and the system must decide whether
  that is even detectable, and what "detectable" can honestly mean.

None of these is solvable by observing PVE more carefully — they are
exactly the class of threat ADR 0001's own candidate audit already proved
stock PVE facts cannot answer for *identity*; this ADR must not quietly
assume a *stronger* claim (destructive trust) is somehow easier to prove
than the *weaker* one ADR 0001 already gave up on.

## 6. Candidate evidence audit

Legend, identical to ADR 0001's own discipline:

- **FACT-DOC** — documented by Proxmox;
- **FACT-SOURCE** — behavior visible in official Proxmox source;
- **INFERENCE** — architectural conclusion from the facts;
- **UNKNOWN** — property not confirmed by an official contract.

| # | Candidate | QEMU | LXC | Evaluation |
| --- | --- | --- | --- | --- |
| 1 | VMID/CTID | yes | yes | **FACT-DOC** (ADR 0001, cited): reusable slot locator, never identity. Rejected outright — already established. |
| 2 | resource type | yes | yes | **FACT-DOC** (ADR 0001): immutable *occupant* property, but says nothing about which specific occupant. Rejected as continuity proof by itself. |
| 3 | name/hostname | yes | yes | **FACT-DOC** (ADR 0001): mutable config, rename explicitly preserves identity by design — cannot simultaneously be identity proof. Rejected. |
| 4 | current node | yes | yes | **FACT-DOC** (ADR 0001): migrates by design; a relation, not identity. Rejected. |
| 5 | config digest | yes | yes | **FACT-SOURCE** (ADR 0001): changes on any edit, can return to a prior value, no create-time binding. Rejected. |
| 6 | QEMU `vmgenid` | yes | n/a | **FACT-SOURCE** (ADR 0001): explicitly regenerated on clone/snapshot-rollback/restore; not cross-type. Rejected as continuity proof (its own purpose — detecting *unexpected* rollback for the guest OS itself — is unrelated to Hubinet-side incarnation proof). |
| 7 | `smbios1.uuid` | yes | n/a | **FACT-SOURCE**, newly re-confirmed this session directly from `qm.adoc`: clone explicitly "generate[s] a new UUID for the VM BIOS (smbios1) setting" specifically "to avoid resource conflicts" — i.e. Proxmox itself treats this UUID as *needing* to change on clone, the opposite of a continuity anchor. Not cross-type. Rejected. |
| 8 | `meta.ctime` / creation metadata | yes | UNKNOWN | **FACT-SOURCE** (ADR 0001): no LXC equivalent found; all restore-path guarantees UNKNOWN even for QEMU. Rejected. |
| 9 | MAC addresses | yes | yes (veth) | **FACT-DOC**, newly re-confirmed this session: clone explicitly randomizes all NIC MAC addresses. Configurable, copyable, and Proxmox itself does not preserve it across the one lifecycle event (clone) most relevant to this threat model. Rejected. |
| 10 | disk/storage identifiers | yes | yes | **INFERENCE**: disk content, including anything written inside it, is exactly what clone/backup/restore copy by design — this is the mechanism the threat model is about, not a defense against it. Rejected as continuity proof; relevant later as the reason tier-2/tier-3 guest-resident evidence cannot solve clone-resistance either (§11). |
| 11 | PVE tags | yes | yes | **FACT-SOURCE**, newly confirmed this session directly from `qemu-server`/`pve-container` schema (`PVE::QemuServer`, `PVE::LXC::Config`): `tags => { type => 'string', format => 'pve-tag-list', description => 'Tags of the VM/Container. This is only meta information.' }` — Proxmox's own schema comment states this is meta information only. Copied on clone (not excluded/regenerated per `qm.adoc`'s clone section). Constrained tag-list format limits usable entropy. Rejected as a *stock* identity signal for the same reason as digest/name; evaluated separately as a possible enrollment-marker *storage location* in §9 and rejected there in favor of `description` on entropy/format grounds. |
| 12 | PVE description/comment | yes | yes | **FACT-SOURCE**, newly confirmed this session directly from source: `description => { type => 'string', maxLength => 1024*8, description => "... saved as comment inside the configuration file." }`, identically defined for both QEMU (`PVE::QemuServer`) and LXC (`PVE::LXC::Config`). Copied on clone (not excluded/regenerated). Not itself continuity proof — but see §9: selected as the *storage location* for the operator-controlled tier-1 marker, precisely because it is generic, cross-type, sufficiently large, and already read via the exact `VM.Audit`-gated config read ADR 0002's endpoint matrix already authorizes ("detail-only" class) — no new privilege required. |
| 13 | arbitrary Hubinet-owned marker in guest config | yes | yes | Not a stock field — a **backend-generated value the operator stores** in `description` (§12 above is its concrete host field). This is Family C, §9. |
| 14 | Hubinet-owned marker stored outside guest config | UNKNOWN | UNKNOWN | No stock PVE per-guest metadata store outside the guest's own config object is documented; storing enrollment evidence in Hubinet's own database (not in PVE at all) removes it from PVE's reach entirely, which means it can never be *read back from the guest* to prove anything about the guest — it degenerates to Family B (pure administrative assertion, §8) unless paired with a config-resident or guest-resident readable value. Not a standalone candidate; folded into §9's design (the marker's *expected value* lives in Hubinet's DB, its *live value* must still be read from the guest). |
| 15 | guest-resident opaque enrollment token (on-disk, no crypto) | yes | yes | **INFERENCE**: on-disk content is copied by clone/backup exactly like candidate 10. No cryptographic binding to anything not-clonable. Strictly weaker than tier-2 (§11) for equal implementation cost; rejected as its own family. |
| 16 | guest-resident asymmetric key / cryptographic agent | yes (via QGA) | via exec | Evaluated as Family D, §11. Genuinely cryptographic (proves key possession at read time) but the private key material still lives in disk state that clone/backup copy identically — does not solve clone-resistance. Not designed in this ADR (tier 2, future). |
| 17 | QEMU Guest Agent–derived evidence | yes | n/a | **FACT-DOC**: QGA requires the in-guest agent installed, configured, and the guest running and cooperative; not installed by default; exec-class QGA commands require elevated PVE privilege beyond `VM.Audit` (`VM.Monitor`/`VM.Console`-class, not verified to the exact string in this session — flagged **UNKNOWN** pending a future implementation-time contract review, mirroring ADR 0002's own discipline of re-verifying exact privilege strings at implementation time). Not designed in this ADR. |
| 18 | LXC filesystem/exec-derived evidence | n/a | yes, via `pct exec`/`lxc-attach` | **FACT-DOC** confirms `pct enter`/`pct console` exist for interactive root access; the exact privilege contract for scripted `pct exec`-class evidence collection was **not confirmed to a primary source in this session** — **UNKNOWN**, flagged for future implementation-time verification. Not designed in this ADR. |
| 19 | trusted node/hostd-mediated workload evidence | yes | yes | Evaluated as Family E, §24. A trusted node route is a precondition for *safely asking* a host to read anything, but the node itself attesting "this workload looks like X" is still bounded by the same on-host/on-disk observability limits as every other candidate here — node trust narrows *where* evidence can be safely collected from, it does not itself manufacture stronger evidence. Kept structurally separate from resource trust (§24), not designed as a proof mechanism of its own. |
| 20 | TPM/vTPM or equivalent | yes (software vTPM) | n/a | **FACT-DOC/INFERENCE**: stock Proxmox VE's vTPM is a software-emulated TPM whose state is itself a disk image (a `vtpm0` volume) — it is copied by clone/backup/snapshot exactly like any other disk (candidate 10's limitation applies identically). A genuinely hardware-rooted, non-clonable TPM attestation chain is **not** a stock PVE guarantee. Rejected as a *stock* mechanism; the honest gap this leaves open is exactly ADR 0003's own tier-3 gap, reused here as tier 3 (§12). |
| 21 | explicit operator administrative assertion | yes | yes | Evaluated as Family B, §8. Necessary, never sufficient alone. |
| 22 | combinations of the above | — | — | The selected minimal path (§9) *is* a combination: an operator assertion (§8) bound to a specific, backend-controlled, re-verified marker value (§9/§12) — never either alone. |

**Conclusion of the audit, stated explicitly per the mission's own
instruction not to force a false solution:** no field candidate 1–12, 14–15,
17–20 constitutes continuity proof on its own, for the identical reasons ADR
0001 already established for the weaker identity-continuity question. This
is not a gap this ADR failed to close — it is the same accepted, honestly
documented limitation of stock Proxmox VE, now confirmed to also foreclose
any *stock-only* answer to the *stronger* trust question (§7).

## 7. Why stock-PVE-only continuity is insufficient (Family A — rejected)

ADR 0001's own conclusion — "żadne pole nie daje pozytywnego dowodu
ciągłości dla obu typów" (no field gives positive continuity proof for
either type) — was reached for the *weaker* claim of observational identity
continuity. `security_continuity=trusted` is strictly *stronger*: it must
justify destructive mutation authority, not merely "the backend may keep
presenting the same `resource_id`." A claim that is already proven
unreachable from stock facts for the weaker case cannot become reachable for
the stronger case by assembling the same facts differently. **Family A is
rejected explicitly and completely**: no combination of VMID, name, node,
digest, `vmgenid`, `smbios1.uuid`, `meta.ctime`, MAC/disk fingerprints, tags,
or description content, read passively from ordinary discovery, may ever be
treated as sufficient evidence for `security_continuity=trusted`. This is a
permanent architectural conclusion, not a placeholder pending better PVE
research.

## 8. Why operator assertion alone is insufficient (Family B — rejected as sole mechanism)

An operator who, at time T0, examines a resource and asserts "this is
trusted" is asserting something true *at T0*. The threat model's core
concern is what happens at a *later* mutation decision at T1 ≫ T0: if the
resource was invisibly destroyed and recreated under the identical VMID
between T0 and T1 (indistinguishable from stock PVE polling, per ADR 0001
§3 item 3), a bare T0 assertion — even if perfectly honest — says nothing
about what actually occupies the slot at T1. Re-checking `resource_id`/
`resource_continuity_revision` by CAS at T1 does not help either: those
tokens track the *backend's own* record-keeping, which (per ADR 0001) can
legitimately retain the same `resource_id` across exactly this kind of
invisible gap. A bare operator assertion is therefore **never sufficient by
itself** to carry trust forward across time, however honestly made.

This does **not** mean operator judgment is worthless — it is the
*opposite* of worthless: it is the only source of the human, administrative
decision this ADR (and ADR 0004's analogous Class-C path) both require.
What Family B lacks, alone, is anything that can be **independently
re-verified against the live resource** at each later decision point. §9
resolves this by requiring the operator assertion to always be paired with,
and re-verified against, a specific, backend-controlled, re-readable value
— never accepted as a standalone signal.

## 9. Recommended decision — three-tier evidence model

Mirroring ADR 0003's own recognition that a real gap in stock PVE
capabilities does not mean "give up," this ADR adopts a three-tier
evidence model for resource continuity, directly parallel to ADR 0003's
tiers for source attestation:

- **Tier 1 — operator-controlled marker evidence.** The operator, acting
  entirely out-of-band (through their own Proxmox access, not through any
  Hubinet Ops mutation — §2, §27), sets a high-entropy, backend-issued
  value into the guest's `description` field (§6 candidate 12 — chosen
  over `tags` for its far larger `maxLength` and unconstrained string
  format). The operator then supplies that same expected value to Hubinet
  Ops as part of one explicit enrollment decision. Hubinet Ops
  **read-only** verifies, via the same `VM.Audit`-class config read ADR
  0002 already authorizes, that the live guest's `description` currently
  contains the expected value, and if so, accepts the enrollment. **This
  ADR recommends tier 1 as the baseline mechanism for the minimal
  supported B1 path** (§13 onward specify it completely).
- **Tier 2 — guest-resident cryptographic evidence (Family D, §6 candidate
  16).** Not designed by this ADR. Would raise the bar from "copy a
  string" to "possess private key material," exactly as ADR 0003 §10a
  describes for source-level tier 2 — but, exactly as ADR 0003 §10a and
  §11 already establish for the source case, does **not** solve
  clone-resistance: the private key lives in disk/config state that
  clone/backup copy identically (§6 candidate 10, 20). A future ADR may
  design this as *additional, optional, corroborating* evidence, never as
  a tier-1 replacement and never sufficient by itself to authorize a
  state change beyond what tier 1 already authorizes.
- **Tier 3 — a future, stronger, genuinely clone-resistant mechanism**
  (e.g., a hardware-rooted attestation chain unavailable from stock,
  software-emulated PVE vTPM, or an out-of-band Hubinet-managed identity
  provisioned and verified through a channel that is not itself guest disk
  state). Not designed by this ADR, and **not available from stock
  Proxmox VE** — this is the honest gap this ADR leaves open, exactly
  parallel to ADR 0003 §7's tier 3.

The central discipline governing all three tiers, restated because it is
load-bearing: **evidence match, at any tier, is not proof of physical or
logical workload continuity.** Tier 1 is not cryptographic proof of
anything — it is an operator-accepted assertion corroborated by a
re-checkable marker. Tier 2, if ever designed, would be genuinely
cryptographic but not clone-resistant. Tier 3 remains undesigned. None of
the three proves that the guest currently occupying the slot is, at the
level of physical disk/process state, the *same* guest the operator
originally examined — they prove different, progressively stronger, but
still fundamentally bounded claims about what evidence is currently
observable, exactly as ADR 0003 §9/§10/§10a/§11 already established for the
analogous source-domain question.

## 10. What tier-1 evidence honestly establishes

**What it establishes:** that, at enrollment time, the operator personally
set a specific, unpredictable, backend-issued value into the exact guest
they intended to enroll, and Hubinet Ops independently read that exact
value back from the exact resource being enrolled over an already-
authenticated, already-accepted-privilege read path; and, at every later
re-verification, that the same value is still present.

**What it explicitly does not establish** — this list is exhaustive and
binding on any future implementation, mirroring ADR 0003's own discipline:

- that the underlying disk/process state is the same one that existed at
  enrollment time (§6 candidate 10; clone/backup copy config identically);
- that no one else with `VM.Config`-class access to *any* guest has copied
  the marker value into a different guest (the marker is config data, not
  a secret protected from anyone who can read the source guest's config
  and write a target guest's config — a materially lower privilege bar
  than destructive/PowerMgmt);
- physical machine or host uniqueness of any kind;
- anything about the workload's *content* (what is running inside it) —
  this is a transport/identity-domain concept for the slot's occupant, not
  content evidence, exactly as ADR 0003 §10 draws the identical line for
  source content;
- authority carried over from a different `source_attestation_epoch` or a
  different `resource_continuity_revision` than the one it was
  established/re-verified under (§15, §21).

## 11. Explicit clone/duplication limitation (tier 1 and tier 2 alike)

Because `description` is ordinary config data copied by clone (§6 candidate
12, confirmed via `qm.adoc`'s own clone documentation covering MAC/`smbios1`
regeneration with no corresponding exclusion for `description`/`tags`), a
clone of an enrolled resource **will** carry the identical marker value into
the new guest. If that clone is then discovered as a new resource (new
`resource_id`, per ADR 0001/0002's own accepted clone-produces-new-slot
rule, since a clone targets a *different* VMID and therefore a *different*
slot), the new resource's config would already contain a value matching a
*different* resource's currently-enrolled marker. This is exactly the
threat model's "proof duplication" case (§22), and this ADR's answer is
identical in spirit to ADR 0003 §11's answer for source-level clones: **a
matching marker value on a second, simultaneously-live resource must never
be treated as proof that either resource is "the" legitimately enrolled
one; it must fail closed** (§22 gives the exact rule; the enrollment
operation's own uniqueness check, §14, is the mechanism that catches this
at accept time for the ordinary case, and §22 covers what happens if it is
detected later).

## 12. Guest-agent/exec-based evidence (tier 2 detail, not designed)

For completeness given the mission's explicit candidate list: QEMU Guest
Agent–mediated evidence (§6 candidate 17) and LXC `pct exec`-mediated
evidence (§6 candidate 18) both require privileges beyond the `VM.Audit`-
only posture ADR 0002 established for discovery, both require the guest to
be running and (for QGA) to have cooperative in-guest software installed
and configured — not a default state — and neither is confirmed against a
primary source in this session to an exact privilege string. Both remain
**UNKNOWN** implementation-time work, not decided or authorized here, and
both would still be Family D (tier 2)'s underlying disk-state limitation
if the evidence they retrieve is itself disk-resident (§11).

## 13. Enrollment target — exact binding

Every enrollment decision (initial or re-enrollment) must bind, atomically,
to the exact incarnation under evaluation:

```text
inventory_source_id
resource_id
exact active binding_id
VMID                                    (redundant provenance only, §14)
locator_generation
exact pre-transition resource_continuity_revision
resource_type
exact source_attestation_epoch          (if the chosen proof, or the
                                          decision context, depends on
                                          source trust-domain continuity
                                          -- tier 1 as specified here does,
                                          §15)
exact current committed discovery/source context needed by the read
  path (source_config_revision, endpoint_id, canonical_transport_locator,
  canonicalization_contract_version, transport_trust_revision)
actor
enrollment decision timestamp
audited reason
exact proof/evidence identity (the marker's own record identity, §17)
```

VMID alone is never sufficient to target an enrollment decision, exactly as
ADR 0004 §9/§18 already require for confirmed removal — the same "redundant
locator provenance only" rule applies identically here.

## 14. Eligibility preconditions for initial enrollment

At minimum, all of the following must hold, re-verified by exact CAS inside
the authoritative write transaction (a pre-check outside that transaction
is never the security boundary, per this repository's established
discipline):

```text
presence == present
lifecycle == active
observational_continuity == consistent
security_continuity == unverified   (or revoked, for re-enrollment, §16)
exact active binding_id open and matching the caller-reviewed value
exact resource_continuity_revision matching the caller-reviewed value
no terminal history for this resource_id
source currently attested (source_attestation_status == attested)
source relationship_gate == clear
exact source_attestation_epoch matching the caller-reviewed value
source current health/freshness fresh under the existing mutation-
  freshness contract (mirroring source_is_fresh_for_future_mutation)
no active discovery owner for the source at commit time
current node relation MAY be unresolved -- resolved current-node status is
  not required for tier 1 (the marker read targets the exact resource's
  config via source-scoped API, not a node-scoped one; see §24 for why
  node trust is a separate question this ADR does not require here)
```

`observational_continuity == consistent` is deliberately required (not
merely `presence == present`): an *ambiguous* current resource
(`uncertain`, ADR 0001's "ambiguous current resource" row) must not become
newly enrollable while its own continuity is itself unresolved — the
operator must first let that ambiguity resolve through ADR 0001's own
existing mechanisms before Hubinet Ops will accept a fresh enrollment
decision against it.

Node trust is explicitly **not** required as an enrollment precondition
here, because tier 1's read path is a source-scoped guest-config read
(identical in shape to ordinary discovery's own config reads), not a
node-mediated operation — see §24 for the full separation and for why a
future node-dependent proof tier would need its own explicit migration
semantics.

## 15. Source-attestation epoch interaction

Tier 1 evidence's read path is a guest-config read over the source's
active endpoint — the identical transport ADR 0003 already governs. This
ADR therefore adopts, as its own conservative decision, the identical
authority-eligibility rule ADR 0003 §20/§26/§29 already fixed as normative
for **any** future Blocker B evidence whose correctness assumption depends
on source trust-domain continuity:

```text
resource trust evidence recorded under source_attestation_epoch N is
  authority-eligible only under exact current epoch N

a source epoch bump N -> N+1 (accepted anchor change or explicit
  revocation, ADR 0003 SS16/SS20) makes any resource trust evidence
  recorded under N no longer authority-eligible

old evidence remains retained, unchanged, for audit -- never deleted

when Blocker B evidence becomes epoch-ineligible, the mandatory default
  (ADR 0003 SS20 "Representation boundary", binding on this ADR exactly as
  it is binding on every future Blocker-B-dependent design) applies:
  security_continuity: trusted -> revoked, with a
  resource_continuity_revision bump, expressed strictly inside ADR 0001's
  existing three-value vocabulary -- never a new canonical value, never a
  separately-computed "effective" value while leaving the stored field
  trusted

a same-anchor source reconfirmation (ADR 0003 SS16, epoch unchanged) never
  invalidates resource trust evidence

a source relationship_gate == mismatch_pending_reattestation blocks every
  new trust-sensitive decision (initial enrollment, re-enrollment, and any
  future read-only re-verification that would otherwise refresh trust
  eligibility) until the operator explicitly resolves the mismatch,
  exactly as it already blocks Class-C confirmed removal (ADR 0004 SS16)
```

This ADR does **not** design a carry-forward procedure across an epoch
bump (ADR 0003 §20 leaves this to Blocker B's own future ADR — this one).
**Decision:** no carry-forward is authorized. An epoch bump always requires
a full new enrollment decision under the new epoch; old-epoch evidence is
never silently re-validated. This is the safest default per ADR 0003's own
guidance and avoids inventing a second, separate epoch-bridging mechanism.

## 16. Resource trust state machine

Exact allowed transitions:

```text
unverified -> trusted     initial enrollment (SS17): new immutable evidence
                          record + new current-state pointer; +1 revision

trusted -> trusted        re-verification with an unchanged, still-matching
                          marker: audit-only, no revision bump (mirrors ADR
                          0003 SS16's same-anchor-reconfirmation rule
                          exactly)

trusted -> revoked        explicit operator revocation, OR an accepted
                          epoch bump invalidating the evidence (SS15), OR a
                          detected marker mismatch/duplication (SS22); +1
                          revision exactly once per accepted decision

revoked -> trusted        re-enrollment: a brand-new evidence record under
                          the exact current epoch/context, identical
                          preconditions to initial enrollment except
                          security_continuity starts at revoked instead of
                          unverified (SS14); +1 revision

revoked -> revoked        a second revocation attempt against an already-
                          revoked resource is a no-op audit event, not a
                          new revision bump (there is nothing left to
                          revoke)
```

`unverified -> revoked` does not exist as a direct transition: a resource
that was never trusted has nothing to revoke; an operator who wants to
permanently foreclose future enrollment of a specific resource does so
through a different, future, explicitly out-of-scope mechanism (not
designed here) — `unverified` already carries no destructive authority, so
there is no security gap in leaving this transition undefined.

Re-enrollment (`revoked -> trusted`) always creates a **new** immutable
evidence record (§17) — it never resurrects or reactivates the previous,
now-superseded evidence record, exactly mirroring ADR 0003's own rule that
a re-attestation is a new event, never a mutation of a prior one.

## 17. Immutable evidence vs. mutable current-state pointer

Mirroring both ADR 0003's `source_attestation_state`/`source_attestation_
events` split and ADR 0004's `resource_absence_pointers` (mutable-current)
vs. evidence-tables (immutable) split, this ADR requires the identical
structural separation for resource continuity:

- **Current resource trust state** — one row per resource, holding the
  *current* `security_continuity` value, the *current* enrollment
  revision/generation pointer, and a reference to the *current* accepted
  evidence record. Mutable in the sense that its pointer/value changes
  across enrollment/re-enrollment/revocation events, but every value it
  ever held is independently retained via the immutable evidence records
  below — never itself the sole record of history.
- **Immutable enrollment/continuity evidence records** — one retained,
  never-updated, never-deleted row per accepted (or rejected/mismatched/
  unavailable/malformed) enrollment attempt, carrying the exact §13
  binding, the exact marker value asserted and observed, the exact outcome
  (accepted/mismatch/unavailable/malformed/revoked/epoch-ineligible), and
  full actor/timestamp/reason provenance. Never overwritten; a later
  attempt is always a **new** record, exactly like ADR 0003's `source_
  attestation_events` and ADR 0004's two Class-C evidence tables.

No duplicate authoritative copy of `security_continuity` may exist outside
the current-state row; audit records are evidence, never a second
authority for the current value, mirroring this repository's existing
"audit records are not current-state authority" discipline (already stated
verbatim in `0.5-inventory-model.md` for the analogous node-attestation
design and reused here for resources).

## 18. Remote evidence read pattern

Because tier 1 requires an actual config read against the resource's
source (remote, untrusted-timing I/O), this ADR requires WAVE B1 to follow
ADR 0003 §19a's exact three-phase discipline, literally, with no
implementation latitude to shortcut it:

```text
PHASE 1 -- short, read-only DB transaction:
  capture the exact expected context from SS13/SS14/SS15 as one consistent
  snapshot (never a held write transaction)

PHASE 2 -- trusted evidence-reader call, entirely OUTSIDE any DB write
  transaction:
  read the resource's live config-resident marker value over the source's
  already-validated transport

PHASE 3 -- BEGIN IMMEDIATE authoritative write transaction:
  re-validate every field captured in phase 1 by exact CAS
  (source_config_revision, endpoint_id, canonical_transport_locator,
  canonicalization_contract_version, transport_trust_revision,
  source_attestation_epoch, relationship_gate, resource_id, binding_id,
  locator_generation, resource_continuity_revision, presence, lifecycle,
  observational_continuity)
  only if every field still matches: accept/reject the evidence and commit
  atomically; any mismatch classifies the attempt as stale and accepts
  nothing, exactly like a discovery run or an attestation attempt with a
  mismatched expected context
```

A pre-read outside this pattern is never the security boundary. This
applies identically to initial enrollment and to re-enrollment; it does
**not** apply to a `trusted -> trusted` audit-only reconfirmation with an
unchanged marker in the strict sense of requiring a full remote read every
single time a resource's trust is merely *consulted* (reading current
state is just a DB read) — it applies whenever a **new** remote marker
read is performed to establish or refresh eligibility.

## 19. Trusted evidence-reader boundary

A typed conceptual boundary, directly analogous to ADR 0003's
`SourceAttestationEvidenceReader`:

```text
ResourceContinuityEvidenceReader (conceptual Protocol, not implemented here)

read(*, inventory_source_id, resource_id, endpoint_id,
     canonical_transport_locator, enrolled_marker_value | None)
  -> ResourceContinuityEvidenceReading

ResourceContinuityEvidenceReading (conceptual, frozen):
  outcome: OBSERVED | UNAVAILABLE | MALFORMED
  observed_marker_value: str | None   (only for OBSERVED)
```

No authority method may ever accept a raw caller-supplied
`marker_verified=True` boolean or any other unverified evidence value —
the trusted reader is the sole boundary through which live marker content
enters the authority transaction, exactly as ADR 0003 §29 negative witness
12 already forbids for source-level tier claims. WAVE B1 may implement
this boundary with fake/deterministic readers only, exactly as WAVE C1 did
for `SourceAttestationEvidenceReader` — "**no production PVE network/TLS
reader implementation exists**" is an accepted, honest, dormant limitation,
not a defect, and this ADR authorizes the identical posture for resource
continuity: the typed boundary is real, production acquisition is not,
and wiring a real reader remains gated on a separate future review exactly
like WAVE C1's own explicit statement about its own reader.

**Malformed/unavailable evidence** must never be treated as an implicit
match or an implicit mismatch (identical rule to ADR 0003 §18); it blocks
only the specific action that requested it and leaves current trust state
completely unchanged, recorded as its own audited outcome.

## 20. `resource_continuity_revision` semantics

Reusing the existing token exactly (no second resource-level security
token is introduced, per the mission's explicit instruction):

```text
initial trusted enrollment:                       +1 exactly once
trust revocation (any accepted cause):             +1 exactly once
accepted re-enrollment of a revoked resource:      +1 exactly once
same-marker audit-only reconfirmation:             0 (unchanged, mirrors
                                                    ADR 0003's same-anchor
                                                    reconfirmation rule)
ordinary observational facts alone (rename,
  runtime status, harmless config change,
  detail-read errors) that cause no already-
  accepted security transition:                    0 (unchanged)
```

One atomic accepted security decision that touches several fields (e.g.
enrollment simultaneously setting `security_continuity=trusted`, the
current evidence pointer, and the enrollment generation) increments the
token exactly once, never once per field — identical to every other
multi-field security decision ADR 0001/0003/0004 already govern this way.

## 21. Missing/uncertain interaction (ADR 0001 preserved)

A resource that enters `missing`/`quarantined` (ADR 0001's own accepted
transition) is, by ADR 0001's own rule, moved toward `uncertain`
observational continuity and, if it was previously `trusted`, immediately
`revoked` — this ADR changes nothing here; it is already accepted ADR 0001
behavior and this ADR's own evidence model does not need to re-derive it.
What this ADR must additionally close, because ADR 0001 leaves the
*re-entry* side open for the trust axis specifically, is: **`missing ->
present` (even with byte-identical observed facts) must never, by itself,
restore a previously `trusted` security_continuity.** ADR 0001 already sets
`security_continuity` on re-appearance to `revoked` (if it was ever
trusted) or `unverified` (if it never was) — this ADR's contribution is
that restoring `trusted` after such a gap **requires a full new enrollment
decision** (§14, §16 `revoked -> trusted`) under the exact current context;
it is never restored implicitly, never by a matching marker read
opportunistically during ordinary discovery, and never by the *same*
evidence record that was valid before the gap. The marker read that
establishes the new `trusted` state after a gap must itself follow §18's
full three-phase pattern against the resource's *current* post-gap state.

## 22. Proof duplication / replay — fail closed

| Scenario | Outcome |
| --- | --- |
| identical marker value observed on two simultaneously-live resources of the same source | fail closed: the *second* enrollment attempt to accept that marker value is rejected by an explicit uniqueness check inside the authority transaction (WAVE B1 must enforce global-per-backend or at minimum per-source uniqueness of currently-*accepted* marker values — exact scope is an implementation detail, but the check itself is normative and required); the *first*, already-accepted resource's trust is not automatically revoked merely because a later collision was observed, but the collision itself must be durably, immutably recorded as its own audited outcome so an operator can investigate |
| marker later observed on a resource's successor (direct replacement, ADR 0001/0002) | irrelevant by construction: the successor starts `unverified` with no evidence linkage of any kind (ADR 0001's existing "no inherited policy" rule already forecloses this); if the successor's *live config* happens to still contain the old marker (because it was cloned from the same base, or the same disk was reused), that is exactly §11's clone-limitation case — the successor must still require its own fresh enrollment; the mere presence of a matching marker value on it is never itself sufficient (§10) |
| marker replayed on a resource in another source | rejected: SS13's binding ties every accepted evidence record to an exact `inventory_source_id`; a cross-source match has no bearing on any single-source uniqueness check and is not itself evidence of anything, since sources are independently namespaced (ADR 0001) |
| marker observed after the original resource became terminal (confirmed removed or replaced) | the terminal resource's evidence remains retained (§17) but is never authority-eligible for any decision again (it is terminal); if a *new* incarnation at the same slot happens to carry the old marker (clone/restore reusing the same disk), that new incarnation still requires its own fresh enrollment under its own new `resource_id` — old evidence never transfers (ADR 0001 §3 item 6/ADR 0004 §25) |
| marker observed after a `source_attestation_epoch` bump | the pre-bump evidence record remains retained but is no longer authority-eligible (§15); any *new* read after the bump must be evaluated fresh, under the new epoch, following §18 in full |
| marker observed after an accepted marker rotation/revocation (§16 `trusted -> revoked` then re-enrollment) | the old marker value is explicitly retired at revocation time (WAVE B1 must record the old value as historically-associated-but-no-longer-current, never silently overwritten in place — mirrors the immutable-evidence-record discipline of §17); a live guest still showing the *old* value after rotation is evidence the rotation instruction (an out-of-band operator action, §9) has not yet been carried out, not evidence of anything security-relevant by itself, and blocks *new* enrollment attempts that expect the *new* value until the operator actually updates the guest |

**Default, restated because it is load-bearing:** uniqueness of a tier-1
marker can be *operationally* enforced by the backend (reject a second
acceptance of an already-accepted value) but can **never** be
*cryptographically* guaranteed against a determined party with config-write
access to some guest. This ADR does not claim otherwise. WAVE B1's
implementation-status description must state this limitation explicitly,
exactly as WAVE C1's own status section states the analogous limitation
for source attestation.

## 23. Direct replacement / confirmed removal interaction

No new rule is introduced here — both are already fully specified by ADR
0001 and ADR 0004 respectively, and this ADR's evidence records simply
participate as retained history:

```text
direct replacement (ADR 0001/0002):
  old resource:  terminal; trusted -> revoked (already ADR 0001); its own
                 continuity evidence records remain retained, permanently
                 authority-ineligible
  successor:     new resource_id, new locator_generation, new binding_id;
                 security_continuity = unverified; zero evidence linkage
                 of any kind; requires its own fresh enrollment (SS14)

confirmed removal (ADR 0004):
  old resource:  terminal, never reopens; trusted -> revoked already
                 applied by ADR 0004 SS19 step 10 if it was trusted;
                 continuity evidence records remain retained, permanently
                 authority-ineligible
  later same-slot occupant: always a brand-new resource_id/generation/
                 binding (ADR 0004 SS25); security_continuity = unverified;
                 zero inherited evidence; requires its own fresh enrollment
```

No proof of any kind ever transfers automatically across a `resource_id`
boundary, at any tier, for any reason — this is the same invariant ADR
0001 already states as "false continuity must never transfer destructive
authority," and this ADR adds no exception to it.

## 24. Node trust separation

Explicit, non-negotiable, mirroring ADR 0001's own node-trust design and
ADR 0003's identical discipline for source vs. resource trust:

```text
trusted resource != trusted node
trusted node != trusted resource
```

Tier 1 (§9) does not require, consume, or depend on node trust at all — its
read path is a source-scoped guest-config read, not a node-mediated
operation, so this ADR's minimal supported path introduces no coupling
between the two axes. A resource remaining node-independent through node
migration (ADR 0001 scenario 2: "Ten sam `resource_id` i locator; aktualizacja
node relation") does **not** by itself invalidate tier-1 trust, because
tier-1 evidence is not node-dependent.

If a future tier-2 or tier-3 mechanism (§11, §12) turns out to require
node-mediated evidence collection (e.g. any QGA/`pct exec` path routed
through a specific host), that future ADR must define its own explicit
node-migration/re-attestation semantics at that time — this ADR does not
pre-authorize assuming node-independence for a mechanism it does not
design. A future mutation, regardless of resource trust tier, still
independently requires the accepted node/hostd trust route (ADR 0001's
host-routing invariant) — resource trust and node trust remain two
separate, both-required gates for any future mutation, never substitutes
for each other.

## 25. Publication and revision semantics

No new published concept is introduced. Existing accepted fields are
reused exactly:

- `security_continuity`, `resource_continuity_revision`, `state_level`,
  `policy_applicability`, and effective capabilities remain exactly as
  ADR 0001/`0.5-inventory-model.md` already define them; this ADR adds no
  new published enum value or field shape.
- every accepted trust transition (`unverified -> trusted`, `trusted ->
  revoked`, `revoked -> trusted`) advances `resource_continuity_revision`
  exactly once (§20) and, because it changes published resource state,
  advances `published_state_revision` exactly once in the same atomic
  transaction, following the identical pattern ADR 0002/0003/0004 already
  use for every other security-relevant transition;
- a trust-only transition is never itself an inventory-reconciliation
  event, so it does **not** advance `inventory_revision` unless the same
  atomic decision also happens to be part of an accepted reconciliation
  commit (it structurally never is, per this ADR's own design — enrollment
  is its own standalone authority transaction, exactly like ADR 0004's
  Class-C decision, never folded into `finalize_successful_discovery_run`);
- Home Assistant remains presentation-only; no writable HA transport is
  introduced or implied by this ADR (§2, §27).

## 26. Policy boundary preserved

Unchanged, restated because it remains load-bearing: **`security_continuity
= trusted` is necessary, never sufficient, for any future mutation.**

```text
security_continuity != trusted  =>  effective destructive policy = false
                                 =>  maintenance permission = none
                                 =>  effective destructive capabilities = none

security_continuity == trusted  =>  still requires, independently:
                                     stored policy allowing the operation
                                     ∩ policy applicability
                                     ∩ backend capability
                                     ∩ exact resource_id/binding_id/
                                       locator_generation/
                                       resource_continuity_revision
                                     ∩ sufficiently fresh committed inventory
                                     ∩ trusted node/hostd route
                                     ∩ every operation-specific precondition
                                     (ADR 0001's existing intersection,
                                     unchanged, cited verbatim)
```

Retained policy is never applicable policy (ADR 0001/`0.5-inventory-
model.md`, unchanged). B0/B1 implement none of the policy/plan/job/lock
authority itself; this ADR only closes the evidence question one of those
future gates depends on.

## 27. R0 boundary

R0 remains read-only runtime activation, exactly as ADR 0004 §31's own
closing line already established. This ADR does not enlarge that boundary
for Blocker B:

```text
even after WAVE B1 implements the durable dormant trust/enrollment
authority described here, R0 activation must not include:
  - production enrollment automation of any kind;
  - HA writable controls;
  - typed host mutation of any kind;
  - job/policy/approval execution;
  - endpoint activation/failover;
  - automatic trust grant triggered by ordinary discovery.
```

Because tier 1's evidence-write side (setting the marker value into a
guest) is explicitly an out-of-band operator action (§9), not a Hubinet
Ops mutation, B1's own dormant implementation does not, by itself, require
crossing the mutation-authority gate to exist as a typed authority
operation with a fake reader — exactly as WAVE C1's attestation authority
and WAVE A1's confirmed-removal authority both already exist, fully typed
and tested, entirely dormant, without any production mutation wiring.
Production acquisition of the marker's *live* value (the read side) is
ordinary read-only PVE API access, identical in privilege class to
existing discovery — but remains gated on R0's own separately-reviewed
read-only runtime activation, never activated by this ADR's acceptance
alone.

## 28. Events classification table

For every event, whether `resource_id` remains, whether `security_
continuity` remains `trusted`/is revoked/becomes temporarily ineligible,
whether `resource_continuity_revision` increments, whether evidence is
retained, and whether re-enrollment is required:

| Event | `resource_id` | `security_continuity` | Temp. ineligible only? | Revision +1? | Evidence retained? | Re-enrollment required to restore `trusted`? |
| --- | --- | --- | --- | --- | --- | --- |
| ordinary successful discovery, no facts changed | same | unchanged | no | no | n/a | no |
| rename | same | unchanged | no | no | n/a | no |
| runtime status change | same | unchanged | no | no | n/a | no |
| harmless config fact change (not the marker field) | same | unchanged | no | no | n/a | no |
| node migration | same | unchanged (§24) | no | no | n/a | no |
| current node temporarily unavailable | same | unchanged | no | no | n/a | no |
| detail read error | same | unchanged | no | no | n/a | no |
| source stale (time expiry) | same | unchanged; new *decisions* blocked by freshness CAS (§14/§18) | yes, for new decisions only | no | n/a | no (once fresh again) |
| source unavailable | same | unchanged; new decisions blocked | yes | no | n/a | no (once available again) |
| partial/configuration_error run | same | unchanged; new decisions blocked (no complete boundary) | yes | no | n/a | no |
| source credential/config revision change | same | unchanged; new decisions require re-CAS | yes, for new decisions | no by itself | n/a | no |
| active endpoint/context change | same | unchanged; new decisions require re-CAS | yes | no by itself | n/a | no |
| canonicalization migration | same | unchanged; new decisions require re-CAS | yes | no by itself | n/a | no |
| transport trust revision change | same | unchanged; new decisions require re-CAS | yes | no by itself | n/a | no |
| source-attestation same-anchor reconfirmation | same | unchanged (§15) | no | no | n/a | no |
| source-attestation epoch bump | same | `trusted -> revoked` if evidence was epoch-bound (§15) | no — permanent for that evidence | **yes**, once | yes | **yes** |
| source relationship mismatch (`mismatch_pending_reattestation`) | same | unchanged; all new trust decisions blocked | yes | no | n/a | no (once resolved) |
| resource becomes `missing` | same | `trusted -> revoked` (already ADR 0001) | no — accepted revocation | yes (ADR 0001's own rule) | yes | yes |
| resource becomes observationally `uncertain` (still present) | same | `trusted -> revoked` if was trusted (ADR 0001) | no | yes | yes | yes |
| resource reappears after `missing`, identical facts | same | remains `revoked`/`unverified` (§21); never auto-restored | no | ADR 0001's own re-entry revision rule applies | yes | **yes** |
| direct replacement | old: terminal; new: fresh `resource_id` | old: `trusted -> revoked`; new: `unverified` | no | old: yes (ADR 0001); new: n/a (starts unverified) | yes (old) | yes (new resource) |
| confirmed removal | terminal, never reopens | `trusted -> revoked` already applied (ADR 0004) | no | yes (ADR 0004) | yes | n/a (terminal) |
| same VMID later reused | new `resource_id` (ADR 0001/0004) | new resource starts `unverified` | no | n/a (new resource) | old evidence retained under old `resource_id` | yes |
| proof read unavailable | same | unchanged (§19) | yes, for that one attempt | no | yes (audited outcome) | no |
| proof malformed | same | unchanged (§19) | yes, for that one attempt | no | yes (audited outcome) | no |
| proof mismatch (wrong/no marker observed) | same | unchanged, unless this is a revocation-eligible policy decision the operator explicitly makes | yes, blocks that attempt | no by itself | yes (audited outcome) | no automatically; operator may choose to explicitly revoke |
| duplicate proof on another resource | both unaffected by the mere read; the *second acceptance attempt* is rejected (§22) | first resource: unaffected by the collision alone; collision itself audited | no | no by itself | yes (rejection audited) | no |
| proof/anchor rotation (accepted) | same | `trusted -> revoked` then `revoked -> trusted` under new marker (two accepted decisions) | no | yes twice (revoke, then re-enroll) | yes, both old and new | yes (by design — rotation *is* re-enrollment) |
| clone | new resource (new VMID/slot) | starts `unverified`, regardless of any copied marker (§11) | no | n/a | n/a (new resource has none yet) | yes |
| snapshot rollback (same resource) | same | unchanged by the rollback event itself; requires operator judgment whether prior trust is still appropriate — this ADR does not mandate automatic revocation on rollback, since rollback of an already-enrolled workload to an earlier *legitimate* snapshot of the *same* workload is not, by itself, one of this ADR's threat classes; **however** any concurrent marker re-verification failure (§18) is handled by the ordinary mismatch path above | conditionally, only via ordinary re-verification | no by itself | n/a | n/a |
| backup restore | same reasoning as snapshot rollback if restoring the *same* resource's own backup; if restoring to a *different*/*new* resource, that new resource starts `unverified` (no different from clone) | see above | conditionally | no by itself (same-resource case) | n/a | only if marker re-verification fails |
| external/manual destroy + recreate (same VMID) | ADR 0001's own accepted ambiguity rule applies first (may retain same `resource_id` for read-only continuity, `uncertain`/`quarantined`) — this ADR requires that even if `resource_id` is retained, `trusted` is **never** retained through it (§21) | `revoked` (if it was ever trusted) or remains `unverified` | no | yes (ADR 0001's own gap-return revision rule) | yes | yes |
| backend restart | same | unchanged, durable | no | no | n/a | no |
| database restore (backend's own DB) | same | unchanged, as durably recorded at restore point; any decisions made against a stale restored context are subject to the exact same CAS as any other operation (§18) | no | no by itself | n/a | no automatically |

## 29. Adversarial matrix

`allowed` means the enrollment/re-verification decision proceeds once all
other stated preconditions independently hold; `rejected` means the
scenario alone blocks it.

| # | Scenario | Allowed/rejected | Resulting `security_continuity` | Identity effect | Binding effect | Revision effect | Evidence retained | Re-enrollment required |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | Same VMID/name/config, never enrolled | rejected (no proof offered) | `unverified` | none | none | none | n/a | — |
| B | Operator assertion only, no marker check | rejected | `unverified` unchanged | none | none | none | audited rejection | — |
| C | Stock digest match only | rejected | unchanged | none | none | none | n/a | — |
| D | Same tag/description text coincidentally present, not a Hubinet-issued marker | rejected (fails the exact-value/format check, §9) | unchanged | none | none | none | audited rejection | — |
| E | Valid supported tier-1 proof, all CAS holds | allowed | `unverified -> trusted` | resource_id unchanged | binding unchanged | +1 once | yes | — |
| F | Wrong proof value observed | rejected | unchanged | none | none | none | audited mismatch | — |
| G | Malformed proof (unreadable/corrupt config) | rejected | unchanged | none | none | none | audited malformed | — |
| H | Proof read unavailable (transient failure) | rejected | unchanged | none | none | none | audited unavailable | — |
| I | Stale resource_continuity_revision | rejected | unchanged | none | none | none | audited stale_cas | operator must re-review |
| J | Stale binding_id | rejected | unchanged | none | none | none | audited stale_cas | operator must re-review |
| K | Stale locator_generation | rejected | unchanged | none | none | none | audited stale_cas | operator must re-review |
| L | Newer successful discovery committed before decision | rejected | unchanged | none | none | none | audited stale_cas | operator must re-review |
| M | Active discovery owner at commit time | rejected/retry | unchanged | none | none | none | none (pre-check, no attempt recorded) | retry after release |
| N | Source stale (time expiry) | rejected | unchanged | none | none | none | none | retry once fresh |
| O | Source unavailable | rejected | unchanged | none | none | none | none | retry once available |
| P | Source config change mid-decision | rejected | unchanged | none | none | none | audited stale_cas | operator must re-review |
| Q | Transport trust change mid-decision | rejected | unchanged | none | none | none | audited stale_cas | operator must re-review |
| R | Source epoch bump mid-decision or after prior enrollment | rejected (mid-decision) / evidence becomes ineligible (after) | unchanged (mid-decision) / `trusted -> revoked` (after, §15) | none | none | +1 (only the after-the-fact revocation case) | yes | yes (after-the-fact case) |
| S | Source `mismatch_pending_reattestation` gate | rejected | unchanged | none | none | none | none | retry once resolved |
| T | Same-anchor source reconfirmation | allowed (no effect on resource trust) | unchanged | none | none | none | n/a | no |
| U | Resource currently `missing` | rejected (fails §14 `presence==present` precondition) | already `revoked`/`unverified` per ADR 0001 | none | none | none (already applied at the missing transition) | yes | yes, once present again |
| V | Resource `missing` then `present` again, identical facts | rejected as automatic restoration (§21); fresh enrollment allowed | remains `revoked`/`unverified` until fresh enrollment | none | none | none automatically | yes | yes |
| W | Direct replacement occurred | rejected (old resource is terminal) | old: `revoked`; new: `unverified` | old terminal, new resource_id | old binding closed, new open | old already +1 (ADR 0001) | yes | yes, for the new resource |
| X | Confirmed removal occurred | rejected (resource is terminal) | already `revoked` (ADR 0004) | terminal, never reopens | closed | already +1 (ADR 0004) | yes | n/a |
| Y | Terminal same-slot reuse (any terminal cause) | new resource requires its own enrollment | new: `unverified` | new resource_id | new binding | new resource starts fresh | old evidence retained separately | yes |
| Z | Proof duplicated on second resource (same source) | second acceptance rejected (§22) | first resource unaffected; second remains `unverified` | none | none | none | collision audited | second resource still needs its own valid proof |
| AA | Proof replayed on a successor | rejected — successor has no inherited evidence linkage (§23) | successor: `unverified` | none | none | none | n/a | yes |
| AB | Proof replayed in another source | rejected — cross-source binding never matches (§13, §22) | unaffected in either source | none | none | none | n/a | yes, independently, in the second source |
| AC | Clone | new resource requires its own enrollment even if marker copied (§11) | new: `unverified` | new resource_id | new binding | n/a | n/a (new resource has none) | yes |
| AD | Snapshot rollback (same resource) | allowed to remain trusted unless re-verification fails (§28) | conditionally unchanged | resource_id unchanged | binding unchanged | no by itself | n/a | only if marker mismatch found |
| AE | Backup restore (same resource's own backup) | same as AD | conditionally unchanged | unchanged | unchanged | no by itself | n/a | only if marker mismatch found |
| AF | Node migration | allowed, no effect (§24) | unchanged | none | none | none | n/a | no |
| AG | Backend restart | unaffected, durable | unchanged | none | none | none | n/a | no |
| AH | Crash before authority commit | no partial state; entire decision rolled back | unchanged (as before the attempt) | none | none | none | none (nothing committed) | operator must retry the entire decision |
| AI | Two operators/enrollment decisions racing | exactly one wins the CAS; the other is rejected as stale | winner: `trusted`; loser: unchanged | none | none | +1 (winner only) | yes (winner); audited stale_cas (loser) | loser must re-review |
| AJ | Accepted proof rotation | allowed as two accepted decisions: revoke then re-enroll | `trusted -> revoked -> trusted` | none | none | +1 twice | both old and new evidence retained | by design (rotation is re-enrollment) |
| AK | Failed proof rotation attempt | rejected; prior trust state untouched | unchanged (still whatever it was before the attempt) | none | none | none | audited failed attempt | operator must retry |
| AL | Old proof observed after rotation | not itself evidence of anything security-relevant; blocks new-marker enrollment attempts expecting the new value (§22) | unchanged by the mere observation | none | none | none | n/a | operator must actually update the guest |

## 30. Open questions closed (normative)

1. What exact claim does `trusted` mean? **That an operator personally
   set a specific, backend-issued, high-entropy value into the exact
   guest's `description` field out-of-band, and Hubinet Ops independently
   read that exact value back over an already-accepted read privilege, at
   the moment of the (re-)enrollment decision — nothing about physical
   continuity beyond that (§9, §10).**
2. What exact proof grants it? **Tier-1 marker evidence per §9, bound
   exactly per §13, under the exact eligibility of §14.**
3. Is stock-PVE-only proof sufficient? **No (§7, permanently rejected).**
4. Is operator assertion alone sufficient? **No (§8) — always paired with
   a re-checkable marker.**
5. Does proof work for both QEMU and LXC? **Yes — `description` is
   identically defined, same `maxLength`, same semantics, confirmed via
   primary source for both (§6 candidate 12).**
6. What is the trusted evidence-reading boundary?
   **`ResourceContinuityEvidenceReader` (§19), conceptual only in this
   ADR.**
7. What exact object/incarnation is enrolled? **§13's exact binding
   tuple.**
8. What source epoch/context is evidence tied to? **Exact current
   `source_attestation_epoch` and full source context at read time (§15).**
9. What invalidates trust? **Revocation, epoch bump, `missing` transition,
   direct replacement/confirmed removal (§16, §21, §23).**
10. What merely makes trust temporarily ineligible, if anything? **New
    *decisions* (not the current trust state itself) are blocked by source
    staleness/unavailability/mismatch-gate/active-run — the already-
    granted `trusted` value is unaffected until an accepted revocation
    event actually occurs (§28).**
11. Can `missing -> present` preserve/restore trust? **No, never
    automatically (§21).**
12. What happens on source epoch bump? **Existing trust tied to the old
    epoch is revoked with a revision bump; retained for audit; requires
    fresh re-enrollment (§15).**
13. What happens on source mismatch? **All new trust decisions blocked
    until resolved; already-granted trust is unaffected by the mismatch
    alone (§15, §28).**
14. What happens on node migration? **Nothing — tier 1 is node-independent
    (§24).**
15. What happens on clone? **New resource, fresh enrollment required, even
    if the marker was copied (§11, §23).**
16. What happens on snapshot rollback? **No automatic revocation; ordinary
    re-verification applies (§28).**
17. What happens on backup restore? **Same as rollback for the same
    resource; a *new* resource from restore is treated like a clone
    (§28).**
18. What happens on same-VMID destroy/recreate? **ADR 0001's own gap rule
    applies to identity; this ADR additionally guarantees trust is never
    retained through it (§21).**
19. What happens on proof duplication? **Second acceptance rejected,
    fail-closed, collision audited (§22).**
20. What happens on proof replay? **Rejected across successors and across
    sources; never itself evidence (§22, §23).**
21. How does proof rotate? **As two accepted decisions: revoke, then
    re-enroll under the new value (§16, §22).**
22. How does `revoked -> trusted` work? **A brand-new evidence record
    under current context, identical preconditions to initial enrollment
    (§14, §16).**
23. Exactly when does `resource_continuity_revision` increment? **§20 —
    once per accepted security decision, never per field, never for a
    same-marker reconfirmation.**
24. What records are immutable? **Enrollment/continuity evidence records
    (§17).**
25. What state is mutable-current? **The current trust-state pointer row
    only (§17).**
26. What evidence is retained after terminal state? **All of it,
    permanently, but never again authority-eligible (§17, §23).**
27. Does trust ever transfer to a successor? **No, never (§23) — MUST be
    NO, confirmed.**
28. Does source attestation imply resource trust? **No (§3 item 7, §15
    only ever *gates*, never *grants*) — MUST be NO, confirmed.**
29. Does node trust imply resource trust? **No (§24) — MUST be NO,
    confirmed.**
30. Can discovery grant trust? **No (§3 item 9) — MUST be NO, confirmed.**
31. Can R0 grant/modify trust automatically? **No (§27) — MUST be NO,
    confirmed.**
32. **Can B1 be implemented without inventing any additional security-
    policy decisions? Yes** — §13 through §29 collectively fix every
    binding, precondition, epoch rule, state transition, revision rule,
    retention rule, and adversarial classification a durable dormant
    implementation needs; the only items left open (§31) are implementation-
    detail choices (exact schema names, exact uniqueness-check scope) or
    explicitly out-of-scope future tiers (2/3), never a load-bearing
    security-policy gap in the tier-1 path this ADR selects.

## 31. What remains unresolved after this ADR

Limited strictly to items genuinely outside the tier-1 minimal path this
ADR closes:

1. Exact schema (table/column/enum names) for the current-state row and
   the immutable evidence records — an implementation-package choice, not
   decided here, mirroring ADR 0003 §30 item 3 and ADR 0004 §30 item 1's
   identical posture.
2. Exact scope of the uniqueness check required by §22 (per-source vs.
   per-backend) — both are defensible; WAVE B1 must pick one and document
   it, but either satisfies this ADR's fail-closed requirement.
3. Tier 2 (guest-resident cryptographic evidence) design, including the
   exact QGA/`pct exec` privilege contract (§12) — explicitly deferred to
   a future ADR, exactly as ADR 0003 deferred its own tier 2/3 in part.
4. Tier 3 (genuinely clone-resistant mechanism) — not designed, not
   available from stock Proxmox VE, exactly parallel to ADR 0003's tier 3
   gap.
5. The exact out-of-band operator workflow/UX for setting the marker value
   into a guest — a product/UX decision, not a security-policy decision;
   this ADR requires only that it happen out-of-band, never as a Hubinet
   Ops mutation, in the minimal tier-1 path.
6. Long-term retention/purge policy for superseded evidence records —
   explicitly out of scope, mirroring ADR 0003 §30 item 6 and ADR 0004
   §27's identical posture.
7. Whether a future mutation-authority gate might eventually let Hubinet
   Ops itself provision the tier-1 marker (closing the current out-of-band
   requirement) — not decided here; would require its own separate review
   once mutation authority exists at all (§27).

No genuine contradiction with ADR 0001, ADR 0002, ADR 0003, or ADR 0004 was
found while drafting this ADR. Every normative rule above is either a
direct restatement/reuse of an already-accepted invariant or vocabulary
(§3), or a new, narrower rule scoped strictly to the new continuity-
evidence concept this ADR itself introduces.

## 32. Implementation consequences for WAVE B1 (not implemented here)

A future, separately reviewed and separately accepted implementation
package would need to add, at minimum:

- an authority schema version bump, most likely `v5 -> v6` (clean break,
  no automatic migration authorized, exactly matching the v4→v5 and
  v3→v4 precedent; a v5 database must be rejected fail-closed by v6 code
  exactly as v1-v4 already are);
- one durable current resource-trust-state row per resource (mutable
  current pointer, §17);
- one immutable, retained, delete-blocked enrollment/continuity-evidence
  table (§17), structurally separate from the current-state row, mirroring
  `source_attestation_events`'s and ADR 0004's evidence tables' exact
  immutability/no-delete/no-update trigger discipline;
- an explicit, operator-driven `InventoryAuthority` enrollment operation
  (initial enrollment) and a re-enrollment operation, both following §18's
  three-phase pattern literally, with the exact §13/§14 CAS discipline;
- an explicit, operator-driven revocation operation (no remote read
  required, mirroring `revoke_source_attestation`'s pure-local design);
- the `ResourceContinuityEvidenceReader` typed boundary (§19), tested with
  fake readers only, no production PVE network/TLS reader implementation
  in this package;
- the §15 epoch-eligibility coupling to `source_attestation_epoch`,
  including the mandatory `trusted -> revoked` default representation
  reused verbatim from ADR 0003 §20's "Representation boundary" (never a
  new canonical `security_continuity` value);
- the §22 uniqueness check (schema trigger and/or transaction-level CAS,
  implementation's choice per §31 item 2);
- explicit tests proving the full §28 events table and the full §29
  adversarial matrix, plus restart/immutability/retention regression tests
  mirroring the exact discipline WAVE C1 and WAVE A1 already established;
- a descriptive-only update to `docs/architecture/0.5-implementation-
  status.md` once WAVE B1 actually lands, following the same "architecture
  decision" vs. "implementation" distinction this status document already
  applies to WAVE A0/A1 and WAVE C0/C1.

WAVE B0 (this ADR) implements none of the above; it only records, and
awaits the operator's decision to accept, this architecture as normative.
Everything in this section remains WAVE B1's future implementation work,
exactly as ADR 0003 §31 and ADR 0004 §32 each established the same posture
for their own next packages.
