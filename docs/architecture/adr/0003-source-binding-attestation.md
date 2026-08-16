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

The central design discipline of this ADR is that **logical PVE trust-domain
anchor equality is not, and must never be treated as, proof of physical
identity** (§10, §11). It is also not, by itself, proof of Hubinet Ops source
identity — it is *evidence* that a human explicitly accepted at one
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
- the Hubinet Ops authority database itself is restored from an older or a
  cloned backup, independently of what happened to the PVE side in the
  meantime;
- races between an in-flight discovery run and a concurrent attestation
  decision, and between attestation and restart recovery, must not silently
  cross evidence across attestation epochs, mirroring the existing
  `source_config_revision`/`transport_trust_revision` CAS discipline.

### 5a. Case-by-case adversarial classification

Required minimum analysis, one row per case from the wave brief. Columns
answer "what can legitimately be proven by anchor evidence alone" —
`same peer` = same transport peer answered; `same domain` = same logical PVE
trust domain (root CA lineage); `same source` = safe to keep treating as the
same `inventory_source_id`; `same physical` = same physical/virtual machine;
`operator` = evidence alone is insufficient, an explicit operator decision is
required.

| Case | Scenario | Same peer | Same trust domain | Same Hubinet source | Same physical | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| A | Fresh standalone install | n/a (first contact) | n/a | n/a — establishes anchor at enrollment | unknown | Nothing to compare against; enrollment (§12) creates the baseline. |
| B | Cluster created from an existing standalone node | likely yes | **unknown** — official contract for CA preservation across `pvecm create` is not verified here | operator | unknown | Treated as **unknown**, not assumed preserved (see §16); mismatch handling applies if the anchor changes. |
| C | Standalone node joins an existing foreign cluster | yes (physical box unchanged) | **no** — node adopts the foreign cluster's CA | **no**, requires re-attestation | yes (but irrelevant) | Sharpest same-peer/different-domain case; anchor mismatch (§17) must trigger even though the physical box is unchanged. |
| D | Adding/removing cluster nodes (existing members) | n/a | yes — surviving members keep the existing CA | yes | n/a | No anchor effect; ordinary operational event. |
| E | Node certificate (leaf) renewal, CA-signed | yes | yes — CA unchanged | yes | yes | Leaf rotates (`transport_trust_revision`-relevant per ADR 0002); anchor does not. No re-attestation needed. |
| F | ACME/custom pveproxy certificate replacement | yes (new leaf) | yes — internal root CA unaffected | yes | yes | Transport trust policy change (§23) independent of anchor; anchor still readable as data over the authenticated session. |
| G | PVE root CA lifetime/deliberate regeneration | yes | **no** — deliberate anchor change | operator (explicit accept via re-attestation) | yes | Requires explicit re-attestation accepting the new anchor (§16). |
| H | Endpoint DNS/IP/port change | operator must verify | evidence-dependent | operator (candidate enrollment, §14) | unknown | New `endpoint_id` (ADR 0002); anchor check supports but never alone authorizes activation. |
| I | Switching to another node of the same cluster | different peer | yes — same cluster CA | operator (candidate enrollment) | different physical box, same domain | Anchor match is *expected* evidence supporting candidate enrollment, still requires explicit action to activate. |
| J | Same URL silently repoints to a different PVE environment | different peer | **no** | **no** | unknown | The exact gap ADR 0002 flags as an accepted observational limitation; anchor check is the only primitive proposed here that can detect it (§31.1 scope question). |
| K | Restore/recovery of `pmxcfs`/`config.db` to replacement hardware | different peer | yes — same CA material restored | operator | **no** — different physical machine, matching anchor | Core evidence for §11: anchor match ≠ physical proof. |
| L | Full cloned PVE environment carrying copied `pmxcfs`/root CA | different peer | yes (both clones) | operator — must not auto-resolve | **no**, and possibly two live copies | Sharpest clone counterexample (§11); never auto-pick a "winner." |
| M | Snapshot/backup cloning leaving the same CA live in two environments | different peer | yes (both) | operator | **no** | Same conclusion as L; both may be transiently live (DR test). |
| N | Compromise/copy of CA private material | attacker-controlled | **appears yes to evidence, is not trustworthy** | **cannot be determined by this primitive** | unknown | Explicit threat-model limitation (§5, §29); anchor is not a defense against private-key exfiltration. |
| O | Backend DB restore independently of PVE restore | n/a (backend-side) | evidence may be stale relative to current PVE state | fails closed via normal mismatch handling | n/a | No special-case logic needed: next real read simply compares against the (possibly stale) enrolled value (§17). |
| P | Backend DB clone and PVE clone together | n/a | yes (paired clone) | **each clone considers itself attested to its own paired PVE clone** | **no** | Real residual risk, flagged as unresolved (§30.item on backend/PVE paired clone split-brain is out of this ADR's scope; see also `0.5-foundation.md` on `backend_instance_id` reinstall semantics). |
| Q | Loss of current endpoint, only an unattested new endpoint reachable | n/a | unknown until checked | **no — stays `source_unavailable`** | unknown | No automatic promotion of an unattested endpoint, matching anchor or not (§29 negative witness 1). |
| R | Pre-attested candidate endpoint before primary failure | yes (attested earlier) | yes, at the attested epoch | conditionally — only if epoch unchanged since | unknown | Necessary but declared **not sufficient** for automatic promotion; left to a future activation ADR (§15, §30.4). |
| S | Standalone↔cluster transition, anchor unchanged / anchor changed | n/a | unchanged case: yes; changed case: no | unchanged: yes; changed: operator | unknown | Two fully independent evaluations — mode transition itself never affects attestation (§25, G8). |
| T | Trust-domain mismatch during an in-flight discovery run | n/a | no (by definition) | run classified invalid/stale | n/a | Same CAS discipline as existing expected-context fields (§21). |
| U | Restart while an attestation/re-attestation operation is incomplete | n/a | prior committed state only | prior committed state only | n/a | No partial state to recover from; atomic commit or nothing (§19). |
| V | Stale worker/run using evidence from an older attestation epoch | n/a | stale | rejected | n/a | Same fencing discipline as `source_config_revision`/`transport_trust_revision` (§21, §29 negative witness 5). |

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

This ADR treats the exact request/response shape, required privilege
(expected to be the same `Sys.Audit` class already used for
`/nodes/{node}` facts, but this must be independently verified against
official Proxmox source with the same FACT-DOC/FACT-SOURCE discipline ADR
0002 uses before implementation — see §31), and exact PVE-version support
matrix as **implementation-package work**, not decided by this ADR.

## 7. Candidate options considered

1. **PVE root CA fingerprint as a logical trust-domain anchor**, bound via
   explicit enrollment (recommended, §9).
2. No new primitive; keep endpoint replacement/failover permanently
   unavailable. Rejected as a permanent answer — it leaves operators with no
   accepted path to ever add a second endpoint or recover from an endpoint
   change, which the existing architecture already flags as a real gap
   (ADR 0002 §"Nierozstrzygnięte kwestie" #6). Still the correct default
   *until* this ADR (or a successor) is accepted.
3. A future, stronger cryptographic host attestation (e.g., dedicated
   Hubinet-issued enrollment secret provisioned out-of-band on the PVE host).
   Not rejected — flagged as a possible *additional* evidence class in §31,
   complementary to, not a replacement for, option 1. Not designed here.

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
- **Treating root CA equality as physical-host proof**: rejected — this is
  the central adversarial finding of this ADR. See §10, §11.

## 9. Recommended decision

Adopt the PVE root CA (`/etc/pve/pve-root-ca.pem`) SHA-256 fingerprint,
retrieved via `GET /nodes/{node}/certificates/info` over the already
transport-validated session, as the **logical PVE trust-domain anchor**
candidate evidence class.

Bind it to exactly one `inventory_source_id` through one explicit, audited
**enrollment** decision (§12), fenced by a new, source-owned, monotonic
**source-attestation epoch** (§20) that is a peer of, and independent from,
`source_config_revision` and `transport_trust_revision` — never derived from
either, never derived from the anchor value itself.

Root CA equality across an enrollment and a later observation proves **only**
"the same logical PVE trust domain (the same `pmxcfs`-maintained cluster/
standalone identity) answered both times, within the threat assumptions of
§5/§11." It is necessary evidence for continuing to trust a transport target
as "the same source," but it is explicitly **not sufficient by itself** to
authorize any state change — every attestation-gated transition requires the
same explicit human enrollment/re-attestation act that established the
anchor in the first place (§18, §29 negative witness 1).

## 10. Exact trust semantics of the PVE root CA anchor

What matching anchor evidence **does** establish, within the stated threat
assumptions:

- the responding endpoint is currently backed by a `pmxcfs` instance that
  possesses the same CA material as the one enrolled for this
  `inventory_source_id`;
- that CA material is the one Proxmox itself uses to sign node certificates
  cluster/standalone-wide, so a match is evidence the responding instance is
  part of the *same replicated cluster filesystem lineage* as the one
  enrolled.

What matching anchor evidence explicitly does **not** establish:

- that the request travelled to the same physical or virtual machine (§11);
- that no restore, clone, or key exfiltration has occurred (§5, §29);
- that the endpoint's *content* (guests, nodes, ACLs) has not diverged —
  attestation is a transport/identity-domain concept, never inventory
  content evidence;
- any resource-level (`security_continuity`) or destructive/management
  authority (§28);
- anything about physical uniqueness — see §11.

## 11. Explicit clone/restore limitation

`pmxcfs`'s config/CA material lives in `/etc/pve/config.db`. Restoring or
cloning that data — to replacement hardware after a hardware failure (case
K), to a full duplicated environment (case L), or via snapshot/backup
cloning that leaves two live copies (case M) — reproduces the CA material
exactly. Two, or more, simultaneously reachable environments can therefore
legitimately present the **identical** root CA fingerprint.

**Same PVE root CA fingerprint MUST NOT be treated as proof of the same
physical machine.** It is not disproof of a legitimate restore/clone either
— both are simply outside what this evidence class can distinguish. Any
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
transport, records it as the enrolled anchor value and evidence kind, and
atomically transitions `attestation_status: not_yet_attested → attested`
while incrementing `source_attestation_epoch` to `1`. This transaction is
conceptually a peer of the existing controlled source/endpoint/transport
transitions (ADR 0002 §"controlled source config/active route/
canonicalization/TLS trust transition"): it must serialize with active
discovery-run ownership exactly like those transitions do (§21).

## 13. Existing endpoint reconnect behavior

Ordinary reconnects to the same already-active `endpoint_id` (transient
network loss, restart, routine leaf-certificate renewal under an unchanged
`transport_trust_revision` policy) are **not** attestation events. They
continue to be governed entirely by the existing endpoint/health/freshness
contract (ADR 0002). This ADR does not add an attestation check to every
ordinary discovery run of the sole active endpoint; whether it *should*
eventually is an explicitly open question, not decided here (§31.1).

## 14. Candidate endpoint enrollment

An operator who wants to prepare a second endpoint (different node/URL)
against the same `inventory_source_id` may explicitly request an
**endpoint-scoped attestation check**: the backend reads anchor evidence
from that candidate endpoint (requiring its own already-validated transport
trust, independent of the active endpoint's) and compares it against the
source's current enrolled anchor at the source's *current*
`source_attestation_epoch`.

A match records an **epoch-scoped candidate attestation binding**:
`(endpoint_id, source_attestation_epoch, matched_at)`. This binding is valid
only for the exact epoch it was taken against; if the source is later
re-attested (epoch bump, §16), every existing candidate binding becomes
stale and must be redone (§29 negative witness 7). A mismatch does **not**
attest the candidate, does not affect the source's own attestation status,
and does not affect the candidate endpoint's existing `candidate` lifecycle
status (ADR 0002) in any way beyond leaving it un-attested.

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
unresolved and out of this wave's boundary (§31.4). The safest minimal
procedure this ADR anticipates — explicit, operator-triggered promotion of
an already epoch-scoped-attested candidate, never fully automatic — is
noted as a direction for that future ADR, not decided here.

## 16. Re-attestation procedure

Re-attestation is the same explicit, audited operation as initial
enrollment (§12), issued against an already-`attested` source. It:

- reads fresh anchor evidence from the endpoint the operator specifies
  (normally the current active endpoint);
- if it matches the currently enrolled anchor value, records the
  re-attestation event and **may** leave `source_attestation_epoch`
  unchanged (a mere reconfirmation) or bump it, depending on the exact
  implementation contract decided in the next package — but a bump is
  required whenever the enrolled anchor *value* itself changes (a genuine
  new anchor), consistent with ADR 0001's `resource_continuity_revision`
  rule that a security-relevant continuity decision always advances its
  token;
- if it does not match, does **not** silently accept the new value. It
  requires the operator to explicitly choose one of: (a) accept the new
  anchor as a deliberate environment change (e.g., planned CA regeneration,
  case G) — which bumps `source_attestation_epoch` and records the new
  anchor with full audit of the prior value, or (b) reject/investigate,
  leaving the source in `mismatch_pending_reattestation` (§17).

Re-attestation must serialize with active discovery-run ownership exactly
like other controlled context transitions (ADR 0002 pattern): at an active
run, the implementation either waits for its terminal release or atomically
fences it, before completing (§21).

## 17. What happens on anchor mismatch

An anchor read that unambiguously disagrees with the currently enrolled
value (for a candidate check, §14, or for any future continuous check, §31.1)
transitions the *evaluated relationship* (never automatically the source
itself) to `mismatch_pending_reattestation`-class evidence:

- it does **not** create a new `inventory_source_id`;
- it does **not** revoke or otherwise touch any resource's
  `security_continuity` (Blocker B remains entirely separate, §27, §28);
- it does **not** by itself stop ordinary discovery on the still-currently
  active endpoint (that would only be added by the explicit continuous-check
  extension left open in §31.1, and even then must be an explicit,
  documented fail-closed decision, not a silent one);
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
transaction (§12, §16), not a long-running background process, so it does
not need a `discovery_runs`-style multi-phase issued/running/completed
lifecycle of its own. It must still be **atomic and crash-safe**: a
transaction that reads anchor evidence and writes the new
`attestation_status`/`source_attestation_epoch`/enrolled-anchor-value must
commit all three together or none. A process crash mid-transaction leaves
the prior committed attestation state entirely unchanged after restart —
there is no partial "epoch bumped but anchor not recorded" state to recover,
because there is no separate issuance phase to abandon/fence. If a future
implementation package introduces a longer-running verification step (e.g.,
multi-endpoint quorum reads), it must reuse the existing
issued/running/abandoned-on-restart pattern from `discovery_runs` rather
than inventing a second one (§31.3).

## 20. Source-attestation epoch/revision semantics

`source_attestation_epoch` is a new, source-owned, monotonic token, a peer
of `source_config_revision` and `transport_trust_revision`, never derived
from either and never derived from the anchor value:

- starts at an explicit initial sentinel (`0`, or equivalent) meaning
  `not_yet_attested`;
- increments exactly once per accepted security-relevant attestation
  decision (initial enrollment, accepted anchor change, explicit
  revocation) — never per read, never per match confirmation that does not
  change the enrolled value (implementation may choose whether a pure
  reconfirmation without a value change also bumps it, but a genuine value
  change **must**, §16);
- is never decremented, never reused, never derived from wall-clock time;
- is the fencing token every attestation-gated evidence artifact (candidate
  attestation bindings, §14) must be scoped to.

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
for `confirmed_removed`) remains entirely orthogonal to source attestation.
Attestation establishes trust-domain continuity for the *source*; it says
nothing about whether any individual resource is present or absent. A future
absence-proof contract that wants to cite source-attestation freshness as
part of its own evidence-freshness fence may do so, but this ADR grants it
no authority to do so implicitly, and does not itself define that
interaction.

## 27. Interaction with future Blocker B enrollment

Resource-level `security_continuity=trusted` (ADR 0001, "Blocker B" —
workload continuity/enrollment proof) remains a **completely separate**
trust object from source-level attestation, forever. A trusted source
attestation never upgrades any resource's `security_continuity`, and a
resource's trusted continuity proof never substitutes for a missing or
mismatched source attestation. Both trust axes must independently hold for
any future capability that depends on either.

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

Required negative witnesses for the next implementation package:

1. a reachable endpoint presenting a matching anchor, but never explicitly
   attested, must never become active/candidate-eligible;
2. an anchor mismatch must never auto-create a new `inventory_source_id`;
3. an anchor mismatch must never auto-transition any resource's
   `security_continuity`, `presence`, or `lifecycle` — those remain governed
   exclusively by ADR 0001/0002's own rules;
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
   in the resource-level policy/capability derivation (§28).

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
3. Exact schema (tables/columns/enum names) and exact backend
   privilege/role requirement for `GET /nodes/{node}/certificates/info`,
   including the same FACT-DOC/FACT-SOURCE verification discipline ADR 0002
   applies to every other endpoint in its matrix.
4. Whether, and under exactly what procedure, a pre-attested candidate
   endpoint (threat case R) may ever be promoted — automatically or only
   operator-triggered — on loss of the primary active endpoint. Not decided
   here; requires a separate, later activation/failover ADR.
5. Whether additional independent evidence classes (e.g., an out-of-band
   Hubinet-provisioned enrollment secret) should ever supplement or replace
   the PVE root CA fingerprint as anchor evidence.
6. Long-term retention/purge policy for superseded attestation epochs'
   evidence.
7. Exact interaction contract with Blocker A and Blocker B once each is
   separately accepted (§26, §27 state only that they remain orthogonal, not
   the mechanics of any future combined evidence contract).
8. Whether a genuine anchor-value reconfirmation without any value change
   should bump `source_attestation_epoch` or not (§16, §20) — left as an
   implementation-package decision within the stated constraint that a
   genuine *value change* must always bump it.

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
- epoch-scoped candidate attestation bindings (§14) as their own audited,
  retained record, invalidated on re-attestation;
- explicit non-goal restated: this next package still does not implement
  endpoint activation, candidate promotion, or failover. Attestation is a
  prerequisite gate only. A separate, later ADR must define the exact
  activation/failover procedure before any such runtime behavior may be
  turned on, and that ADR must itself satisfy the same
  architecture-change process as this one.

This wave (WAVE C0) implements none of the above. It only records this
architecture as `PROPOSED`, awaiting explicit operator acceptance.
