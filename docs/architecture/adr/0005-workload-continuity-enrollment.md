# ADR 0005: workload continuity enrollment and trust

Status: **PROPOSED**

This ADR is not yet accepted architecture. It does not authorize any schema,
persistence, or runtime implementation by itself. It does not amend ADR 0001,
ADR 0002, ADR 0003, or ADR 0004; where it depends on their invariants it cites
them and adds a new, narrower normative layer on top, exactly as ADR 0003 and
ADR 0004 each added their own layer without changing the others.

**Corrective revision note.** An earlier draft of this ADR selected a
backend-issued marker stored in ordinary PVE guest configuration as a
"tier 1" mechanism sufficient to grant `security_continuity=trusted`. An
independent architecture review found that this selection does not survive
ADR 0001's own accepted invisible same-slot destroy/recreate limitation (one
P1) and five further internal/factual inconsistencies (P2s). This revision
corrects the central decision: **no mechanism composed solely of ordinary,
copyable PVE/guest/config state — at any entropy — can be sufficient to
grant or sustain `security_continuity=trusted`** under ADR 0001's accepted
threat model. This ADR now closes the *research question* ("can stock PVE
read-only evidence provide generic persistent workload security continuity?"
— no) while leaving Blocker B itself **open** for mutation, pending a future,
separately reviewed, stronger continuity-proof mechanism. All of §9 onward
reflects this corrected decision.

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
accepted), has had **no architecture work of any kind** before this ADR.

Blocker B is the highest-stakes of the three: source attestation (Blocker C)
only gates *which trust domain* a source belongs to, and confirmed removal
(Blocker A) only gates a terminal *closure* decision. `security_continuity=
trusted`, by contrast, is the one accepted precondition standing between a
resource and **destructive mutation authority** over a running or existing
workload. Getting this wrong in either direction is asymmetric: too weak, and
an attacker or an ordinary accidental collision authorizes destructive action
against the wrong incarnation; too strict, and the whole 0.5 mutation model
never has a legitimate mutation target at all.

This corrected ADR's job is narrower than originally framed: it audits every
realistic evidence candidate available under the current stock-PVE/read-only
baseline against ADR 0001's own accepted threat model, and reaches an honest
conclusion. That conclusion is negative for the current baseline: **no
candidate evaluated here is sufficient**, and this ADR does not force a false
solution merely because implementation would be easier with one. It defines
the exact negative boundary R0 must respect, and the exact minimum properties
a *future* stronger mechanism would have to satisfy before Blocker B can
close for mutation.

## 2. Scope and non-goals

In scope: the concept, terminology, threat model, evidence audit, and exact
normative negative-boundary semantics of workload/resource continuity under
the current stock-PVE/read-only baseline — which evidence candidates fail to
qualify for `security_continuity: unverified -> trusted`, why each fails
against ADR 0001's threat model, what an administrative correlation marker
may still be used for (audit/UX only, never trust), and the minimum
properties a future stronger mechanism must satisfy before a later ADR may
re-open the positive `-> trusted` path.

Explicitly **not** in scope, and not authorized by this ADR:

- any schema, table, column, trigger, or enum-value implementation;
- any bump of the authority schema version (currently `5`, merged on
  `main`); this ADR does **not** authorize a schema v6 durable
  trusted-enrollment package (§26);
- self-acceptance of this ADR by the agent that wrote it;
- any production mechanism that **writes** into a guest's configuration or
  provisions anything inside a guest — that would be a mutation, and no
  mutation authority exists yet;
- any change to production startup, scheduler, HTTP, MQTT, or Home
  Assistant wiring;
- any change to ADR 0001, ADR 0002, ADR 0003, or ADR 0004;
- any policy/approval/job/lock authority of any kind (§23);
- endpoint activation, candidate promotion, or failover (ADR 0003 §15's
  separate, still-unauthorized future ADR);
- **designing the future stronger continuity-proof mechanism itself** — §14
  records only the minimum properties such a mechanism must satisfy, not
  its design;
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
   positive continuity proof for both QEMU and LXC (ADR 0001's own
   candidate audit).
3. A silent delete/recreate between two identical, complete polling
   observations can be indistinguishable from stock PVE alone; the backend
   may retain the same `resource_id` for read-only/UX continuity in that
   case (`observational_continuity` stays `consistent`, **not**
   `uncertain` — ADR 0001, "Scenariusze lifecycle i zagrożeń", row 10), but
   this **never** authorizes destructive mutation.
4. `observational_continuity` (`consistent`/`uncertain`/`replaced`) and
   `security_continuity` (`unverified`/`trusted`/`revoked`) are separate
   axes with separate owners; ADR 0001's canonical state matrix forbids
   inferring one from the other automatically.
5. `resource_continuity_revision` is a monotonic per-resource security/
   concurrency token, owned by `resource_incarnations` (ADR 0001,
   `0.5-inventory-model.md`); this ADR reuses it exactly as specified,
   inventing no second resource-level security token.
6. `security_continuity` itself is owned by `resource_incarnations` as a
   mutable field of that single record (`0.5-inventory-model.md`,
   `resource_incarnations`: "mutable: ... security continuity
   (`unverified`, `trusted`, `revoked`), ... enrollment state/revision i
   monotonic `resource_continuity_revision`"); this ADR does not, and must
   not, introduce a second authoritative location for that value (§16).
7. Direct replacement and confirmed removal each already fully define
   their own terminal effect on `security_continuity` (`trusted -> revoked`,
   never-trusted stays `unverified`, no successor inherits anything) —
   ADR 0001 and ADR 0004 respectively; this ADR does not touch either
   transition.
8. Source attestation (ADR 0003) grants no workload/mutation authority by
   itself and is never sufficient for resource trust (ADR 0003 §28).
9. Node/hostd attestation (ADR 0001's node section, `0.5-inventory-model.md`
   `node_attestations`) is a **separate axis** from resource continuity;
   this ADR does not design node attestation and does not let it stand in
   for resource trust (§18).
10. Discovery is strictly read-only and never grants trust, management,
    maintenance, or destructive capability by itself (ADR 0002, AGENTS.md).
11. ADR 0001, "Scenariusze lifecycle i zagrożeń", row 5 (snapshot rollback):
    revalidation of continuity proof is *required behavior*, not optional
    (§17 makes this explicit for any future trust-granting mechanism).
12. ADR 0002's endpoint table: `GET /nodes/<node>/<type>/<vmid>/config` is
    the accepted detail-config read path, and it requires a resolved
    `<node>` locator — it is **not** a purely source-scoped API (§18).
13. The full mutation trust path (HA → API → backend policy/plans/jobs/
    locks/audit → typed host-control → hostd/forced-command → Proxmox)
    remains entirely unimplemented; nothing in this ADR is itself
    sufficient for mutation (§23).

## 4. Terminology

| Term | What it is | Owned by |
| --- | --- | --- |
| **Observational continuity** | ADR 0001's read-only assessment of whether current facts belong to the same incarnation | `resource_incarnations` (ADR 0001, unchanged) |
| **Security continuity** | ADR 0001's `unverified`/`trusted`/`revoked` axis; this ADR defines the *negative boundary* for the current baseline (§9–§13) | `resource_incarnations` (ADR 0001, unchanged) — sole authoritative field, no satellite copy (§16) |
| **Administrative correlation marker** | a backend-issued value an operator places, out-of-band, into a guest's ordinary PVE `description` field; read-only verifiable evidence that "the currently-read config contains the value an operator previously placed there" — **never** continuity/trust evidence (§9–§11) | not a `resource_incarnations` field; at most an optional, non-authoritative audit/correlation record, not authorized for schema in this ADR (§26) |
| **Physical/logical workload continuity** | whether the actual running guest (its disk state, memory, process) is genuinely the same one previously observed | **not represented by any Hubinet Ops primitive, before or after this ADR** — no evaluated candidate establishes it (§6–§13) |
| **Node/host trust** | ADR 0001's separate `node_binding`/`node_attestation` axis | `node_bindings`/`node_attestations` (ADR 0001, unchanged) |
| **Resolved node locator** | the exact current node identifier needed to *address* a config-read request (a presentation/routing fact) | distinct from node **trust**; required for any live config read regardless of trust (§18) |

The vocabulary below is a **research-strength ladder**, not a set of
interchangeable tiers that already authorize the same state. Only the
strongest, undesigned class (clone-resistant / externally-rooted proof, §13)
could ever be sufficient for `trusted`; the other two classes are retained
purely as weaker, non-trust-granting evidence or as future partial
ingredients:

- **Administrative marker evidence** (§9–§11) — useful correlation/audit
  signal only; **not** sufficient for `trusted`.
- **Guest-resident cryptographic key** (§13) — stronger proof of key
  possession at read time; still clone/restore-copyable unless backed by a
  non-copyable root; **not** currently accepted for `trusted`.
- **Clone-resistant / externally-rooted continuity proof** (§13) — the
  *required* class of mechanism before generic persistent `trusted` can be
  supported; not currently designed or available in this ADR.

## 5. Threat model

- an attacker (or an innocent operator mistake) destroys the enrolled
  workload and recreates a different one under the same VMID, hoping stock
  PVE facts alone (name, digest, node) — or any copyable config-resident
  marker — will make the new occupant appear already-trusted; **this is
  the exact witness this ADR's corrected decision is built around (§9)**;
- an attacker who can edit a guest's configuration (a lower privilege bar
  than destructive/PowerMgmt) copies any config-resident marker into a
  guest they control, hoping possession of the marker string alone proves
  continuity;
- backup/restore, disk cloning, or template-based provisioning produces two
  or more guests carrying identical config-resident or disk-resident
  evidence, simultaneously or sequentially;
- an operator's one-time administrative decision is relied upon far later,
  after an invisible destroy/recreate has occurred in between, without any
  re-verification at the actual mutation decision point;
- trust evidence established while a source's trust-domain continuity was
  itself unproven, or later invalidated by an accepted anchor change
  (ADR 0003 §20's authority-eligibility rule), is cited as if still valid;
- a resource legitimately passes through an accepted observational gap
  (`missing`, later `present` again with identical facts) and the system
  is tempted to silently restore prior trust because "nothing looks
  different";
- two operators race to make concurrent administrative decisions about the
  same resource;
- a crash or restart occurs mid-decision, and stale in-memory state is
  relied upon instead of durable committed state;
- a piece of evidence is duplicated onto a second, unrelated resource
  (same or different source), and the system must decide whether that is
  even detectable, and what "detectable" can honestly mean.

None of these is solvable by observing PVE more carefully — they are
exactly the class of threat ADR 0001's own candidate audit already proved
stock PVE facts cannot answer for *identity*. This ADR's central finding is
that the same limitation transfers, undiminished, to every copyable
evidence class evaluated (§9–§13): **entropy defeats accidental collision,
never deliberate or accidental copying/replay.**

## 6. Candidate evidence audit

Legend, identical to ADR 0001's own discipline:

- **FACT-DOC** — documented by Proxmox;
- **FACT-SOURCE** — behavior visible in official Proxmox source;
- **INFERENCE** — architectural conclusion from the facts;
- **UNKNOWN** — property not confirmed by an official contract.

This research is retained in full because it remains valid and useful — the
decision built on it (§9–§13) has changed, not the underlying facts.

| # | Candidate | QEMU | LXC | Evaluation |
| --- | --- | --- | --- | --- |
| 1 | VMID/CTID | yes | yes | **FACT-DOC** (ADR 0001, cited): reusable slot locator, never identity. Rejected outright — already established. |
| 2 | resource type | yes | yes | **FACT-DOC** (ADR 0001): immutable *occupant* property, but says nothing about which specific occupant. Rejected as continuity proof by itself. |
| 3 | name/hostname | yes | yes | **FACT-DOC** (ADR 0001): mutable config, rename explicitly preserves identity by design — cannot simultaneously be identity proof. Rejected. |
| 4 | current node | yes | yes | **FACT-DOC** (ADR 0001): migrates by design; a relation, not identity. Rejected. |
| 5 | config digest | yes | yes | **FACT-SOURCE** (ADR 0001): changes on any edit, can return to a prior value, no create-time binding. Rejected. |
| 6 | QEMU `vmgenid` | yes | n/a | **FACT-SOURCE** (ADR 0001): explicitly regenerated on clone/snapshot-rollback/restore; not cross-type. Rejected. |
| 7 | `smbios1.uuid` | yes | n/a | **FACT-SOURCE**: `qm.adoc` confirms clone explicitly "generate[s] a new UUID for the VM BIOS (smbios1) setting" "to avoid resource conflicts." Not cross-type. Rejected. |
| 8 | `meta.ctime` / creation metadata | yes | UNKNOWN | **FACT-SOURCE** (ADR 0001): no LXC equivalent found; all restore-path guarantees UNKNOWN even for QEMU. Rejected. |
| 9 | MAC addresses | yes | yes (veth) | **FACT-DOC**: clone explicitly randomizes all NIC MAC addresses. Rejected. |
| 10 | disk/storage identifiers | yes | yes | **INFERENCE**: disk content, including anything written inside it, is exactly what clone/backup/restore copy by design. Rejected as continuity proof; this is *why* tier "guest-resident cryptographic key" (§13) cannot solve clone-resistance either. |
| 11 | PVE tags | yes | yes | **FACT-SOURCE**: `tags => { ..., description => 'Tags of the VM/Container. This is only meta information.' }` — Proxmox's own schema comment. Copied on clone. Rejected as a stock signal; smaller entropy capacity than `description`. |
| 12 | PVE description/comment | yes | yes | **FACT-SOURCE**: `description => { type => 'string', maxLength => 1024*8, description => "... saved as comment inside the configuration file." }`, identically defined for QEMU (`PVE::QemuServer`) and LXC (`PVE::LXC::Config`). Copied on clone (not excluded/regenerated per `qm.adoc`'s clone section). **Corrected classification: this is the storage location for an administrative correlation marker (§9), explicitly demoted from "sufficient for `trusted`" to "audit/correlation evidence only" — see §9's full reasoning.** |
| 13 | arbitrary Hubinet-owned marker in guest config | yes | yes | Same field as candidate 12; this is the administrative correlation marker concept, §9. Demoted, not eliminated as a concept — retained for optional audit use only. |
| 14 | Hubinet-owned marker stored outside guest config | UNKNOWN | UNKNOWN | No stock PVE per-guest metadata store outside the guest's own config object is documented; storing evidence purely in Hubinet's own database removes any ability to read it back from the guest at all, degenerating to Family B (pure administrative assertion, §8) unless paired with a config-resident or guest-resident readable value — and pairing does not change §9's conclusion. |
| 15 | guest-resident opaque enrollment token (on-disk, no crypto) | yes | yes | **INFERENCE**: on-disk content is copied by clone/backup exactly like candidate 10. No cryptographic binding to anything not-clonable. Strictly weaker than candidate 16/17 for equal implementation cost; rejected as its own family. |
| 16 | guest-resident asymmetric key / cryptographic agent | yes (via QGA) | via exec | Evaluated in §13. Genuinely cryptographic (proves key possession at read time) but the private key material still lives in disk state that clone/backup copy identically — does not solve clone-resistance. Not sufficient for `trusted` by itself. |
| 17 | QEMU Guest Agent–derived evidence | yes | n/a | **FACT-DOC**: requires the in-guest agent installed, configured, and the guest running and cooperative; not default. Exec-class QGA commands require privilege beyond `VM.Audit` — exact string **UNKNOWN**, flagged for future implementation-time verification. Still subject to candidate 10/16's clone-copyability limitation regardless of privilege. |
| 18 | LXC filesystem/exec-derived evidence | n/a | yes, via `pct exec`/`lxc-attach` | **FACT-DOC** confirms `pct enter`/`pct console` exist; exact scripted-exec privilege contract **UNKNOWN** this session. Same clone-copyability limitation as candidate 16/17 applies to anything it reads from disk. |
| 19 | trusted node/hostd-mediated workload evidence | yes | yes | Evaluated in §18. A trusted node route narrows *where* evidence can be safely collected from; it does not itself manufacture stronger evidence, and does not substitute for resource-level continuity proof. |
| 20 | TPM/vTPM or equivalent | yes (software vTPM) | n/a | **FACT-DOC/INFERENCE**: stock Proxmox VE's vTPM is a software-emulated TPM whose state is itself a disk image (`vtpm0` volume) — copied by clone/backup/snapshot exactly like any other disk (candidate 10's limitation applies identically). A genuinely hardware-rooted, non-clonable TPM attestation chain is **not** a stock PVE guarantee. This is exactly the gap the "clone-resistant / externally-rooted" future class (§13) would have to close. |
| 21 | explicit operator administrative assertion | yes | yes | Evaluated as Family B, §8. Necessary context for any future mechanism's audit trail, never sufficient alone. |
| 22 | combinations of the above | — | — | No combination evaluated here composed solely of ordinary copyable PVE/guest/config state (candidates 1–15, 17–18, 20–21) closes the invisible same-slot destroy/recreate witness (§5, §9). |

**Conclusion of the audit, unchanged from the original research and now
carried through consistently into the decision below:** no field candidate
here constitutes continuity proof sufficient for `security_continuity=
trusted` on its own, for the identical reasons ADR 0001 already established
for the weaker identity-continuity question, and — as this corrective
revision establishes — for the *combination* of any of them as well (§9).

## 7. Why stock-PVE-only continuity is insufficient (Family A — rejected)

ADR 0001's own conclusion — "żadne pole nie daje pozytywnego dowodu
ciągłości dla obu typów" (no field gives positive continuity proof for
either type) — was reached for the *weaker* claim of observational identity
continuity. `security_continuity=trusted` is strictly *stronger*. **Family A
is rejected explicitly and completely**: no combination of VMID, name, node,
digest, `vmgenid`, `smbios1.uuid`, `meta.ctime`, MAC/disk fingerprints, tags,
or description content, read passively from ordinary discovery, may ever be
treated as sufficient evidence for `security_continuity=trusted`. This is a
permanent architectural conclusion.

## 8. Why operator assertion alone is insufficient (Family B — rejected)

An operator who, at time T0, examines a resource and asserts "this is
trusted" is asserting something true *at T0*. The threat model's core
concern is what happens at a *later* mutation decision at T1 ≫ T0: if the
resource was invisibly destroyed and recreated under the identical VMID
between T0 and T1 (indistinguishable from stock PVE polling, per ADR 0001
row 10), a bare T0 assertion — even if perfectly honest — says nothing
about what actually occupies the slot at T1. Re-checking `resource_id`/
`resource_continuity_revision` by CAS at T1 does not help either: those
tokens track the *backend's own* record-keeping, which (per ADR 0001) can
legitimately retain the same `resource_id` across exactly this kind of
invisible gap. **Family B is rejected as sufficient by itself, in every
combination** — including paired with an administrative marker (§9), since
§9 establishes that pairing does not close the gap either. Operator
judgment remains necessary context for any future mechanism's audit trail,
but is never itself sufficient.

## 9. Why an administrative correlation marker is insufficient (Family C — corrected)

**This section replaces the original draft's "recommended decision."** The
original draft proposed a backend-issued, high-entropy value stored in the
guest's `description` field (§6 candidate 12/13), verified read-only, as
sufficient to grant `security_continuity=trusted`. Independent review
constructed the following witness, which this ADR now adopts as the
controlling reason for rejection:

```text
1. trusted resource R at slot VMID 101 (hypothetically, under the
   original draft's rule);
2. marker M present in R's `description`;
3. old workload destroyed entirely between two successful, complete
   discovery observations;
4. another workload recreated at the same VMID/type;
5. M copied/restored into the new occupant's ordinary PVE configuration
   (trivially possible: `description` is readable via the same `VM.Audit`
   privilege the legitimate verification path itself uses -- copying it
   requires no more privilege than a party who could destroy/recreate the
   guest in the first place already has, or than an innocent
   clone-from-template-at-the-same-VMID workflow would carry forward with
   zero malicious intent);
6. no observable missing interval occurs (both polls are complete and
   successful);
7. stock discovery cannot distinguish the replacement -- per ADR 0001 row
   10, `observational_continuity` stays `consistent`, NOT `uncertain`, for
   exactly this no-observable-gap case;
8. `resource_id`/`binding_id`/`locator_generation`/
   `resource_continuity_revision` may all remain unchanged for read-only
   continuity, exactly as ADR 0001 accepts for the indistinguishable
   invisible-replacement case;
9. a live marker re-read returns the same M -- an accepted, unchanged
   `trusted -> trusted` reconfirmation under the original draft's own
   rule, not a mismatch.
```

Marker equality cannot distinguish the wrong incarnation in this witness.
**High entropy prevents accidental random collision. It does not prevent
copying, replay, clone, restore, or deliberate recreation** — the marker is
config data, not a secret protected from anyone who can read the source
guest's config and write a target guest's config, a materially lower
privilege bar than destructive/PowerMgmt.

The accepted invariant this witness would otherwise violate is explicit and
non-negotiable (ADR 0001, `0.5-inventory-model.md`): **false continuity must
never transfer destructive authority.** Therefore:

```text
an administrative correlation marker (description-field or otherwise)
  MUST NOT transition:  unverified -> trusted
  MUST NOT sustain:     trusted -> trusted   (based solely on continued
                                              marker equality)
```

**Reclassification.** The description-field marker concept is preserved —
the primary-source research behind it (§6) remains valid and useful — but
it is renamed and reframed as an **administrative correlation marker** (or
equivalently: operator-reviewed resource marker), not continuity proof:

- it *may* provide evidence that "the currently-read PVE config contains
  the backend-issued value that an operator previously placed there";
- it explicitly does **not** prove: physical workload continuity; logical
  incarnation continuity; absence of destroy/recreate; absence of
  clone/restore; proof uniqueness; destructive mutation eligibility;
- it **cannot**: change `security_continuity`; make policy applicable;
  grant capabilities; preserve old trust; transfer authority across a
  gap/replacement/`resource_id` boundary;
- it *may* be retained as optional future administrative/audit evidence —
  a correlation signal for operators/UX, never a security decision input.

Any future typed evidence-reading boundary for this marker (the original
draft's `ResourceContinuityEvidenceReader`) must be renamed and reframed so
the term "continuity proof" does not falsely imply marker equality
satisfies Blocker B. Such a reader — if ever built, as optional
administrative tooling, not authorized by this ADR (§26) — is permitted to
verify marker **presence/equality only**. It is not, and must never be
described as, a trusted-incarnation oracle.

## 10. What administrative marker evidence honestly establishes

**What it establishes, at most:** that, at read time, the operator
personally set a specific, unpredictable, backend-issued value into the
exact guest they intended to mark, and Hubinet Ops independently read that
exact value back from the exact resource being examined over an
already-authenticated, already-accepted-privilege read path — a snapshot
correlation fact, valid only at the instant it was read.

**What it does not establish** — exhaustive, binding on any future use of
this evidence class:

- that the underlying disk/process state is the same one that existed when
  the marker was set (§6 candidate 10; clone/backup copy config
  identically);
- that no one else with `VM.Config`-class access to *any* guest — including
  the *same* guest across a destroy/recreate at the *same* VMID (§9's
  witness), not merely a *different* guest — has copied the marker value
  forward;
- physical machine or host uniqueness of any kind;
- anything about the workload's *content* (what is running inside it);
- continuity across any gap, migration, clone, snapshot rollback, or
  backup restore, observed or unobserved;
- authority carried over from a different `source_attestation_epoch` or a
  different `resource_continuity_revision` than the one it was read under.

## 11. Clone/duplication limitation (reinforces §9's rejection)

Because `description` is ordinary config data copied by clone (§6 candidate
12), a clone of a marked resource **will** carry the identical marker value
into the new guest. Because the same underlying limitation defeats both the
"different guest" duplication case and the "same slot, invisible
destroy/recreate" case (§9), this is not a narrow edge case — it is the
general failure mode of any evidence class drawn entirely from ordinary
guest/config state. A matching marker value on a second, simultaneously-live
resource — or on a resurrected/recreated occupant of the *same* slot — must
never be treated as proof that either resource is "the" legitimately marked
one. This reinforces, rather than merely accompanies, §9's rejection.

## 12. Guest-agent/exec-based evidence detail (not designed)

For completeness given the candidate list evaluated: QEMU Guest
Agent–mediated evidence (§6 candidate 17) and LXC `pct exec`-mediated
evidence (§6 candidate 18) both require privileges beyond the `VM.Audit`-
only posture ADR 0002 established for discovery, both require the guest to
be running and (for QGA) to have cooperative in-guest software installed
and configured — not a default state — and neither is confirmed against a
primary source in this session to an exact privilege string (**UNKNOWN**,
flagged for future implementation-time verification). Both remain subject
to §13's clone-copyability limitation regardless of privilege, since the
evidence they retrieve is itself disk-resident.

## 13. Guest-resident cryptographic key and the required future class

A guest-resident asymmetric key or cryptographic agent (§6 candidate 16) is
genuinely cryptographic — proving key *possession* at read time is a
materially stronger claim than proving a config string matches. But the
private key material still lives in disk/config state that clone, backup,
and restore copy identically (§6 candidate 10, 20; §11). **It does not
solve clone-resistance, and is therefore not sufficient by itself for
persistent, generic `trusted` under ADR 0001's threat model** — the same
witness in §9 applies with equal force: a party who can destroy/recreate a
guest and copy its disk state copies the key along with everything else.

The only class of mechanism that could close this gap is one whose proof
does **not** live entirely inside state that an ordinary destroy/recreate
(with disk/config copy) can reproduce — for example, a hardware-rooted
attestation chain unavailable from stock, or an out-of-band Hubinet-managed
identity provisioned and verified through a channel that is not itself
guest disk state. This class is **required** before generic persistent
`trusted` can be supported, and it is **not designed by this ADR** and
**not available from stock Proxmox VE**. This is the honest, deliberately
undesigned gap this ADR leaves open — the same posture ADR 0003 §7 already
established for its own tier-3 gap.

## 14. Minimum properties any future stronger-proof mechanism must satisfy

Without designing the mechanism, any future ADR proposing a mechanism
sufficient for `security_continuity=trusted` must, at minimum:

- **not** preserve authority merely because an attacker or an ordinary
  operator workflow can copy VMID, name, resource type, PVE description,
  tags, config, disks, guest filesystem content, or a guest-resident
  private key into a false successor or recreated occupant (§9, §11, §13);
- define explicit QEMU/LXC support scope (parity or documented asymmetry);
- define exact clone behavior;
- define exact snapshot-rollback behavior (§17);
- define exact backup-restore behavior (§17);
- define exact same-slot destroy/recreate behavior (§9's witness, closed —
  not merely disclosed as a limitation);
- define exact node-migration behavior;
- define exact replay/duplication handling, fail-closed by default;
- define exact source-attestation-epoch coupling if the proof depends on
  source trust-domain continuity (§19);
- define exact node-trust dependence, if any, kept separate from resource
  trust unless the proof is genuinely node-bound (§18);
- define the exact remote-evidence trusted-reader boundary, following the
  three-phase discipline (§20);
- define exact CAS fields and transaction boundaries;
- define exact revocation, rotation, and restart/retention semantics.

**If a mechanism cannot distinguish its intended security claim after an
adversary or ordinary workflow has copied all the state it relies on into
another incarnation, it cannot be sufficient for persistent canonical
`trusted` under ADR 0001.** This is the single test every future candidate
must pass that no candidate evaluated in this ADR passes.

## 15. `security_continuity` — single canonical owner

`resource_incarnations.security_continuity` is the **sole** durable
authoritative owner of the `security_continuity` axis (ADR 0001,
`0.5-inventory-model.md`; §3 item 6 above). This ADR does not introduce, and
no future implementation of this ADR may introduce, a second table or row
holding a current, independently-authoritative copy of that value. Any
future satellite state associated with a stronger continuity mechanism (once
one exists) may contain only concepts such as: `current_evidence_id`;
enrollment/evidence generation; proof/marker record identity; evidence
status/provenance — **never** a second canonical `security_continuity`
field. Audit/evidence tables are, and remain, never a second current-state
authority (mirroring the existing "audit records are not current-state
authority" discipline already stated for node attestation in
`0.5-inventory-model.md`).

## 16. Marker uniqueness — backend-global, never-reuse

Even though the administrative correlation marker no longer grants or
sustains `trusted`, its administrative identity semantics must remain
unambiguous, and this ADR fixes exactly one rule rather than leaving a
choice to a future implementer:

```text
backend-issued marker values are unique across the ENTIRE backend
  instance (namespaced by backend_instance_id) and are NEVER intentionally
  reused across retained history

a value previously issued/retained for any resource must not be
  intentionally issued for another resource even after the original
  resource becomes revoked, terminal, or historical
```

This is honestly bounded: it prevents Hubinet Ops **itself** from
intentionally accepting or reissuing duplicate administrative marker
identities. It does **not** cryptographically stop an external actor from
copying the string into another guest (§9, §11) — backend-side uniqueness
enforcement and external copy-resistance are different properties, and this
rule only provides the former. A detected duplicate (the same marker value
observed on two resources, however it got there) is an **audited
ambiguity/collision**, nothing more — because the marker is not continuity
trust, backend-side uniqueness must never be read as turning the string
into an incarnation proof.

## 17. Snapshot rollback / backup restore — future-mechanism requirement

ADR 0001, "Scenariusze lifecycle i zagrożeń", row 5, states as *required
behavior*: "snapshot rollback tego samego workloadu ... Rewalidacja
continuity proof; przy ambiguity ten sam `resource_id`/binding przechodzi do
`uncertain`/`quarantined`, bez effective destructive policy." This is
mandatory, not conditional. For any **future** mechanism capable of granting
`trusted`, this ADR fixes the conservative requirement that mechanism must
satisfy:

```text
a detected snapshot rollback MUST invalidate current trust eligibility
  until the future mechanism has been fully revalidated

within the existing canonical three-value vocabulary, the required
  transition is:
    trusted -> revoked
    resource_continuity_revision +1 exactly once

a completely fresh, accepted re-validation is then required to restore
  trusted -- prior evidence never carries forward across the rollback
  boundary implicitly

the same fail-closed rule applies to a detected same-resource backup
  restore, unless a specific future accepted proof mechanism explicitly
  proves its own trust anchor survives that restore safely -- this ADR
  does not grant that exception generically
```

No fourth canonical security state is introduced. For the current
stock-PVE baseline this requirement is prospective — no resource can reach
`trusted` through any mechanism this ADR evaluates — but the requirement is
fixed now so a future mechanism's ADR does not have to re-derive it, and so
it cannot be silently weakened later.

## 18. Node locator vs. node trust; resolved node CAS requirement

ADR 0002's accepted detail-config read endpoint is `GET /nodes/<node>/
<type>/<vmid>/config` (§3 item 12) — a **node-scoped** path, not a purely
source-scoped one, despite what the original draft of this ADR claimed. Any
live marker-correlation read, or any future stronger mechanism's remote
evidence read, therefore requires a **resolved current node locator** to
even construct the request. This is explicitly **not** the same as node
*trust*:

```text
resolved current node locator  -- a presentation/routing fact, required to
                                   address the PVE request at all

node_trust_state == trusted    -- a security claim about the node itself,
                                   NOT required merely to perform a
                                   read-only config read (ADR 0002's
                                   VM.Audit-class discovery already reads
                                   config without requiring node trust)
```

For any future three-phase live evidence/marker-correlation read (mirroring
ADR 0003 §19a's discipline, §20 below):

```text
PHASE 1 must capture the exact current node_id / external node locator
  used to construct the request, alongside every other expected-context
  field

PHASE 2 performs the remote config read outside any DB write transaction,
  addressed using the phase-1-captured node locator

PHASE 3 must re-CAS the exact node relation captured in phase 1, alongside
  every other captured field

if the resource migrated or the node relation changed during remote I/O:
  classify the attempt as stale; accept no evidence result as current;
  retry only after a fresh phase-1 capture

`last_known_node_id` (the presentation-layer fallback used when a node is
  currently unavailable) must never be used as the security/request route
  for a live evidence read -- only an exact, freshly-captured current node
  locator is acceptable
```

## 19. Source-attestation epoch rule (preserved)

The general rule from the original draft remains sound and is preserved
without change, because it does not depend on which trust mechanism is
eventually chosen:

```text
future Blocker-B proof evidence that depends on source trust-domain
  continuity is authority-eligible only under the exact
  source_attestation_epoch at which it was accepted

an epoch bump makes old evidence immediately authority-ineligible

old evidence remains retained, for audit, never deleted

no carry-forward mechanism is designed here
```

**Timing clarification (resolves the original draft's advisory-level
ambiguity):** even if durable stored `trusted -> revoked` materialization
were performed in a separate, later transaction in some future design, **no
consumer may treat a `trusted` row as authority-eligible** when `current
source_attestation_epoch != evidence.source_attestation_epoch`. This
independent consumption-time gate means stale old-epoch trust can never be
consumed while materialization is pending, regardless of the exact
transaction shape a future mechanism chooses. This ADR does not solve an
implementation transaction shape that no currently-supported mechanism
uses — that remains a legitimate detail for the future mechanism's own ADR.

## 20. Remote evidence read pattern (general requirement, preserved)

Any future mechanism — including optional administrative marker-correlation
tooling, if ever built — that requires a live remote read must follow ADR
0003 §19a's exact three-phase discipline literally: a short, read-only DB
capture of expected context (Phase 1); a trusted-reader call entirely
outside any write transaction (Phase 2); and a `BEGIN IMMEDIATE` write
transaction that re-validates every captured field by exact CAS, including
the resolved node locator (§18), before accepting or rejecting atomically
(Phase 3). A pre-read outside this pattern is never the security boundary.
No caller-supplied `verified=true` boolean or unverified hash may ever
bypass a trusted reader.

## 21. Node trust separation

Unchanged and explicit: `trusted resource != trusted node`, and vice versa.
No evaluated candidate in this ADR requires node trust to be read (§18); a
future mechanism that turns out to require node-mediated evidence
collection must define its own explicit node-migration/re-attestation
semantics at that time. A future mutation, regardless of any resource trust
mechanism, still independently requires the accepted node/hostd trust route
— resource trust and node trust remain two separate, both-required gates
for any future mutation, never substitutes for each other.

## 22. Publication and revision semantics

No new published concept is introduced by this ADR. Because no mechanism
evaluated here grants `trusted`, there is no new `security_continuity`
transition for this ADR to wire into `resource_continuity_revision`,
`inventory_revision`, or `published_state_revision` — those existing fields
and their existing transition rules (ADR 0001/`0.5-inventory-model.md`) are
untouched. If a future stronger-proof ADR introduces an actual
trust-granting transition, that ADR is responsible for defining its own
revision/publication effect, following the same pattern ADR 0001/0003/0004
already established. Home Assistant remains presentation-only; no writable
HA transport is introduced or implied.

## 23. Policy boundary preserved (strengthened, not merely restated)

Unchanged and, given §9's conclusion, now unconditionally true for the
entire current stock-PVE baseline: **no resource can reach
`security_continuity=trusted` through any mechanism this ADR evaluates**,
therefore:

```text
every resource under the current baseline: security_continuity ==
  unverified OR revoked

=>  effective destructive policy = false, for every resource, always
=>  maintenance permission = none, for every resource, always
=>  effective destructive capabilities = none, for every resource, always
```

This holds regardless of retained stored policy, administrative marker
state, source attestation state, or node trust state — retained policy is
never applicable policy, and `trusted` remains necessary (never sufficient)
for any future mutation once some future mechanism eventually grants it.

## 24. R0 boundary

R0 remains read-only runtime activation. `0.5-inventory-model.md`'s "Phase 1
runtime activation gate" (the accepted 19-item ledger) contains **no**
requirement referencing Blocker B, workload continuity, or trusted
enrollment — it governs identity, source binding, discovery, and
publication only. This ADR therefore confirms, rather than overrides,
that R0 does not require a Blocker B mechanism to exist. Given §9's
conclusion, R0 activation, if and when separately reviewed and approved,
must not include:

```text
- granting trusted to any resource, by any path;
- running enrollment automation of any kind;
- writing an administrative marker into any guest configuration
  (that would be a mutation);
- exposing writable HA controls;
- enabling policy/jobs/mutations;
- endpoint activation/failover;
- inferring trust from discovery;
- treating marker equality (or any other copyable evidence) as security
  authority, even informally or in presentation layer logic.
```

Every resource remaining `unverified`/`revoked` and every destructive
capability remaining `none` (§23) is precisely what makes read-only
presentation safe to activate independent of Blocker B's resolution — the
absence of a trusted workload-continuity mechanism blocks mutation, not
read-only observation.

## 25. Blocker B status

This ADR closes the **architecture research question**: "Can stock PVE +
ordinary read-only config evidence safely provide generic persistent
workload security continuity?" **Answer: no** — no candidate evaluated in
§6–§13, alone or in combination, survives ADR 0001's own accepted
invisible-replacement witness (§9).

This ADR does **not** close Blocker B for mutation authority. **Blocker B
remains OPEN.** A future, separately reviewed and separately **ACCEPTED**
stronger continuity-proof mechanism (§13, §14) is required before any
`unverified -> trusted` transition may be implemented for the general case.

## 26. WAVE B1 status

WAVE B1, as originally imagined — a durable trusted-enrollment
implementation package (schema v6, current-trust-state row, evidence
tables, enrollment/revocation authority operations) — is **DEFERRED / NOT
AUTHORIZED**. No schema bump is authorized by this ADR. Implementing a
schema v6 merely to persist an administrative marker that this ADR
establishes cannot satisfy the security property would not close Blocker B,
and creating a dormant "trust subsystem" for architectural symmetry with
WAVE C1/A1 is explicitly **not** a reason to build one — symmetry with
completed waves is not itself an architecture requirement.

A later, separately reviewed and accepted stronger-proof ADR (§13, §14) may
re-authorize a future B1 implementation package. Only after such an ADR
exists may a future implementation decide schema bump, current proof state,
immutable evidence, reader, enrollment/revocation operations, epoch
coupling, reconciliation hooks, and tests.

## 27. Proposed sequencing

```text
WAVE B0 (this ADR, corrected) -- establishes the negative stock-PVE trust
  boundary and the R0 safety conclusion, pending acceptance
        |
        v
R0 -- read-only runtime activation review (separate, not started; §24)
        |
        v
[future, separately reviewed and accepted stronger continuity-proof ADR]
        |
        v
WAVE B1 -- durable trusted-enrollment implementation (deferred, not
  authorized until the above ADR exists)
        |
        v
Phase 1C -- policy/jobs/mutation authority (already gated on this, and on
  much else, per AGENTS.md and 0.5-implementation-status.md)
```

Blocker B remains an explicit hard prerequisite before any future Phase 1C/
mutation authority can become usable. It is **not** a prerequisite for R0
(§24) — no ACCEPTED ADR or the `0.5-inventory-model.md` runtime activation
gate makes B1 implementation, rather than Blocker-B security semantics, a
prerequisite to R0.

## 28. Adversarial matrix

For the current stock-PVE/read-only baseline, no row below produces
`security_continuity=trusted`. This matrix intentionally shows a uniform
negative result — that uniformity is the finding, not an omission.

| # | Scenario | Resulting `security_continuity` | Why |
| --- | --- | --- | --- |
| A | Same VMID/name/config, marker absent | `unverified` | No evidence offered at all (§7). |
| B | Operator assertion only, no marker | `unverified` | Family B alone rejected (§8). |
| C | Stock digest match only | `unverified` | Family A rejected (§7). |
| D | Ordinary description/tag text, not a Hubinet-issued marker | `unverified` | Fails exact-value/format check; not evidence of anything (§9). |
| E | Correct backend-issued administrative marker, freshly read | `unverified` (unchanged) | Administrative correlation only, never sufficient for `trusted` (§9, §10). |
| F | Copied marker observed on a second, distinct resource | both resources: `unverified`/unchanged | Duplication is an audited ambiguity, not evidence for either resource (§11, §16). |
| G | Copied marker after invisible same-slot recreate (no observable gap) | `unverified` (new occupant), and the prior resource's own history remains whatever it already was | This is §9's controlling witness: marker equality cannot distinguish the replacement; nothing in this ADR treats it as sufficient. |
| H | Cloned resource with copied marker | new resource: `unverified` | Clone produces a new `resource_id`; marker copied along with config proves nothing about the new incarnation (§11). |
| I | Same-resource snapshot rollback | if a future mechanism ever grants `trusted`: `trusted -> revoked` mandatory (§17); under the current baseline: `unverified`/`revoked`, unaffected either way since nothing here reaches `trusted` | ADR 0001 row 5's mandatory revalidation requirement (§17). |
| J | Backup restore (same resource) | same as I | Same fail-closed default as rollback unless a future mechanism proves otherwise (§17). |
| K | Source-attestation epoch bump | any evidence tied to the old epoch: authority-ineligible immediately (§19); does not itself change `security_continuity` under the current baseline since nothing here is `trusted` | §19's authority-eligibility rule, preserved from the original draft. |
| L | Source relationship mismatch | new trust-sensitive decisions blocked; current `security_continuity` (`unverified`/`revoked`) unaffected by the mismatch alone | Mirrors ADR 0004 §16's mismatch-gate discipline. |
| M | Node migration | unaffected | No evaluated candidate requires node trust to be read; node migration alone is not a security event for this axis (§21). |
| N | Resolved node changes during a marker-correlation read | attempt classified stale; no result accepted as current | §18's Phase 3 CAS requirement on the resolved node locator. |
| O | Backend restart | unaffected, durable | No in-flight decision state is trusted across a restart; any partial attempt is discarded, not resumed (§20's Phase 3 atomicity). |

## 29. Closed-decision checklist

1. **What does `trusted` mean?** Still the accepted ADR 0001 concept: the
   required accepted continuity proof remains valid and no disqualifying
   gap/evidence exists (ADR 0001's own definition, unchanged by this ADR).
2. **Does this ADR provide such a proof for stock PVE?** **NO.**
3. **Can the description marker grant `trusted`?** **NO** (§9).
4. **Can operator assertion grant `trusted`?** **NO** (§8).
5. **Can source attestation grant `trusted`?** **NO** (§3 item 8).
6. **Can node trust grant `trusted`?** **NO** (§21).
7. **Can discovery grant `trusted`?** **NO** (§3 item 10).
8. **Can a copyable guest-resident key alone grant persistent `trusted`?**
   **NO**, not under the clone/restore threat model (§13).
9. **Is Blocker B closed for mutation?** **NO** (§25).
10. **Can B1 implement `trusted` without a future stronger-proof
    architecture decision?** **NO** (§14, §26).
11. **Does this prevent R0 read-only activation?** **NO**, provided R0
    keeps trust/mutation disabled and every resource remains
    `unverified`/`revoked` (§24).
12. **Does trust ever transfer to a successor?** **NO** (unchanged from
    ADR 0001/0004, never affected by this ADR).
13. **Is `security_continuity` owned only by `resource_incarnations`?**
    **YES** (§15).
14. **Does snapshot rollback require future-mechanism revalidation?**
    **YES** (§17).
15. **Does same-slot invisible recreate defeat ordinary marker
    continuity?** **YES** — this is §9's controlling finding.

This ADR is **not** architecture-complete for a `trusted`-granting B1
implementation, and does not claim to be. It **is** architecture-complete
as the negative stock-PVE capability boundary and the R0 safety decision:
every resource stays `unverified`/`revoked`, no destructive capability
exists anywhere in the current baseline, and R0 may proceed read-only
independent of Blocker B's eventual resolution.

## 30. What remains unresolved after this ADR

1. The actual design of a clone-resistant/externally-rooted continuity
   mechanism (§13) — explicitly not designed here, requires its own future
   ADR.
2. The exact QGA/`pct exec` privilege contract (§12) — UNKNOWN, deferred to
   whichever future ADR needs it.
3. Whether/how an administrative correlation marker (§9) is ever
   implemented as optional audit/UX tooling — not authorized by this ADR;
   if pursued, requires its own scoped review establishing it is
   genuinely non-authoritative in every code path that touches it.
4. Long-term retention/purge policy for any future evidence records —
   out of scope, mirroring ADR 0003/0004's identical posture.

No contradiction with ADR 0001, ADR 0002, ADR 0003, or ADR 0004 was found or
introduced by this corrective revision. Every normative rule above is
either a direct restatement/reuse of an already-accepted invariant, or a
new, narrower negative-boundary rule scoped strictly to the corrected
conclusion this ADR now reaches.

## 31. Implementation consequences

**No WAVE B1 durable trusted-enrollment package is currently authorized.**
A future stronger-proof ADR must first define a sufficient mechanism
satisfying §13/§14. Only after that ADR exists and is accepted can a future
implementation package decide: schema bump; current proof state; immutable
evidence; trusted reader; enrollment/revocation operations; epoch coupling;
reconciliation hooks; tests. The administrative correlation marker concept
by itself does not justify a new authority schema version, and this ADR
does not authorize one.
