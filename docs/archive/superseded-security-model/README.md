# Superseded security-proof architecture (ADR 0003–0006 and its evidence)

**Archived. Authority for nothing. Not a roadmap.**

These documents were ACCEPTED under an earlier threat model in which a malicious
root inside a managed guest, a malicious Proxmox root, or a malicious
administrator replacing Hubinet-owned state were in scope. Under that model
Hubinet needed cryptographic source attestation, attestation epochs,
relationship gates, candidate-endpoint attestation proofs, dual-evidence
confirmed removal, and persistent workload-incarnation proof ("Blocker B")
before any operator-driven update workflow could exist.

**That threat model has been retired by an explicit operator decision.** Hubinet
Ops targets a trusted, self-administered Proxmox environment: the Proxmox
administrator/root, the Proxmox host, root inside a managed LXC, the Hubinet
operator, and normal apt/dpkg behavior are all TRUSTED. Defending against a
hostile administrator of the environment being managed is **out of scope**, and
Blocker B is **no longer a blanket prerequisite** for the practical
`plan -> approval -> fresh job-owned snapshot -> update -> healthcheck ->
same-job rollback` roadmap.

The implementing code, schema, and tests were removed on branch
`refactor/lean-security-reset`. The historical source is recoverable from Git
history.

| File | Was |
| --- | --- |
| `0003-source-binding-attestation.md` | ACCEPTED ADR — source binding / attestation epochs / relationship gate / candidate endpoint proof |
| `0004-confirmed-removal-operator-absence.md` | ACCEPTED ADR — Class-C dual-evidence confirmed removal and operator authoritative-absence attestation |
| `0005-workload-continuity-enrollment.md` | ACCEPTED ADR — negative stock-PVE trust boundary; `security_continuity=trusted` granted nowhere |
| `0006-workload-continuity-stronger-proof.md` | ACCEPTED ADR — negative/unresolved stronger-proof research record |
| `adr0006-workload-continuity-evidence.md` | non-normative evidence record referenced by ADR 0006 |

## What this archiving does not do

It does not rewrite what these documents claimed, and it does not pretend they
were never accepted. It records that the product's threat model changed, so the
decisions they encode no longer describe what Hubinet Ops is defending against.

## What is still in force

The ordinary application-safety rules these ADRs happened to sit next to are
**kept**, and now live in `AGENTS.md` ("Threat model" and "Mutation and security
boundaries") and `docs/product-intent.md`:

- least-privilege PVE credentials and mandatory TLS verification;
- no secrets in argv or logs; recursive redaction in diagnostics;
- fixed, typed, allowlisted operations — never arbitrary command text;
- correct target/VMID validation;
- a failed or unavailable discovery is never resource deletion;
- a failed package scan is never "zero updates";
- concurrency protection against ordinary operational races;
- update-plan revalidation before an approved future update;
- job-owned snapshots and same-job rollback only;
- non-Hubinet snapshots are never touched;
- **NO AUTO-UPDATE.**
