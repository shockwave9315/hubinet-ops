# ADR 0003: source binding / attestation for Proxmox inventory sources

Status: **PROPOSED / DRAFT — READY FOR OPERATOR ACCEPTANCE**

Date: 2026-08-16

This ADR is **not accepted**. It does not authorize implementation of any new
persistent schema, runtime behavior, endpoint activation, or failover. It is
architecture for a future implementation package (referred to below as "the
next package"), gated on explicit operator/reviewer acceptance exactly like
ADR 0001 and ADR 0002 were before they became `ACCEPTED`.

## 1. Context / problem

ADR 0002 accepts a hard constraint: one `inventory_source_id` has exactly one
active discovery endpoint, and automatic endpoint failover is disabled.
Candidate/inactive endpoints exist only as inert metadata. Activation of a
candidate, replacement of the active endpoint, and multi-endpoint failover are
all explicitly deferred to "a later accepted source-binding/attestation
contract" (ADR 0002 §"Source i endpointy", §"Nierozstrzygnięte kwestie" #6;
`0.5-inventory-model.md` §`source_endpoints`, Phase 1 runtime activation gate
closing paragraph).

The reason that contract does not exist yet is that Hubinet Ops has no
accepted primitive that answers: *does a newly contacted or changed endpoint
still belong to the logical Proxmox environment this `inventory_source_id`
was created for?* ADR 0002 is explicit that a stable canonical URL, TLS
session validity, cluster name, node membership, hostname, or VMIDs do not
answer this question (`stable/unchanged endpoint URL != proven physical
source continuity`).

This ADR proposes a **logical PVE trust-domain anchor** primitive, bound to
one `inventory_source_id` through an explicit, audited operator/backend
**enrollment** decision, as the missing prerequisite for that later contract.
It does not itself design endpoint activation/failover — it designs what a
future activation/failover contract would be allowed to rely on.

## 2. Scope and non-goals

In scope: the concept, terminology, threat model, evidence source, and
fail-closed semantics of source-level trust-domain attestation, and how it
must interact with every existing accepted revision/epoch/fencing mechanism.

Explicitly **not** in scope, and not authorized by this ADR:

- attestation tables, columns, or any other schema change;
- any bump of the authority schema version;
- any endpoint activation, candidate promotion, or failover behavior;
- any change to production startup, scheduler, HTTP, or Home Assistant wiring;
- self-acceptance of this ADR by the agent that wrote it;
- any reinterpretation of the existing `transport_trust_revision` contract;
- any change to ADR 0001 or ADR 0002;
- any weakening of the current fail-closed single-fixed-active-endpoint
  posture. That posture remains exactly as accepted until a *separate*
  activation/failover ADR is accepted on top of this one.

## 3. Existing accepted invariants that remain unchanged

- One `inventory_source_id` = exactly one active discovery endpoint;
  automatic failover stays disabled (ADR 0002).
- `inventory_source_id` is a backend-generated opaque identity; URL, cluster
  name, node membership, and TLS certificate are not identity (ADR 0001,
  ADR 0002).
- Candidate/inactive endpoints never participate in discovery, retry, or
  failover (ADR 0002).
- `transport_trust_revision` continues to mean exactly what it means today:
  the monotonic revision of TLS trust *policy* (CA roots trusted, pinned
  fingerprint, verification policy) bound to one endpoint and revalidated by
  every discovery run (ADR 0002). This ADR does not touch its meaning.
- `source_config_revision` continues to mean exactly what it means today
  (ADR 0002, `0.5-inventory-model.md`). This ADR does not fold trust-domain
  attestation into it; see §22.
- `baseline_mode` (standalone/cluster) remains a per-run observed fact, not
  identity (this wave's G8 closure). This ADR does not change that.
- Base read-only inventory discovery at the one existing active endpoint of
  an existing source is **not** blocked by the absence of a source-binding/
  attestation contract (`0.5-inventory-model.md`, Phase 1 gate closing
  paragraph: "Brak zaakceptowanego source-binding/attestation contract
  blokuje zmianę active transport targetu... Nie blokuje to podstawowego
  read-only inventory przy jednym active endpoint"). This ADR preserves that
  exact boundary; see §31 for the one open question this raises.

## 4. Terminology

Seven distinct concepts must never be treated as interchangeable:

| Term | What it is | Owned by |
| --- | --- | --- |
| **Backend source identity** | `inventory_source_id`: opaque, backend-generated, durable | `inventory_sources` (ADR 0001/0002, unchanged) |
| **Endpoint locator** | canonical HTTPS URL/transport locator + `canonicalization_contract_version` for one `endpoint_id` | `source_endpoints` (ADR 0002, unchanged) |
| **Transport trust** | TLS policy used to negotiate/validate the HTTPS session to one endpoint (CA roots, pin, verification policy), versioned as `transport_trust_revision` | `source_endpoints` (ADR 0002, unchanged) |
| **Logical PVE trust-domain anchor** | evidence identifying which internal PVE cluster/standalone trust domain answered, independent of which node/URL was contacted | **new concept, this ADR** |
| **Attestation / enrollment** | the explicit, audited decision binding one `inventory_source_id` to one accepted trust-domain anchor value, at one attestation epoch | **new concept, this ADR** |
| **Physical identity** | the actual physical or virtual machine(s) running PVE | **not represented by any Hubinet Ops primitive, before or after this ADR** |
| **Resource/workload continuity** | ADR 0001's `resource_continuity_revision` / `security_continuity` for one guest incarnation | `resource_incarnations` (ADR 0001, unchanged; "Blocker B") |

This ADR designs a **three-tier evidence model** for the logical PVE
trust-domain anchor (fully specified in §7/§10a), not a single primitive,
and the tiers must never be conflated with one another:

- **Tier 1 — asserted PVE root-CA identifier evidence** — a self-reported
  identifier value, read as ordinary API response data over an
  already-authenticated transport session, that an operator explicitly
  chooses to trust as evidence at enrollment time. **This is what this ADR
  recommends as the baseline mechanism** (§9).
- **Tier 2 — cryptographic TLS-peer-to-enrolled-PVE-CA binding evidence** —
  whether the certificate actually presented in the already-completed TLS
  handshake for a connection cryptographically chain-verifies against the
  enrolled root CA. This *is* genuine cryptographic evidence: it proves
  leaf-key possession plus a valid chain to the enrolled PVE CA, subject to
  the clone/key-compromise limitations documented in §10a/§11. Available
  from stock Proxmox VE capabilities (§6) whenever that chain actually
  validates — determined by the actual chain, never by a certificate's
  "default"/"custom"/"ACME" label (§10a). **This ADR describes tier 2 as
  optional, additional, corroborating evidence; it does not design it as
  the primary mechanism and does not, by itself, authorize any state
  change** (§10a, §15).
- **Tier 3 — a future, stronger host-specific/Hubinet-managed attestation**
  — not designed by this ADR. Potentially able to give properties beyond
  tier 2, such as a dedicated Hubinet-controlled host identity or stronger
  clone-resistance, but its exact properties remain unresolved (§7, §30.5).
  **Stock, read-only Proxmox VE API provides no tier-3 primitive** — this is
  the actual gap this ADR leaves open, not an absence of cryptographic
  evidence altogether, since tier 2 already is cryptographic.

The central design discipline of this ADR follows directly from that model:
**logical PVE trust-domain anchor equality — at any tier — is not, and must
never be treated as, proof of physical identity** (§9, §10, §10a, §11); tier
1 additionally is not, by itself, cryptographic proof of anything (§9,
§10); and even tier 2, though genuinely cryptographic, does not prove root
CA private-key possession, physical uniqueness, or freedom from clone/
compromise (§10a). None of the three tiers is, by itself, proof of Hubinet
Ops source identity — each is *evidence* a human explicitly accepts at one
enrollment event, exactly as ADR 0001 treats a `resource_id` as never
self-proving physical incarnation continuity.

## 5. Threat model

Adversaries/failure modes this ADR must reason about:

- an operator or attacker repoints the existing stable URL (DNS, reverse
  proxy, rebuilt host reusing the same address) at a different PVE
  environment;
- a standalone node used as the active endpoint is joined into a foreign
  cluster, silently changing which trust domain answers at that URL;
- an operator wants to add a second endpoint (different node, different
  URL) intending it to route to the *same* logical environment, and needs a
  basis stronger than "it answered";
- backup/restore, disk cloning, or full-environment duplication produces two
  reachable PVE instances sharing identical CA material;
- an attacker who has copied (not merely observed) the PVE cluster's CA
  private key can mint indistinguishable evidence for an illegitimate
  instance — this ADR's proposed anchor primitive is explicitly **not** a
  defense against that key-compromise class; see §11 and §29;
- **a strictly weaker attack than key compromise**: because the anchor value
  is delivered as ordinary API response data (§9, §10), *any* endpoint that
  can complete transport authentication (including with its own, entirely
  unrelated, genuinely-owned leaf certificate — e.g. a valid ACME
  certificate, case F) can simply return an arbitrary or copied fingerprint
  string in the `certificates/info` response body, without ever possessing
  the real PVE root CA private key at all. This does not require stealing
  key material — only writing (or misconfiguring) server-side code that
  answers the request. This is the sharpest form of the proof-of-possession
  gap identified in §9/§10;
- the Hubinet Ops authority database itself is restored from an older or a
  cloned backup, independently of what happened to the PVE side in the
  meantime;
- races between an in-flight discovery run and a concurrent attestation
  decision, and between attestation and restart recovery, must not silently
  cross evidence across attestation epochs, mirroring the existing
  `source_config_revision`/`transport_trust_revision` CAS discipline;
- **security-sensitive evidence established under one attestation epoch must
  not silently retain authority under a later epoch.** Concretely: a
  resource is enrolled with a future Blocker B workload-continuity proof and
  reaches `security_continuity=trusted` while `source_attestation_epoch=7`;
  an operator later accepts a changed PVE anchor, bumping
  `source_attestation_epoch` to `8`; ordinary reconciliation (ADR 0001/0002,
  unaffected by attestation) legitimately retains the same `resource_id` for
  the same VMID/type. If the epoch-7 trust were still authority-eligible
  after the bump, a future mutation could silently carry old workload trust
  into a source context whose continuity was just explicitly broken. §20,
  §26, §27, and §29 make the fencing rule that prevents this normative now,
  even though neither Blocker A nor Blocker B evidence exists yet.

### 5a. Case-by-case adversarial classification

Required minimum analysis, one row per case from the wave brief. Per Finding
1 of the corrective review, this table strictly separates **ground truth**
(what the scenario description stipulates is actually true — known to the
ADR author writing the scenario, never something Hubinet Ops can observe)
from **what anchor evidence can establish** (bounded by §9/§10 for tier 1 —
at most an *asserted* identifier match read as API data over an
authenticated session, never cryptographic proof-of-possession, never
physical-machine proof — and by §10a for the optional, cryptographically
stronger tier 2, where a row notes it is available). Conflating ground
truth with evidence was the defect this revision corrects: a row's ground
truth being "same physical machine" (because the scenario says so) must
never be read as something either evidence tier proves.

| Case | Scenario | Ground truth: same trust domain | Ground truth: same physical machine | What anchor evidence can establish | Required Hubinet Ops handling |
| --- | --- | --- | --- | --- | --- |
| A | Fresh standalone install | n/a — nothing precedes it | n/a | n/a; first read only defines the enrolled value | Enrollment (§12) records the asserted value as baseline; establishes nothing about identity yet. |
| B | Cluster created from an existing standalone node | **unknown** — official contract for CA preservation across `pvecm create` is not verified here | yes, by construction | an asserted match or mismatch, whichever is actually returned | Not assumed preserved; whichever the read shows is handled by ordinary re-attestation (§16). |
| C | Standalone node joins an existing foreign cluster | no — node adopts the foreign cluster's CA | yes (box unchanged) | an asserted mismatch, if genuine PVE software honestly reports it (§9) | Mismatch handling (§17) even though the physical box never changed — the sharpest same-peer/different-domain case. |
| D | Adding/removing cluster nodes (existing members) | yes — surviving members keep the existing CA | n/a (concerns other members) | an asserted match, if checked | No anchor effect; ordinary operational event; no check is required. |
| E | Node certificate (leaf) renewal, CA-signed | yes — CA unchanged | yes | tier 1: an asserted match (root-CA API data unaffected by leaf rotation); **tier 2 also available and matches** — the renewed leaf still chain-verifies against the enrolled root CA (§10a) | `transport_trust_revision`-relevant only (ADR 0002); no re-attestation. |
| F | ACME/custom pveproxy certificate replacement | yes — internal root CA unaffected | yes | tier 1: an asserted match, still returned as API data independent of which leaf negotiated the session — the case that most sharply shows the evidence is a self-report, not a TLS-handshake proof (§9); **tier 2 is typically, but not categorically, unavailable** — the normative test is whether the actual leaf chain-verifies against the enrolled root CA, and a typical ACME/public-CA leaf will not, but this is not guaranteed by the "custom"/"ACME" label alone; either way a matching tier-1 read never repairs a failed chain (§10a) | Transport-trust-policy change only (§23); no re-attestation of the anchor required; unless the actual chain happens to validate, this configuration operates at tier-1-only strength, requiring out-of-band verification or a future tier-3 primitive for anything stronger (§10a). |
| G | PVE root CA lifetime/deliberate regeneration | no — deliberate anchor change | yes | an asserted mismatch | Explicit re-attestation accepting the new anchor (§16). |
| H | Endpoint DNS/IP/port change | depends on target; not knowable in advance | unknown | an asserted match or mismatch, depending on target | New `endpoint_id` (ADR 0002); candidate check (§14) supports but never alone authorizes activation. |
| I | Switching to another node of the same cluster | yes — same cluster CA | no — different box, same cluster | tier 1: an asserted match, expected; **tier 2 also expected** — every cluster-member node's default leaf chains to the same replicated cluster root CA (§10a), giving stronger corroboration | Still requires explicit candidate-enrollment action (§14/§15) to activate; match alone (either tier) is insufficient — the later activation ADR decides what tier(s) any future promotion requires. |
| J | Same URL silently repoints to a different PVE environment | no | no | tier 1: an asserted mismatch, **only if a check is ever explicitly performed** — ordinary discovery does not perform one (§13); **tier 2, if checked, is meaningfully harder for the foreign environment to pass** — chain-verification requires the foreign leaf to actually be signed by the enrolled root CA, not merely a copied string (§10a) | The one gap this ADR's evidence class could detect, but only when explicitly triggered; see corrected §17/Case O. Tier 2, where available, hardens this detection without eliminating the trigger gap in §30.1. |
| K | Restore/recovery of `pmxcfs` data to replacement hardware | yes — CA material genuinely restored, not fabricated | **no** | an asserted match that happens to be genuine (real key material was restored) | §11 — genuine match is still never physical proof. |
| L | Full cloned PVE environment carrying copied `pmxcfs`/root CA | yes, on both clones, genuinely | **no**, and not unique — two live copies | an asserted match on either/both | Operator decision required; never auto-resolve which is "real" (§11). |
| M | Snapshot/backup cloning leaving the same CA live in two environments | yes (both), genuinely | **no** | an asserted match on either/both | Same conclusion as L; both may be transiently live (DR test). |
| N | Compromise/copy of CA private material, **or** a fabricated response with no key possession at all | attacker-controlled / indeterminate | no | an asserted match that is **entirely worthless as proof** — this is not limited to key theft; §5 shows a compromised key is not even required to fabricate matching evidence | Explicit threat-model limitation (§5, §9, §29); anchor evidence is not a defense against either key exfiltration or response fabrication. |
| O | Backend DB restore independently of PVE restore | unknown — the restored enrolled value may be stale relative to current PVE state | n/a | **none, until an attestation-gated action explicitly triggers a fresh read** — ordinary discovery does not (§13) | Ordinary read-only inventory continues unaffected (accepted limitation); stale attestation state is not auto-revalidated. See corrected §13/§17. |
| P | Backend DB clone and PVE clone together | yes (paired clone) | no | an asserted match, independently on each paired clone | Each clone considers itself attested to its own paired PVE clone; flagged unresolved (§30; see also `0.5-foundation.md` on `backend_instance_id` reinstall semantics). |
| Q | Loss of current endpoint, only an unattested new endpoint reachable | unknown until checked | unknown | none obtained (no check performed by default) | Stays `source_unavailable`; no automatic promotion regardless of any evidence (§29 negative witness 1). |
| R | Pre-attested candidate endpoint before primary failure | yes, as of the epoch it was checked against | unknown | tier 1: an asserted match recorded at a specific epoch (§14); **tier 2, where available, could be recorded alongside it** as stronger prerequisite corroboration (§10a) | Necessary but declared **not sufficient** for automatic promotion, regardless of tier; left to a future activation ADR to decide what tier(s) it requires (§15, §30.4). |
| S | Standalone↔cluster transition, anchor unchanged / anchor changed | per sub-case, entirely independent of the mode transition | unaffected by the mode transition | independent of `baseline_mode` entirely (§25, G8) | Mode transition itself never triggers, and is never triggered by, attestation logic. |
| T | Trust-domain mismatch during an in-flight discovery run | no, by definition | n/a | n/a to reconciliation, only to the fencing check | Run classified invalid/stale via CAS (§21, §19a). |
| U | Restart while an attestation/re-attestation operation is incomplete | prior committed value only | n/a | none accepted from the incomplete attempt | Atomic commit-or-nothing; no partial state (§19). |
| V | Stale worker/run using evidence from an older attestation epoch | stale, by definition | n/a | rejected | Same fencing discipline as `source_config_revision`/`transport_trust_revision` (§21, §29 negative witness 5); now also covers stale-epoch Blocker A/B evidence (§20, §26, §27, §29 negative witness 10). |

## 6. Stock PVE capabilities actually relied upon

Candidate primitive: `GET /nodes/{node}/certificates/info`, an authenticated,
read-only endpoint documented in the Proxmox VE API that returns certificate
metadata for a node, including `/etc/pve/pve-root-ca.pem` — the internal
cluster CA that `pmxcfs` maintains and distributes cluster-wide, and which
signs each node's default `pve-ssl.pem` unless the operator has replaced the
pveproxy-presented leaf certificate (ACME or custom). The response includes a
SHA-256 fingerprint for each certificate entry.

Two properties matter architecturally:

1. `pve-root-ca.pem` is a **cluster/standalone-node-wide** artifact
   maintained by `pmxcfs`, not a per-connection TLS negotiation artifact. It
   can be read as *data* over an already-authenticated API session,
   independent of which certificate that same session's TLS handshake
   actually validated against.
2. It survives events that rotate the pveproxy leaf certificate (routine
   renewal, ACME issuance/reissuance) because those events do not, by
   themselves, regenerate the cluster CA.

Required privilege: current upstream `pve-manager` API registration for
`certificates/info` declares `permissions => { user => 'all' }` — i.e. any
authenticated user or API token may call it, regardless of assigned
privilege or ACL. It is **not** gated by `Sys.Audit`, `VM.Audit`, or any
other specific privilege, unlike every endpoint already in ADR 0002's
`ENDPOINT_ACL_MATRIX`. An earlier draft of this ADR incorrectly assumed a
`Sys.Audit` requirement; that was wrong and is corrected here. This is
architecturally coherent, not a weakness: `/etc/pve/pve-root-ca.pem` is a
public certificate, not secret material, so exposing it to any
authenticated caller does not create a new confidentiality exposure the
way an unrestricted `Sys.Audit`-class read would.

This ADR still treats the exact request/response shape and exact PVE-version
support matrix as **implementation-package work**, not decided by this ADR.
The next package must independently re-verify the permission contract above
against the exact supported PVE 9.x tag, with the same FACT-DOC/FACT-SOURCE
discipline ADR 0002 uses for every other endpoint in its matrix (§30.3), and
must fail closed (a `configuration_error`-class outcome, §18) if a future
PVE release changes this contract or if the deployed release's support
cannot be confirmed.

**A second, stronger stock capability (Finding C of this revision).**
`pveproxy` normally serves `/etc/pve/local/pve-ssl.pem` /
`/etc/pve/local/pve-ssl.key` as its TLS leaf certificate/key — the node's
default certificate, which PVE generates and signs *using*
`pve-root-ca.pem`/`pve-root-ca.key` as the signing CA. If an operator has
instead configured a custom `pveproxy-ssl.pem`/`pveproxy-ssl.key` pair
(including via ACME), `pveproxy` serves that certificate instead, and it is
not, in general, signed by `pve-root-ca.pem` at all.

This means the certificate actually presented during the TLS handshake for
a given connection is, in the default (non-custom, non-ACME) configuration,
cryptographically chainable to `pve-root-ca.pem` using ordinary X.509 chain
verification — a check Hubinet Ops can perform itself against the already-
enrolled root CA material, not merely a value the peer asserts. §10a
develops this into a second, stronger evidence tier, complementing (not
replacing) the asserted-identifier evidence class above.

## 7. Candidate options considered

This ADR's recommended decision (§9) is not a single evidence primitive but
a **three-tier evidence model** (Finding C of this revision, §10a):

1. **Tier 1 — asserted PVE root-CA identifier evidence**: the
   `certificates/info` fingerprint read as ordinary API data (§6, §9).
   Operator-accepted asserted anchor identity — explicitly **not**
   cryptographic proof-of-possession. Always obtainable whenever the
   endpoint answers authenticated requests at all. Recommended as the
   baseline mechanism (§9).
2. **Tier 2 — cryptographic TLS-peer-to-enrolled-PVE-CA binding evidence**:
   whether the certificate actually presented in the already-completed TLS
   handshake cryptographically chain-verifies against the enrolled root CA
   (§6, §10a). Genuinely cryptographic and stronger than tier 1 — Hubinet
   Ops verifies this itself rather than trusting a self-report. Available
   exactly when that chain actually validates; **the normative test is the
   chain, never a certificate's "default"/"custom"/"ACME" label** — a
   typical ACME/public-CA leaf will not validate against the enrolled PVE
   root CA, and a typical custom pveproxy leaf may not either, but neither
   is categorically excluded or guaranteed by label alone (§10a). Recommended
   as an additional, optional corroborating check where the chain actually
   validates, never as a tier-1 replacement, and never sufficient by itself
   to authorize any state change (§15).
3. **Tier 3 — a future, stronger host-specific/Hubinet-managed
   attestation**: e.g. a dedicated Hubinet-issued enrollment secret
   provisioned out-of-band on the PVE host, or another mechanism giving
   properties beyond what tier 2 provides — such as a dedicated
   Hubinet-controlled host identity or stronger clone-resistance — though
   its exact properties remain unresolved (§9, §10a). This is the actual
   gap stock, read-only Proxmox VE API leaves open today — not an absence
   of cryptographic evidence altogether, since tier 2 is already
   cryptographic. Not rejected — flagged as the necessary complement to
   tiers 1/2's limitations, and as a possible *additional* evidence class in
   §30.5. Not designed here.

No new primitive at all — keeping endpoint replacement/failover permanently
unavailable — remains rejected as a permanent answer: it leaves operators
with no accepted path to ever add a second endpoint or recover from an
endpoint change, which the existing architecture already flags as a real
gap (ADR 0002 §"Nierozstrzygnięte kwestie" #6). Still the correct default
*until* this ADR (or a successor) is accepted.

A later activation/failover ADR decides which tier(s) are required for
which transition (§10a, §15) — this ADR fixes the tier definitions and
their honest strength, not an activation policy over them.

## 8. Rejected alternatives and why

- **URL/IP alone**: already explicitly rejected in ADR 0002
  ("stable/unchanged endpoint URL != proven physical source continuity").
  Does not survive DNS/reverse-proxy repoint (threat case J) or ordinary
  endpoint change (case H).
- **Cluster name**: not globally unique across independent networks (ADR
  0001), mutable only at cluster-creation time but not distinguishing, and
  already explicitly rejected as source-binding proof in ADR 0002.
- **Node name**: mutable/reusable across reinstall; ADR 0001's node section
  already establishes that node name never carries mutation trust across
  reinstall, for the identical reason.
- **`baseline_mode`**: a per-run observed presentation fact, not identity —
  this is exactly this wave's G8 closure (Part 1). Explicitly rejected here
  for the same reason.
- **Leaf TLS fingerprint alone**: rotates on ordinary CA-valid renewal
  (ADR 0002 already classifies routine renewal as "peer observation change,
  not source identity, not trust-policy revision") and changes completely on
  ACME/custom certificate replacement (case F) even though the underlying
  environment is unchanged. Using it alone would force spurious
  re-attestation on routine, benign events, and is exactly the axis
  `transport_trust_revision` already owns — conflating it with trust-domain
  identity would violate §3's "no reinterpretation of
  `transport_trust_revision`" boundary.
- **Arbitrary topology/resource hashes (VMIDs, names, resource counts)**:
  ADR 0001's own candidate audit already rejects every one of these as
  identity for the identical reasons (rename, VMID reuse, clone all defeat
  them). Reusing them at the source level would be strictly weaker than the
  worst-rejected per-resource candidate.
- **Automatically trusting a newly reachable endpoint**: directly
  contradicts the accepted "automatic discovery endpoint failover =
  disabled" invariant. Rejected outright; this ADR does not touch that
  invariant.
- **Treating root CA equality as physical-host proof, or as cryptographic
  proof-of-possession**: both rejected — this is the central adversarial
  finding of this ADR. Root CA equality is, at most, an asserted identifier
  match reported as ordinary API data (§9); it proves neither which physical
  machine answered (§11) nor that the responding endpoint actually possesses
  any PVE CA private key material (§9, §10, §5's fabrication case).

## 9. Recommended decision

Adopt the PVE root CA (`/etc/pve/pve-root-ca.pem`) SHA-256 fingerprint,
retrieved via `GET /nodes/{node}/certificates/info` over the already
transport-authenticated session, as the **logical PVE trust-domain anchor**
candidate evidence class — as **tier 1, an operator-accepted asserted
anchor identifier** (§4, §7), never as tier 2 or tier 3 cryptographic
evidence.

This distinction is load-bearing and must not be blurred:

- the fingerprint is delivered as an ordinary JSON response field from an
  application-layer PVE API call, not as part of the TLS handshake itself;
- nothing cryptographically binds that field's truthfulness to the
  identity/key material actually used to negotiate the surrounding HTTPS
  session — the transport handshake proves possession of the negotiated
  *leaf* certificate's private key (standard TLS security), and that proof
  does **not** extend to a claim made in a separate API payload;
- consequently, a responding endpoint that has never possessed any PVE CA
  private key material can still return a fingerprint value that matches a
  legitimately enrolled one — by copying it from a prior legitimate
  observation, from clone/restore (§11), or simply by fabricating the string
  in a simulated/malicious API implementation (§5). This is strictly weaker
  than the private-key-compromise threat class already in scope — it
  requires no key theft at all.

Stock, read-only Proxmox VE API provides **no primitive that proves
possession** of `pve-root-ca.key` (or of any other private key) as part of
serving this endpoint. This is a genuine limitation of what this ADR can
deliver from stock capabilities alone, not an oversight: closing it requires
either (1) a stronger, explicitly out-of-band operator verification step
(e.g., an operator manually cross-checking the fingerprint through a
separately trusted channel before accepting an enrollment/re-attestation),
or (2) a future, separately designed, stronger Hubinet-managed cryptographic
host/endpoint attestation primitive (§7 option 3, §30.5) that this ADR does
not design.

Given that limitation, root CA equality across an enrollment and a later
observation proves **only** "the two reads returned the identical asserted
identifier, and the peer that returned the later one completed ordinary
transport authentication" — nothing about actual CA possession, and nothing
about physical identity (§10, §11). It is, at most, weak corroborating
evidence that an operator may choose to trust; it is explicitly **not
sufficient by itself** to authorize any state change on its own logical
merits — every attestation-gated transition requires the same explicit
human enrollment/re-attestation act that established the anchor in the
first place (§18, §29 negative witness 1), and that human act is where the
actual trust decision is made, not the fingerprint comparison.

## 10. Exact trust semantics of the PVE root CA anchor

What a matching anchor read **does** establish, within the stated threat
assumptions:

- that the endpoint, over an already transport-authenticated session,
  **asserted** the identical root-CA fingerprint value as the one
  previously enrolled for this `inventory_source_id` — an
  operator-comparable identifier match, delivered as ordinary API response
  data;
- that, *if* the assertion is honest and the responding software is
  genuine, unmodified PVE software (an assumption this primitive cannot
  itself verify — §9), the two reads are consistent with the responding
  instance belonging to the same `pmxcfs`-replicated cluster/standalone
  filesystem lineage as the one enrolled.

What a matching anchor read explicitly does **not** establish:

- that the responding endpoint actually **possesses** the PVE CA private
  key, or is genuine `pmxcfs`-backed PVE software at all — the value is
  ordinary API response data with no cryptographic binding to the transport
  session's own proof-of-possession (of the *leaf* certificate only); a
  foreign, simulated, or otherwise non-genuine endpoint that has merely
  observed, copied, or fabricated a legitimate fingerprint string can return
  the identical value without owning any PVE private key material (§9, §5).
  This holds regardless of whether the endpoint's leaf certificate is
  internal-CA-signed or ACME/custom (case F) — the two are cryptographically
  unrelated once the value is read as payload data;
- that the request travelled to the same physical or virtual machine (§11);
- that no restore, clone, key exfiltration, or outright fabrication has
  occurred (§5, §9, §29);
- that the endpoint's *content* (guests, nodes, ACLs) has not diverged —
  attestation is a transport/identity-domain concept, never inventory
  content evidence;
- any resource-level (`security_continuity`) or destructive/management
  authority (§28);
- anything about physical uniqueness — see §11;
- authority carried over from an earlier attestation epoch — a match at the
  *current* epoch says nothing about the validity of evidence recorded under
  a *prior* epoch (§20, §26, §27).

Everything in this section describes **tier 1** evidence only (§7). §10a
describes a second, cryptographically stronger evidence tier and is
explicit about where its strength stops.

## 10a. Stronger optional evidence: TLS-peer-chain-to-enrolled-CA (tier 2)

**NEW ARCHITECTURAL DECISION, Finding C of this revision.** §9/§10 correctly
establish that the `certificates/info` fingerprint alone is not
cryptographic proof of anything. That does not mean stock PVE offers no
stronger endpoint evidence at all — it means the stronger evidence lives
elsewhere: in the TLS handshake itself, not in a self-reported API field.

**The mechanism, and the normative test.** As established in §6: `pveproxy`
normally presents `/etc/pve/local/pve-ssl.pem`, a leaf certificate PVE
itself generates and signs using `pve-root-ca.pem`/`pve-root-ca.key`, unless
the operator has configured a custom `pveproxy-ssl.pem`/`.key` pair
(including ACME). That default/custom distinction explains *why* a chain
might or might not validate, but it is **not** the test this ADR uses.
**The normative test is exclusively: does the certificate actually presented
in the current TLS handshake cryptographically chain-verify against the
enrolled PVE root CA — yes or no?** When it does, that constitutes **tier 2
evidence**, regardless of whether the certificate happens to be labeled
"default," "custom," or "ACME":

```text
tier 1 (§9/§10): peer ASSERTS a fingerprint value as API response data
                  — Hubinet Ops trusts the assertion, or does not

tier 2 (this section): Hubinet Ops CRYPTOGRAPHICALLY VERIFIES the peer's
                  already-negotiated leaf certificate chains to the
                  enrolled root CA — a check Hubinet Ops performs itself,
                  not a value the peer merely states
```

The TLS handshake proves possession of the *leaf* certificate's private key
(ordinary TLS security). Chain-verifying that leaf against the enrolled
root CA is a stronger, independently-computed claim than tier 1: it is not
defeated by an endpoint that can merely echo a copied or fabricated string
in a JSON payload (§5, §9's fabrication threat), because producing a leaf
that chain-verifies against a specific CA requires that CA to have actually
signed it.

**What tier 2 still does NOT prove — precise limitations, as required:**

- it does **not** prove the responding endpoint possesses
  `pve-root-ca.key` itself — only that *some* certificate it presents was
  signed by whoever *did* possess it at signing time; the endpoint is
  proving possession of the leaf's key, not the root's;
- it does **not** prove physical-machine uniqueness — a full clone that
  carries both `pve-root-ca.pem`/`.key` *and* `pve-ssl.pem`/`.key` (§11)
  satisfies tier 2 identically to the original, for the same reason tier 1
  does not distinguish clones;
- **CA compromise defeats it**: an attacker who has copied
  `pve-root-ca.key` (§5) can mint an arbitrary number of leaves that chain-
  verify correctly — tier 2 raises the bar from "copy a string" to "possess
  or derive from the CA key," but does not eliminate the key-compromise
  threat class;
- **leaf-key compromise alone can impersonate that one node**: an attacker
  who has only stolen `pve-ssl.key` (without the root CA key) can still
  complete a chain-verifying handshake for that specific leaf, until it is
  revoked/rotated — tier 2 evidence is peer-specific, not source-domain-
  proof independent of the specific node compromised;
- it does **not** replace, substitute for, or grant workload/node `hostd`
  trust (ADR 0001's node attestation, §28) — it remains scoped to the
  source-attestation question this ADR designs, nothing else.

**When the actual chain fails — tier 2 is unavailable for that connection,
regardless of label (case F).** Whenever the certificate actually presented
in the current TLS handshake does **not** cryptographically chain to the
enrolled PVE root CA, tier 2 evidence cannot be produced for that
connection, period — independent of why: a typical ACME/public-CA leaf will
not chain-verify against the enrolled PVE root CA, and a typical custom
`pveproxy-ssl.pem`/`.key` pair may not either, but **this ADR does not
categorically assert that every custom certificate can never chain to the
enrolled PVE root CA** — an operator's custom leaf could, in principle, itself
be issued from that same root CA and would then satisfy the actual test
above. The label is never the test; the chain-verification result is.
Critically: **a `certificates/info` payload independently claiming the same
enrolled root CA fingerprint does NOT repair a failed chain.** Tier 1 and
tier 2 are not substitutable — a tier-1 match, when the actual chain fails,
proves exactly what §9/§10 already say a tier-1 match proves (an asserted
identifier match, nothing cryptographic), never a chain proof by
association. In that configuration the system has, and only ever has: tier
1 asserted-anchor evidence, explicit out-of-band operator verification, or
a future tier 3 primitive (§7).

**This does not authorize activation/failover, and does not reinterpret
`transport_trust_revision`.** Tier 2 availability is strictly more evidence
than tier 1 alone, but this ADR does **not** decide that tier 2 alone is
sufficient for any endpoint-identity state change — exactly as tier 1 alone
is declared insufficient in §9. The later, separately accepted
activation/failover ADR (§15) decides which tier(s), if any, are required
for candidate promotion or failover; this ADR only defines what each tier
honestly proves. `transport_trust_revision` (§23) is unchanged and is not
reinterpreted by this section: it continues to mean the TLS trust *policy*
that governs whether a connection is authenticated/accepted at all (which
CA roots are trusted, pinning, verification policy). Source attestation may
*additionally* consume the already-observed peer certificate from an
already-successful connection as evidence and independently chain-verify it
against the enrolled PVE CA — a related but architecturally separate check,
performed after transport authentication has already succeeded, asking a
different question ("does this already-trusted peer's certificate chain to
the environment I enrolled?" rather than "should this connection be allowed
to proceed at all?").

## 11. Explicit clone/restore limitation

`pmxcfs`'s persistent database is `/var/lib/pve-cluster/config.db`; `/etc/pve`
is the `pmxcfs`-managed mount that exposes its content as files, including
the public `/etc/pve/pve-root-ca.pem` and, readable only with root/priv
access, `/etc/pve/priv/pve-root-ca.key`. (An earlier draft of this ADR
incorrectly wrote `/etc/pve/config.db`; that path does not exist and is
corrected here.) Restoring or cloning `config.db` — to replacement hardware
after a hardware failure (case K), to a full duplicated environment (case
L), or via snapshot/backup cloning that leaves two live copies (case M) —
reproduces the CA material, including the private key, exactly. Two, or
more, simultaneously reachable environments can therefore legitimately, and
*genuinely* (with real key material, not fabricated evidence), present the
**identical** root CA fingerprint.

This is a distinct limitation from §9/§10's proof-of-possession gap, and the
two must not be collapsed into one: §9/§10 shows the anchor evidence is not
cryptographic proof at all, so it can be matched even by an endpoint with
**no** genuine key material (pure fabrication). This section shows that,
even in the case where the match *is* genuine (real key material, actually
possessed, actually restored/cloned), it **still** does not identify a
unique physical machine, because clone/restore can legitimately duplicate
that possession across multiple machines.

**Same PVE root CA fingerprint MUST NOT be treated as proof of the same
physical machine**, whether the match is fabricated (§9/§10) or genuine
(this section). It is not disproof of a legitimate restore/clone either —
both are simply outside what this evidence class can distinguish. Any
future implementation package must treat two simultaneously live matches
against the same enrolled anchor as an operator-decision case, never as an
automatic "which one is real" resolution (§29 negative witness 4).

## 12. Initial source enrollment procedure

Atomic initial source creation (already accepted, ADR 0002/`0.5-inventory-
model.md`) is unaffected: it still creates exactly one `inventory_source_id`,
one initial active endpoint, and one initial non-fresh
`source_runtime_health` record, with `published_state_revision` advanced.

This ADR adds a source-attestation state that starts at
`attestation_status = not_yet_attested`, `source_attestation_epoch = 0` (or
an equivalent explicit initial sentinel), with no anchor value recorded.
`not_yet_attested` is a legitimate, long-lived, non-blocking state for
ordinary single-endpoint read-only discovery (§3 last bullet). It is **only**
a blocking state for any future attestation-gated action (§21, §29 negative
witness 1).

A separate, explicit, audited **enrollment** operation — issued by an
operator/backend actor, never by discovery itself — reads current anchor
evidence from the currently active endpoint over its already-validated
transport, then, in a **separate** authoritative step, records it as the
enrolled anchor value/evidence kind and transitions
`attestation_status: not_yet_attested → attested` while incrementing
`source_attestation_epoch` to `1`. The remote evidence read and the
authoritative database write are two distinct steps, not one atomic
operation — see §19a for the exact concurrency pattern this must follow,
which mirrors the existing controlled source/endpoint/transport transitions
(ADR 0002 §"controlled source config/active route/canonicalization/TLS
trust transition") and must serialize with active discovery-run ownership
exactly like those transitions do (§21).

Initial enrollment's `0 -> 1` transition is, precisely because it is an
epoch bump, one instance of the controlled source security-context
transition defined in §20's "Freshness-context participation" — even though
it is the *first* attestation event for this source. Concretely: if a
discovery run already committed successfully while the source was still
`not_yet_attested` (epoch `0`) and the source is currently `healthy/fresh`,
the same enrollment write transaction that bumps the epoch to `1` must
atomically mark current health `stale`, with
`current_health_origin=controlled_context_transition`, invalidate mutation
freshness, and advance `published_state_revision` — exactly as it would for
any later re-attestation epoch bump. It must not remain possible to treat
inventory committed under epoch `0` as fresh under epoch `1` merely because
epoch `0` predates attestation existing at all (§20).

## 13. Existing endpoint reconnect behavior

Ordinary reconnects to the same already-active `endpoint_id` (transient
network loss, restart, routine leaf-certificate renewal under an unchanged
`transport_trust_revision` policy) are **not** attestation events. They
continue to be governed entirely by the existing endpoint/health/freshness
contract (ADR 0002). This ADR does not add an attestation check to every
ordinary discovery run of the sole active endpoint; whether it *should*
eventually is an explicitly open question, not decided here (§30.1).

This has a direct, important consequence, corrected by this revision (case
O, §5a): because ordinary discovery never re-reads attestation evidence, a
stale or already-mismatched `attestation_status`/enrolled anchor value
(e.g. after an independent Hubinet Ops backend database restore) is **not**
automatically detected or revalidated by continuing read-only discovery. It
simply persists, unrevalidated, until an operator explicitly triggers an
attestation-gated action (candidate check §14, re-attestation §16), at
which point a fresh read happens and §17/§18 apply. Ordinary read-only
inventory presentation is unaffected either way — this is the accepted
single-endpoint observational limitation ADR 0002 already documents,
unchanged by this ADR.

## 14. Candidate endpoint enrollment

An operator who wants to prepare a second endpoint (different node/URL)
against the same `inventory_source_id` may explicitly request an
**endpoint-scoped attestation check**: the backend reads anchor evidence
from that candidate endpoint (requiring its own already-validated transport
trust, independent of the active endpoint's) and compares it against the
source's current enrolled anchor at the source's *current*
`source_attestation_epoch`.

The read (against the candidate) and the write (recording the binding) are
two separate steps, following the same concurrency pattern as enrollment —
see §19a, which this operation must use identically ("equivalent fencing to
candidate endpoint attestation"), **including** §19a's candidate-specific
requirement that the candidate's exact lifecycle/admissibility state is
captured before the read and re-validated inside the write transaction, so a
candidate retired mid-flight cannot still receive a binding.

A match records an **epoch-scoped candidate attestation binding**:
`(endpoint_id, source_attestation_epoch, matched_at)`. This binding is valid
only for the exact epoch it was taken against; if the source is later
re-attested with a bumped epoch (§16, §20), every existing candidate binding
becomes stale and must be redone (§29 negative witness 7). A mismatch does
**not** attest the candidate, does not affect the source's own attestation
status, and does not affect the candidate endpoint's existing `candidate`
lifecycle status (ADR 0002) in any way beyond leaving it un-attested.

This binding is a **prerequisite record**, not an authorization. It does not
by itself change `EndpointLifecycle`, does not activate anything, and does
not participate in discovery.

## 15. Endpoint replacement/failover prerequisites

This ADR asserts, but does not itself authorize implementing, the following
prerequisite relationship for any future activation/failover ADR/contract:

```text
candidate/inactive -> active transition
  requires >= existing accepted source-binding gate (ADR 0002)
  requires an epoch-scoped candidate attestation binding at the exact
    current source_attestation_epoch (this ADR)
  requires the explicit operator/backend action that a future
    activation-specific ADR must still separately define
```

A matching attestation is **necessary** for any future candidate promotion
or automatic failover to be considered safe, but this ADR does **not**
decide that it is **sufficient**, and does not authorize implementing
promotion or failover. Whether a pre-attested candidate (threat case R) may
ever be *automatically* promoted on primary loss remains explicitly
unresolved and out of this wave's boundary (§30.4). The safest minimal
procedure this ADR anticipates — explicit, operator-triggered promotion of
an already epoch-scoped-attested candidate, never fully automatic — is
noted as a direction for that future ADR, not decided here.

## 16. Re-attestation procedure

Re-attestation is the same explicit, audited operation as initial
enrollment (§12), issued against an already-`attested` source, and follows
the identical read-then-write concurrency pattern (§19a). It:

- reads fresh anchor evidence from the endpoint the operator specifies
  (normally the current active endpoint);
- if it matches the currently enrolled anchor value, records the
  re-attestation as an **audit event only** — `source_attestation_epoch`
  does **not** bump on a same-value reconfirmation;
- if it does not match, does **not** silently accept the new value. It
  requires the operator to explicitly choose one of: (a) accept the new
  anchor as a deliberate environment change (e.g., planned CA regeneration,
  case G) — which bumps `source_attestation_epoch` and records the new
  anchor with full audit of the prior value, or (b) reject/investigate,
  leaving the source's current epoch/anchor unchanged, with the mismatch
  itself recorded as its own audited outcome (§17).

§20 fixes this deterministic scheme as the epoch/revision contract, not an
implementation choice: reconfirming an unchanged value must never bump the
epoch (doing so would needlessly invalidate every candidate binding and, per
§26/§27, cut off all Blocker A/B authority evidence for no security reason),
while accepting a genuinely changed value, or an explicit revocation/reset,
always must — and, whenever it does, that same write additionally performs
the controlled source security-context transition defined in §20's
"Freshness-context participation": current health becomes stale, mutation
freshness is invalidated, and `published_state_revision` advances, exactly
mirroring the already-accepted controlled source/endpoint/transport-trust
transition pattern. A same-value reconfirmation, having no epoch bump,
correspondingly triggers none of that — it is audit-only, and current
health/freshness is left completely untouched.

Re-attestation must serialize with active discovery-run ownership exactly
like other controlled context transitions (ADR 0002 pattern): at an active
run, the implementation either waits for its terminal release or atomically
fences it, before completing (§21).

## 17. What happens on anchor mismatch

An anchor read that unambiguously disagrees with the currently enrolled
value (for a candidate check, §14, or for any future continuous check,
§30.1) transitions the *evaluated relationship* (never automatically the
source itself) to `mismatch_pending_reattestation`-class evidence. As with
every other outcome here, "disagrees" means the returned assertion differs
from the enrolled value — this is still evidence at the asserted-identifier
level (§9, §10), not a cryptographically-verified mismatch:

- it does **not** create a new `inventory_source_id`;
- it does **not** revoke or otherwise touch any resource's
  `security_continuity` (Blocker B remains entirely separate, §27, §28);
- it does **not** by itself stop ordinary discovery on the still-currently
  active endpoint (that would only be added by the explicit continuous-check
  extension left open in §30.1, and even then must be an explicit,
  documented fail-closed decision, not a silent one) — note that today, with
  no continuous check, a mismatch is only ever detected when an operator
  explicitly triggers a check (§13, corrected case O in §5a);
- it **does** block every attestation-gated action (candidate binding
  validity, any future promotion) until an operator explicitly resolves it
  via re-attestation (§16) accepting the new anchor, or explicitly creating
  a separate new source instead (mirroring ADR 0002's existing rule that an
  operator who cannot present source-binding proof "must create a new
  `inventory_source_id` with its own initial active endpoint").

Mismatch is evidence that continuity is **unproven**, not evidence of
replacement and not evidence of continuity. This mirrors ADR 0001's
observational-ambiguity rule exactly: ambiguity alone never manufactures a
new identity and never silently preserves trust either.

## 18. What happens if evidence cannot be obtained

A failed, malformed, ambiguous (multiple root CA entries where one is
expected), or unauthorized read of anchor evidence is a **third outcome**,
distinct from both match and mismatch:

- it must never be treated as an implicit match (that would let a transient
  read failure be indistinguishable from "trust confirmed" — a fail-open
  bug);
- it must never be treated as an implicit mismatch either (that would make
  transient unavailability equivalent to an authoritative "different
  environment" finding, and could be used to force spurious
  re-attestation/denial-of-service against a legitimately unchanged source);
- it blocks only the specific attestation-gated action that requested the
  evidence, and leaves the source's existing `attestation_status` and
  `source_attestation_epoch` completely unchanged;
- it must be recorded as its own audited outcome (not silently dropped),
  exactly as ADR 0002 already requires for provider read failures
  (`configuration_error`/`partial` classes), so operators can distinguish
  "never checked," "checked and matched," "checked and mismatched," and
  "checked and could not be evaluated."

## 19. Restart behavior

Attestation/re-attestation is an explicit, operator-triggered, short-lived
operation (§12, §16), not a long-running background process, so it does not
need a `discovery_runs`-style multi-phase issued/running/completed lifecycle
of its own. Its authoritative *write* (§19a step 3-6) must still be
**atomic and crash-safe**: the transaction that revalidates expected context
and writes the new `attestation_status`/`source_attestation_epoch`/enrolled-
anchor-value must commit all of it together or none. A process crash before
that write transaction commits leaves the prior committed attestation state
entirely unchanged after restart — there is no partial "epoch bumped but
anchor not recorded" state to recover, because the remote evidence read
(§19a steps 1-2) that precedes the write carries no durable state of its
own to abandon/fence. If a future implementation package introduces a
longer-running verification step (e.g., multi-endpoint quorum reads before
a single write), it must reuse the existing issued/running/abandoned-on-
restart pattern from `discovery_runs` rather than inventing a second one
(§30.3).

## 19a. Concurrency pattern: remote evidence read vs. authoritative DB write

**Normative for any future implementation package** (Finding 6 of this
revision): a remote HTTP evidence read must never be modeled as if it were
itself part of one atomic SQLite transaction. Attestation evidence reads are
slow, untrusted, network I/O; holding a database write transaction open
across them would block unrelated writers and turn a network stall into a
database-availability problem. Every attestation-gated evidence read (initial
enrollment §12, re-attestation §16, candidate endpoint attestation §14) must
follow this exact sequence, which mirrors — and reuses, not reinvents — the
existing `discovery_runs` issuance/commit pattern from ADR 0002:

```text
1. canonically capture expected context: current source_config_revision,
   endpoint_id, canonical transport locator, canonicalization_contract_version,
   transport_trust_revision, and source_attestation_epoch, read together as
   one consistent snapshot (a single read, not a held write transaction)
2. perform the remote evidence read (GET /nodes/{node}/certificates/info)
   against the target endpoint, entirely outside any DB write transaction
3. enter the authoritative DB write transaction
4. inside that same transaction, re-validate every exact expected-context
   field captured in step 1 against its current value (CAS) — exactly like
   discovery_runs commit revalidates its own expected context
5. if step 4 holds: persist the evidence/decision/epoch transition (§20)
   in that same transaction, and commit
6. if step 4 does not hold (any expected-context field changed between
   step 1 and step 4): classify the attempt as stale, write no accepted
   attestation transition, and record it as its own audited outcome (§18) —
   exactly like a discovery run with a mismatched expected context is
   classified invalid/stale and never reconciled (ADR 0002)
```

A stale classification at step 6 is not a security failure by itself — it
means a concurrent controlled context transition happened between the read
and the write, and the operation must simply be retried against the new
context. This pattern applies identically to candidate endpoint attestation
(§14) — "equivalent fencing," as required — with the candidate's own
`endpoint_id`/transport substituted for the active endpoint's.

**For candidate endpoint attestation specifically (§14)**, step 1's captured
expected context must additionally include the candidate endpoint's exact
lifecycle/admissibility state (e.g. `candidate`) at the moment the check
starts, and step 4's revalidation must confirm that state still holds before
accepting the binding. A candidate that is retired, or otherwise made
ineligible, while the remote evidence read is in flight must not receive a
newly accepted candidate-attestation binding merely because `endpoint_id`,
canonical locator, and `transport_trust_revision` happened not to change —
lifecycle eligibility is an independent axis from those and must be
independently fenced. This ADR does not prescribe a specific mechanism
(no SQL column is decided here); the next implementation package may satisfy
this with an exact lifecycle/status CAS, an endpoint-local monotonic control
revision, or any equivalent authoritative transaction check, as long as it
is checked inside the same write transaction as step 4, not merely before
it. This requirement is bounded to candidate-attestation admissibility and
does not design, and must not be read as designing, endpoint activation
itself (§15).

## 20. Source-attestation epoch/revision semantics

`source_attestation_epoch` is a new, source-owned, monotonic token, a peer
of `source_config_revision` and `transport_trust_revision`, never derived
from either and never derived from the anchor value.

**Deterministic epoch transition rule** (Finding 7 of this revision — this
is a fixed architecture decision, not an implementation-package choice,
because epoch changes invalidate candidate bindings and gate all future
Blocker A/B authority evidence below):

```text
initial enrollment (§12):            epoch 0  -> 1
same-anchor reconfirmation (§16):     audit event only, epoch unchanged
accepted anchor-value change (§16):   epoch N -> N+1
explicit revocation/reset that
  changes authority context:         epoch N -> N+1
```

A pure reconfirmation must never bump the epoch: doing so would needlessly
invalidate every existing candidate binding (§14) and, per the authority
rule below, cut off all Blocker A/B evidence, for zero security benefit
since nothing about the trust context actually changed. A genuine anchor
value change, or an explicit revocation, always must bump it — consistent
with ADR 0001's `resource_continuity_revision` rule that a security-relevant
continuity decision always advances its own token.

General properties: the epoch starts at an explicit initial sentinel (`0`,
equivalent to `not_yet_attested`); it is never decremented, never reused,
never derived from wall-clock time; it never increments merely from a read
(§19a steps 1-2) — only from an accepted write (§19a step 5).

### Authority-eligibility rule (normative; Finding 2 of this revision)

`source_attestation_epoch` is not only a fencing token for `discovery_runs`
and candidate bindings (§21) — it is the fencing token for **every**
security-sensitive evidence artifact whose validity depends on source
trust-domain continuity, including future Blocker A absence evidence and
future Blocker B workload-continuity/enrollment evidence. This ADR fixes
the following rule as normative now, even though neither Blocker A nor
Blocker B evidence exists yet:

```text
every security-sensitive evidence artifact that depends on source
  trust-domain continuity must record the exact source_attestation_epoch
  under which it was established

an epoch bump (accepted anchor change or explicit revocation) retains all
  historical evidence recorded under prior epochs, unchanged, in full audit

an epoch bump makes evidence recorded under any prior epoch INELIGIBLE for
  authority under the new epoch, until a separately accepted procedure
  (owned by Blocker A's or Blocker B's own future ADR, not this one)
  explicitly revalidates and carries it forward

a source-attestation epoch bump does NOT automatically manufacture a new
  resource_id, does NOT automatically assert resource replacement, and does
  NOT by itself change any resource's presence/lifecycle/observational
  continuity — ordinary reconciliation (ADR 0001/0002) is entirely
  unaffected by an epoch bump; only the AUTHORITY-ELIGIBILITY of trust
  evidence tied to the old epoch is cut off
```

Worked witness (the one from §5's threat model): a resource reaches
`security_continuity=trusted` under some future Blocker B contract while
`source_attestation_epoch=7`. An operator later accepts a changed PVE
anchor; `source_attestation_epoch` becomes `8`. The same VMID/type is
observed again and ordinary reconciliation legitimately retains the same
`resource_id` (ADR 0001 — reconciliation identity and attestation epoch are
independent axes, exactly like `baseline_mode`, §25). The epoch-7 trust
fact is **not deleted** — it remains a retained historical record — but it
is **not authority-eligible** under epoch 8. Any future mutation-eligibility
check that (once Blocker B is designed) would rely on `trusted` continuity
must additionally check that the trust evidence's recorded epoch equals the
source's *current* `source_attestation_epoch`, or that an explicit
epoch-carry-forward procedure accepted it. Until Blocker B defines that
carry-forward procedure, the safe default is that epoch-7 trust simply
cannot authorize anything once the epoch has moved to 8 — re-enrollment of
workload trust is required, exactly as if the resource had never been
trusted under the new epoch.

**Representation boundary (Finding 1 of this correction pass — narrower
than an earlier draft of this ADR, which wrongly left this as a free
choice).** This ADR does not, and cannot, change ADR 0001 (§2 non-goal);
ADR 0001 remains the sole owner of the canonical `security_continuity`
vocabulary (`unverified`, `trusted`, `revoked`) and of
`resource_continuity_revision`'s transition rules. ADR 0001 defines
`trusted` as meaning the required accepted continuity proof **remains
valid**, and `revoked` as covering prior trust whose proof was withdrawn
**or ceased to be valid**. Source-attestation-epoch-dependent proof that
falls out of scope when the epoch advances has, by ADR 0001's own
definition, ceased to be valid. It follows that leaving the stored value
`trusted` while merely computing a separate "effective" value at decision
time is **not** a legitimate representation under ADR 0001 — it would mean
the canonical field asserts something ADR 0001 says is no longer true. This
ADR therefore fixes the mandatory default:

```text
IF a resource currently has security_continuity=trusted whose required
   proof depends on source_attestation_epoch, AND source_attestation_epoch
   advances past the epoch that proof was established under,
THEN, UNLESS an accepted Blocker-B carry-forward/revalidation procedure
   atomically establishes or carries forward proof valid for the exact new
   epoch (and itself satisfies ADR 0001):

     security_continuity: trusted -> revoked
     resource_continuity_revision increments
       (ADR 0001's existing rule: every security-relevant continuity
       decision advances this token)

   the superseded old-epoch evidence remains retained, unchanged, for audit
   — it is not deleted, only no longer authority-eligible (as already
   established above)
```

This transition is a security-continuity decision belonging to ADR 0001's
own transition rules, not a source-attestation-owned identity event. It
therefore, by itself, MUST NOT:

- create a new `resource_id`;
- close or otherwise change the active `binding_id`;
- change `locator_generation`;
- assert or constitute positive replacement evidence;
- change `presence`;
- change `observational_continuity`.

A resource whose `security_continuity` was `unverified` (never `trusted`)
remains `unverified` through an epoch bump — nothing here promotes or
demotes it — unless some independent ADR 0001 rule requires otherwise. This
ADR does not invent, and no future Blocker B implementation may invent, a
new canonical `security_continuity` value (e.g. no "epoch-stale" enum
member): the mandatory default above is expressed entirely inside ADR
0001's existing three-value vocabulary. If a future Blocker B design
genuinely needs a state that vocabulary cannot express, that requires its
own separate, explicit amendment to ADR 0001, reviewed and accepted on its
own merits, **before** such an implementation — not something this ADR
pre-authorizes or that Blocker B may introduce unilaterally.

A future, separately accepted Blocker B carry-forward/revalidation
procedure remains the only way to *avoid* this default: it may preserve or
restore `trusted` continuity across an epoch boundary, but only by actually
establishing proof valid for the **exact** new epoch, and only in a way
that itself satisfies ADR 0001's state matrix and revision rules — never by
retroactively excusing evidence that was valid for the old epoch alone.

A source-attestation epoch bump still does not, by itself, manufacture a
new `resource_id` or constitute positive replacement evidence, independent
of the security-continuity transition above (§20 above, §29 negative
witness 11) — discovered inventory identity/presence/facts owned by ADR
0001/0002's own reconciliation rules remain entirely theirs; only the
backend-owned `security_continuity`/`resource_continuity_revision` axis
transitions, and only exactly as described here.

**This ADR's normative contribution remains the authority rule plus this
mandatory default representation** — old-epoch evidence is not
authority-eligible in a new epoch, and a resource whose trust depended on
it reverts to `revoked` unless valid new-epoch proof is established —
expressed strictly inside ADR 0001's existing vocabulary and revision
rules, which Blocker A's and Blocker B's future ADRs must satisfy, not
re-litigate (§26, §27, §29 negative witnesses 10, 16, and 18, §30 item 7).

### Freshness-context participation (normative; Finding A of this revision)

`source_attestation_epoch` is part of the exact security-sensitive source
context for discovery/freshness provenance, a peer of
`source_config_revision`, `endpoint_id`, canonical locator/version, and
`transport_trust_revision` in every place ADR 0002 already tracks that
context. Concretely, and mirroring `0.5-inventory-model.md`'s existing
`committed_source_config_revision`/`committed_endpoint_id`/
`committed_transport_trust_revision` provenance exactly:

```text
source_runtime_health (conceptual) additionally records:
  committed_source_attestation_epoch
    — the exact source_attestation_epoch under which the last successful,
      applicable discovery commit was made (§21)

current health/freshness derivation additionally requires:
  committed_source_attestation_epoch == current source_attestation_epoch

  (exact equality, not merely >=, exactly like the existing
  committed_endpoint_id == current active endpoint_id requirement — any
  divergence means a context transition happened since the last commit)
```

**Every epoch bump — initial enrollment `0 -> 1` (§12), an accepted
anchor-value change, or an explicit revocation/reset (§16, §20) — is a
controlled source security-context transition**, of the same architectural
kind ADR 0002/`0.5-inventory-model.md` already define for a controlled
source-config/active-route/canonicalization/TLS-trust context transition.
In one atomic transaction, it must:

```text
1. serialize with active discovery-run ownership exactly like every other
   controlled context transition (ADR 0002 pattern, §21): either wait for
   the active run's terminal finalization/release, or atomically
   terminalize/fence it with a real reason and release ownership, before
   the transition proceeds — no new provider I/O may begin concurrently
   with the transition
2. update source_attestation_epoch (and, where applicable, the enrolled
   anchor value/evidence kind)
3. mark current source health stale, with
   current_health_origin=controlled_context_transition and an explicit
   reason identifying an attestation-context transition
4. invalidate mutation freshness
5. advance published_state_revision
6. leave inventory_revision UNCHANGED — the context change alone is not an
   inventory commit
7. leave every current *discovered inventory* fact/identity exactly as last
   reconciled — this transition performs no reconciliation, invents
   nothing, and removes nothing from `presence`, `lifecycle`,
   `observational_continuity`, `resource_id`, `binding_id`, or
   `locator_generation` (ADR 0001/0002 remain the sole owners of those).
   This carve-out does **not** extend to the backend-owned
   `security_continuity`/`resource_continuity_revision` axis: once Blocker B
   exists, an accepted anchor-change or revocation epoch bump additionally
   triggers the mandatory `trusted -> revoked` default defined in this
   section's "Representation boundary" above, for any resource whose
   `trusted` proof depended on the epoch being left behind — that is a
   security-continuity decision ADR 0001 already owns, not an inventory
   reconciliation fact this step protects
8. commit all of the above together, or none of it
```

Only a subsequent successful, authoritative discovery commit — issued under
`expected_source_attestation_epoch` equal to the new epoch, and finalized
without a context mismatch (§21) — may restore
`committed_source_attestation_epoch == current source_attestation_epoch`
and therefore fresh health, exactly as a normal successful commit restores
freshness after any other controlled context transition today.

A **same-anchor reconfirmation** (§16) never bumps the epoch and therefore
never performs this transition: an audit-only reconfirmation leaves
`source_attestation_epoch`, current health, `committed_source_attestation_
epoch`, and every revision token completely untouched, exactly as it must
per §16's deterministic scheme.

Concrete witness this closes: a discovery run commits successfully while
`source_attestation_epoch=0` (`not_yet_attested`) and the source is
`healthy/fresh`. An operator then performs initial enrollment, bumping the
epoch to `1`, with no new discovery run yet issued or completed. Before this
revision, nothing in this ADR prevented mutation/security logic from
continuing to treat that epoch-`0`-committed inventory as fresh under the
now-current epoch `1`. The rule above closes that gap: the enrollment
transaction itself, being an epoch bump, atomically marks health stale with
`current_health_origin=controlled_context_transition` — the inventory
remains retained and presented as last-known data, but is not mutation-fresh
again until a new successful commit is issued and finalized under exactly
`source_attestation_epoch=1`.

## 21. Discovery-run fencing interaction

Once implemented, `discovery_runs` issuance would capture an
`expected_source_attestation_epoch` alongside the existing expected
`source_config_revision`, `endpoint_id`, canonicalization contract, and
`transport_trust_revision` (ADR 0002's existing issuance-context shape).
Commit would revalidate it exactly like every other expected-context field
inside the same atomic CAS boundary: a mismatch classifies the run as
invalid/stale, exactly like an in-flight `source_config_revision` or
`transport_trust_revision` change does today. Before attestation exists
(`not_yet_attested`), this expected value is simply the initial sentinel and
imposes no additional constraint on ordinary single-endpoint discovery
(§3, §13) — this is how the "does not block base read-only inventory"
invariant survives the addition of this new token.

A **successful, applicable** commit does not merely check
`expected_source_attestation_epoch` against current — in the same atomic
reconciliation transaction that already records
`committed_source_config_revision`/`committed_endpoint_id`/
`committed_canonical_transport_locator`/
`committed_canonicalization_contract_version`/
`committed_transport_trust_revision` (ADR 0002), it must additionally record
`committed_source_attestation_epoch = expected_source_attestation_epoch`
(the exact epoch the run was issued and finalized under). This is the
provenance §20's "Freshness-context participation" compares against current
`source_attestation_epoch` to decide fresh-eligibility — recording only the
CAS check without this committed provenance would leave §20's freshness rule
unenforceable, since there would be nothing durable to compare a later
epoch bump against.

## 22. Relationship to existing `source_config_revision`

Independent tokens, never derived from one another. `source_config_revision`
protects the meaning of *controlled source/provider/discovery configuration*
(ADR 0002); `source_attestation_epoch` protects the meaning of *which
logical trust domain this source is currently bound to*. A pure
re-attestation with an unchanged accepted anchor does not, by itself, bump
`source_config_revision`. If an operator's remedy for a mismatch also
changes provider/discovery-relevant configuration (e.g., swapping
credentials for a rebuilt environment), that separately and independently
bumps `source_config_revision` under its own existing rule — not because of
the attestation decision itself.

## 23. Relationship to existing `transport_trust_revision`

Fully orthogonal, and this ADR does not reinterpret `transport_trust_revision`
in any way (§2 non-goal). `transport_trust_revision` versions the TLS trust
*policy* used to negotiate one endpoint's HTTPS session (leaf/pin/CA-roots/
verification policy). `source_attestation_epoch` versions the enrolled
*logical trust-domain anchor* for the whole source. Rotating a pinned leaf
fingerprint (a `transport_trust_revision` bump, case E/F) does not by itself
imply an anchor change, and an anchor change (case C, G, J) does not by
itself imply a `transport_trust_revision` bump. Both must independently hold
for any future attestation-gated action to proceed.

This includes tier-2 evidence (§10a): consuming the already-negotiated peer
certificate to chain-verify against the enrolled root CA is a *use* of data
that already passed transport authentication under the current
`transport_trust_revision` policy, asked as a separate question after the
fact — it is not a redefinition of what `transport_trust_revision` itself
governs (whether the connection is authenticated/accepted at all).

## 24. Relationship to canonicalization migration

None. Canonicalization (`canonical_transport_locator` +
`canonicalization_contract_version`) only concerns how raw URL text maps to
one immutable locator value; it is unrelated to trust-domain identity. A
canonicalization-version migration (ADR 0002) never changes, resets, or
otherwise touches `source_attestation_epoch`, and attestation state never
participates in the canonicalization retained-namespace uniqueness
constraint.

## 25. Relationship to `baseline_mode` / standalone-cluster transitions

Fully independent axis, confirmed by this wave's Part 1 (G8) closure:
`baseline_mode` is a per-run observed presentation fact and never affects,
and is never affected by, `source_attestation_epoch`. Threat case S from
§5's adversarial list is explicitly two independent evaluations: a mode
transition with an *unchanged* anchor has zero attestation effect (same
conclusion as G8); a mode transition that happens to coincide with a
*changed* anchor (e.g., case C, a standalone node silently joining a foreign
cluster) is purely an anchor-mismatch event, handled entirely by §17,
regardless of what `baseline_mode` reports.

## 26. Interaction with future Blocker A evidence

Authoritative interval-wide absence proof (ADR 0002's proof classes A/B/C
for `confirmed_removed`) remains entirely orthogonal to source attestation
at the *identity* level: attestation establishes trust-domain continuity for
the *source*, and says nothing about whether any individual resource is
present or absent. Ordinary presence/missing/`confirmed_removed`
determination (ADR 0001/0002) is unaffected by attestation epoch changes.

However, the §20 authority-eligibility rule is normative for the subset of
future Blocker A evidence whose own correctness assumption depends on
trust-domain continuity — for example, a future evidence class B (trusted
event/task-stream proof, ADR 0002 §"Kiedy dokładnie wolno ustawić
confirmed_removed") that assumes a continuous, uninterrupted cursor/stream
from one specific PVE trust domain would have that assumption broken by an
accepted anchor change; such evidence must not be cited across the
`source_attestation_epoch` boundary where it was established. Evidence
classes whose correctness does not depend on any trust-domain-continuity
assumption (e.g., class A, a durable Hubinet Ops-side backend-mediated
operation record) are not affected by this rule merely because attestation
exists — Blocker A's own future ADR decides, for each of its evidence
classes, whether trust-domain continuity is one of its assumptions, and if
so, must record and check the epoch exactly as §20 requires. This ADR does
not grant Blocker A any authority beyond that constraint, and does not
itself define which of its evidence classes are epoch-dependent.

## 27. Interaction with future Blocker B enrollment

Resource-level `security_continuity=trusted` (ADR 0001, "Blocker B" —
workload continuity/enrollment proof) remains a **completely separate**
trust object from source-level attestation: a trusted source attestation
never upgrades any resource's `security_continuity`, and a resource's
trusted continuity proof never substitutes for a missing or mismatched
source attestation.

They are separate *objects*, but not fully independent in authority once
Blocker B exists: the §20 authority-eligibility rule requires that
`trusted` continuity established under one `source_attestation_epoch` is
**not authority-eligible** once the source's current epoch has advanced past
it (§5's worked witness, §20's worked witness). This is the normative
authority rule fixed by this ADR now.

**Boundary with ADR 0001 (Finding 1 of this correction pass).** §20's
"Representation boundary" is the exact, binding statement of this
interaction and is not restated in full here to avoid drift; in summary,
because this ADR does not and cannot change ADR 0001 (§2 non-goal), and
because ADR 0001 defines `trusted` as meaning the required proof *remains
valid*: this ADR invents no new canonical `security_continuity` value; the
mandatory default, absent an accepted Blocker-B carry-forward procedure
establishing new-epoch-valid proof, is an ordinary ADR 0001
`trusted -> revoked` transition with its own `resource_continuity_revision`
bump — **not** a free choice between that and leaving the stored value
`trusted` while computing a separate effective value, which §20 now
excludes as inconsistent with ADR 0001's own definition of `trusted`. That
transition must not, by itself, touch `resource_id`, `binding_id`,
`locator_generation`, `presence`, or `observational_continuity` (§20). A
genuinely new state beyond ADR 0001's existing vocabulary requires its own
separate, explicit ADR 0001 amendment before implementation, not something
Blocker B introduces unilaterally.

## 28. Source attestation grants no workload/mutation authority

Explicit, non-negotiable statement, mirroring ADR 0001's "false continuity
must never transfer destructive authority": **source attestation does not
grant, and must never be implemented to grant, workload trust, management,
maintenance, policy applicability, or any destructive/mutation capability**,
for any resource on that source. It is exclusively a prerequisite for future
endpoint-identity decisions (candidate activation/failover), nothing else.
Discovery — attested or not — remains strictly read-only (ADR 0002) and
grants no capability by itself, unchanged.

## 29. Fail-closed rules and negative witnesses

Fail-closed defaults:

- `not_yet_attested` is the permanent default absent an explicit enrollment
  action; it never silently becomes `attested`.
- Every attestation-gated action requires the exact current
  `source_attestation_epoch` and an unambiguous match; anything else (no
  attestation, mismatch, evidence unavailable, stale epoch) blocks the
  action.
- No automatic promotion, no automatic new-source creation, no automatic
  trust revocation ever results from an attestation evidence read by
  itself; every state-changing consequence requires an explicit,
  audited human/operator decision.
- A remote evidence read never happens inside an open authoritative DB
  write transaction (§19a); a stale expected-context CAS at write time
  discards the read's result and accepts no attestation transition.
- Security-sensitive Blocker A/B evidence that depends on source
  trust-domain continuity is not authority-eligible once
  `source_attestation_epoch` has advanced past the epoch it was recorded
  under (§20, §26, §27); it is retained as historical audit, never deleted,
  but never cited as current authority.
- Every epoch bump — including initial enrollment `0 -> 1` — is a
  controlled source security-context transition that marks current health
  stale and invalidates mutation freshness in the same atomic transaction as
  the epoch change; inventory committed under an old epoch is never treated
  as fresh under a newer one, even though a same-anchor reconfirmation
  performs none of this (§20 "Freshness-context participation").
- No future Blocker B implementation may represent "trusted-but-epoch-stale"
  as a new canonical `security_continuity` value, and no implementation may
  leave the stored value `trusted` while its required proof is epoch-stale
  (that would assert something ADR 0001 already defines as no longer true).
  The mandatory default, absent an accepted Blocker-B carry-forward
  procedure establishing proof valid for the exact new epoch, is an
  ordinary ADR 0001 `trusted -> revoked` transition with a
  `resource_continuity_revision` bump — expressed entirely within ADR
  0001's existing `unverified`/`trusted`/`revoked` vocabulary and revision
  rules, or else requiring its own separate ADR 0001 amendment before
  implementation (§20 "Representation boundary", §27).
- A candidate endpoint's lifecycle/admissibility state is captured before
  its remote evidence read and re-validated inside the same write
  transaction that would accept a candidate-attestation binding; a
  candidate retired mid-flight must never receive a binding merely because
  its `endpoint_id`/locator/transport-trust context did not change (§19a).

Required negative witnesses for the next implementation package:

1. a reachable endpoint presenting a matching anchor, but never explicitly
   attested, must never become active/candidate-eligible;
2. an anchor mismatch must never auto-create a new `inventory_source_id`;
3. an anchor **mismatch** (an unresolved read outcome, before any operator
   decision, §17) must never auto-transition any resource's
   `security_continuity`, `presence`, or `lifecycle` — those remain governed
   exclusively by ADR 0001/0002's own rules. This is distinct from an
   *accepted* epoch bump (§16, an explicit operator decision), which does
   mandate a `security_continuity` transition for affected resources under
   witness 18 below — mismatch alone never reaches that decision point;
4. two simultaneously reachable environments presenting the identical
   enrolled anchor (clone/restore, §11) must never be treated as proof that
   either one is "the" source for any automatic action;
5. a run/worker holding an `expected_source_attestation_epoch` older than
   current must be rejected exactly like a stale `source_config_revision`
   or `transport_trust_revision` (§21);
6. an epoch-scoped candidate attestation binding from a prior epoch must
   never be honored after a re-attestation epoch bump (§14, §16);
7. an evidence-unavailable outcome must never be treated as either an
   implicit match or an implicit mismatch (§18);
8. `baseline_mode`/observed-topology changes must never affect, and must
   never be affected by, `source_attestation_epoch` (§25);
9. attestation must never appear as a precondition or side effect anywhere
   in the resource-level policy/capability derivation (§28);
10. Blocker A/B evidence recorded under an older `source_attestation_epoch`
    must never be cited to authorize a decision under a newer epoch without
    an explicit, separately accepted carry-forward/re-validation procedure
    owned by that evidence class's own future ADR (§20, §26, §27);
11. an `source_attestation_epoch` bump must never, by itself, create a new
    `resource_id`, close/change a `binding_id`, change `locator_generation`,
    assert resource replacement, or change any resource's
    presence/observational continuity — those *discovered-inventory* facts
    remain governed exclusively by ADR 0001/0002's own reconciliation
    rules (§20). This is distinct from the resource's backend-owned
    `security_continuity`/`resource_continuity_revision` axis, which an
    accepted epoch bump *does* mandate a transition for under witness 18
    below — the two are separate axes, not a contradiction;
12. a matching anchor read must never be described or implemented as proof
    that the responding endpoint possesses PVE CA private key material —
    it is, at most, an asserted identifier match an operator chooses to
    trust (§9, §10);
13. a discovery run that commits successfully while
    `source_attestation_epoch=N` must never be presented as mutation-fresh
    once the source's current epoch has advanced past `N`, absent a newer
    successful commit issued and finalized under the new epoch (§20, §21);
14. a tier-1 asserted-anchor match must never be described, implemented, or
    logged as if it were tier-2 cryptographic chain evidence, or vice versa
    — the two tiers are recorded and reasoned about separately (§10a);
15. a `certificates/info` payload asserting the enrolled root-CA fingerprint
    must never be treated as substituting for, or repairing the absence of,
    tier-2 TLS-peer-chain evidence when the current leaf does not
    cryptographically chain to the enrolled root CA (§10a, case F);
16. no future Blocker B implementation may introduce a new canonical
    `security_continuity` enum value to represent epoch-stale trust, and
    none may leave the stored value `trusted` while its required proof is
    epoch-stale by computing only a separate epoch-derived "effective"
    value — the mandatory default (`trusted -> revoked` plus a
    `resource_continuity_revision` bump) must be expressed inside ADR
    0001's existing three-value vocabulary and revision rules, or else
    requires its own separate, explicit ADR 0001 amendment before
    implementation (§20 "Representation boundary", §27);
17. a candidate endpoint made ineligible (e.g. retired) after its remote
    evidence read began, but before the write transaction commits, must
    never receive an accepted candidate-attestation binding, even if its
    `endpoint_id`/locator/`transport_trust_revision` never changed (§14,
    §19a);
18. once Blocker B exists, a resource whose `security_continuity=trusted`
    depends on proof established under an older `source_attestation_epoch`
    must transition to `revoked` (with a `resource_continuity_revision`
    bump) when the epoch advances past it, unless an accepted Blocker-B
    carry-forward procedure establishes proof valid for the exact new
    epoch — it must never remain canonically `trusted` while its required
    proof is epoch-stale (§20 "Representation boundary", §27).

## 30. What remains unresolved after this ADR

1. Whether, and if so how, ongoing/continuous re-validation of the anchor
   against the sole already-active endpoint should be added (beyond the
   candidate-endpoint-only checks this ADR designs), to close the residual
   "stable URL silently repoints to a different environment" gap that ADR
   0002 currently documents as an accepted observational limitation for
   base read-only inventory. This ADR takes no position beyond noting that,
   if implemented, it must be strictly fail-closed and must not weaken
   today's posture; it is not authorized here.
2. Exact operator UX/audit-trail presentation for enrollment/re-attestation.
3. Exact schema (tables/columns/enum names) for the attestation state
   itself. The privilege requirement for `GET /nodes/{node}/certificates/info`
   is now recorded as verified against current upstream `pve-manager` source
   (§6: `permissions => { user => 'all' }`, no `Sys.Audit`/`VM.Audit`
   requirement) — what remains open is only re-confirming that contract
   against the exact supported PVE 9.x tag at implementation time, with the
   same FACT-DOC/FACT-SOURCE discipline ADR 0002 applies to every other
   endpoint in its matrix, and failing closed if it cannot be confirmed.
4. Whether, and under exactly what procedure, a pre-attested candidate
   endpoint (threat case R) may ever be promoted — automatically or only
   operator-triggered — on loss of the primary active endpoint, and which
   evidence tier(s) (§7, §10a) such a procedure would require. Not decided
   here; requires a separate, later activation/failover ADR.
5. Whether additional independent evidence classes (tier 3, e.g. an
   out-of-band Hubinet-provisioned enrollment secret) should ever
   supplement tiers 1/2 as anchor evidence (§7, §10a).
6. Long-term retention/purge policy for superseded attestation epochs'
   evidence.
7. The **authority rule** for Blocker A/B evidence crossing an attestation
   epoch boundary is now normative (§20, §26, §27, §29 negative witness 10),
   and, for Blocker B specifically, so is the **mandatory default
   representation** within ADR 0001's existing vocabulary
   (§20 "Representation boundary", §27, §29 negative witnesses 16/18):
   old-epoch security-sensitive evidence is not authority-eligible under a
   newer epoch, and a resource whose `trusted` continuity depended on it
   reverts to `revoked` (with a `resource_continuity_revision` bump) unless
   an accepted carry-forward procedure establishes valid new-epoch proof.
   What remains unresolved is only the *mechanics* of that optional
   carry-forward/re-validation procedure — whether and how it could ever
   establish new-epoch-valid proof without requiring a full re-enrollment —
   and Blocker A's own analogous mechanics for whichever of its evidence
   classes turn out to be epoch-dependent (§26). Both are left to Blocker
   A's and Blocker B's own future ADRs, which must satisfy, not re-litigate,
   the authority rule and mandatory default fixed here.
8. ~~Whether reconfirmation bumps the epoch~~ — resolved by this revision
   (§16, §20): a same-value reconfirmation is an audit event only and never
   bumps `source_attestation_epoch`; only an accepted value change or an
   explicit revocation/reset does.

## 31. Implementation consequences for the next package (not implemented here)

A future, separately reviewed and separately accepted implementation
package would need to add, at minimum:

- new durable fields/table(s) on the `app.inventory` authority schema for
  `attestation_status`, `source_attestation_epoch`, enrolled anchor kind/
  value, and full audit provenance (attested-at, attested-by, prior value),
  paralleling the existing `node_attestations` pattern in shape but scoped
  to sources, not nodes;
- a new, explicit, audited backend enrollment/re-attestation operation,
  never invoked by discovery itself;
- extension of `discovery_runs` issuance/commit CAS to include
  `expected_source_attestation_epoch` (§21), with the full positive/negative
  contract-test family this repository already requires for every other
  expected-context field (in-flight change invalidates commit; stale worker
  rejected; restart-safe; etc.), mirroring ADR 0002's existing test
  discipline;
- extension of `source_runtime_health`'s committed-context provenance to
  include `committed_source_attestation_epoch`, recorded by every
  successful applicable commit alongside the existing committed
  source-config/endpoint/canonicalization/transport-trust fields (§21);
- the controlled source security-context transition for every epoch bump
  (§20 "Freshness-context participation"), including initial enrollment
  `0 -> 1`: fences active discovery-run ownership, marks current health
  stale with `current_health_origin=controlled_context_transition`,
  invalidates mutation freshness, advances `published_state_revision`,
  leaves `inventory_revision` and all discovered resource facts/identities
  (`presence`, `binding_id`, `locator_generation`,
  `observational_continuity`) untouched, and the positive/negative
  contract-test family this repository already
  requires for that pattern (successful run under old epoch does not read
  as fresh after a later epoch bump; same-anchor reconfirmation performs no
  invalidation; only a new commit under the new epoch restores fresh; etc.);
- epoch-scoped candidate attestation bindings (§14) as their own audited,
  retained record, invalidated on re-attestation, and further invalidated
  if the candidate's lifecycle/admissibility state changed between read and
  write (§19a, §29 negative witness 17);
- the §19a read-then-write concurrency pattern implemented literally: every
  attestation-gated remote evidence read happens outside any DB write
  transaction, with expected context (including
  `source_attestation_epoch`, and, for candidate checks, the candidate's
  lifecycle/admissibility state) captured before the read and re-validated
  by CAS inside the write transaction that follows it; positive/negative
  contract tests for this pattern (concurrent context change between read
  and write is classified stale, never partially applied);
- when Blocker A's and Blocker B's own future implementation packages are
  designed, they must each record the exact `source_attestation_epoch`
  their evidence was established under and enforce the §20
  authority-eligibility rule (old-epoch evidence is not authority-eligible
  under a newer epoch); Blocker B's implementation specifically must apply
  the **mandatory default representation** fixed by this ADR — a resource
  whose `security_continuity=trusted` depends on epoch-stale proof
  transitions to `revoked` with a `resource_continuity_revision` bump
  unless an accepted carry-forward procedure establishes proof valid for
  the exact new epoch, expressed **strictly within ADR 0001's existing
  vocabulary and revision rules** — this package does not implement
  Blocker A/B evidence itself, and does not authorize either of those
  future packages to leave stale `trusted` uncorrected or to introduce a
  new ADR 0001 canonical state without ADR 0001's own separate amendment
  (§20 "Representation boundary", §27, §29 negative witnesses 16/18);
- optional capture of tier-2 TLS-peer-chain-to-enrolled-CA evidence (§10a)
  alongside tier-1 asserted-anchor evidence, recorded and reasoned about as
  a distinct evidence class, never conflated with or substituted for tier 1
  (§29 negative witnesses 14/15); this remains additional corroborating
  evidence only — the next package does not decide, and must not decide by
  implementation-detail default, that tier 2 alone authorizes any
  activation-gated action;
- explicit non-goal restated: this next package still does not implement
  endpoint activation, candidate promotion, or failover. Attestation is a
  prerequisite gate only. A separate, later ADR must define the exact
  activation/failover procedure — including which evidence tier(s) it
  requires — before any such runtime behavior may be turned on, and that
  ADR must itself satisfy the same architecture-change process as this one.
- explicit non-goal restated: this next package still does not deliver
  **tier-3** host-specific/Hubinet-managed attestation (§4, §7). Tier 2
  (§10a) is already cryptographic and may be captured as described above,
  but neither tier proves root CA private-key possession or physical
  uniqueness. If stronger assurance than tier 2 is ever required, it needs
  its own separately designed tier-3 primitive/ADR — tier-1 asserted-
  identifier evidence is not upgradeable into cryptographic proof by
  implementation detail alone, and tier-2 chain evidence is not upgradeable
  into root-CA-possession or physical-uniqueness proof by implementation
  detail alone either.

This wave (WAVE C0) implements none of the above. It only records this
architecture as `PROPOSED`, awaiting explicit operator acceptance.
