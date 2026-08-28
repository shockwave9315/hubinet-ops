# Changelog

The 0.2.x-0.4.x release history has been retired along with the legacy implementation it
described, as part of the 0.5-only repository cleanup. It remains available through Git
history/tags for that source line.

Hubinet Ops 0.5 has not had a numbered release yet, and no 0.5 version has been
tagged/released. This file will begin recording entries from the first 0.5 release.

Current state is tracked in
[`docs/architecture/0.5-implementation-status.md`](docs/architecture/0.5-implementation-status.md):
the R0 read-only runtime is implemented and merged into `main`, has been deployed and
exercised on a real Proxmox host, and its operational activation decision is **GO** —
strictly read-only, mutation authority **NONE**. The full activation chronology is
preserved in
[`docs/archive/project-history/0.5-r0-activation-chronology.md`](docs/archive/project-history/0.5-r0-activation-chronology.md).

## Unreleased — security model reset

Hubinet Ops now targets a **trusted, self-administered** Proxmox environment. The
Proxmox administrator/root, the Proxmox host, root inside a managed LXC, the operator,
and normal `apt`/`dpkg` behavior are trusted; resistance to a hostile administrator of
the managed environment is out of scope. See `AGENTS.md`, "Threat model".

**Operator action required on an existing R0 install.** The authority database schema
moved from **v5 to v6** (the attestation and confirmed-removal tables were removed).
As the clean-break design requires, an existing v5 database is **rejected** on startup
rather than migrated:

1. stop `hubinet-ops-0.5.service`;
2. move `/var/lib/hubinet-ops/authority.db*` aside;
3. start the service — it creates a fresh v6 database and rediscovers the full
   inventory on the next scheduler cycle.

The fresh database mints a new `backend_instance_id` and new `resource_id`s, so Home
Assistant sees new devices/entities. Remove the old config entry and its stale registry
entries per [`docs/operations/0.5-ha-clean-break.md`](docs/operations/0.5-ha-clean-break.md),
then re-enroll.

Removed: source attestation authority and epochs, the attestation relationship gate,
candidate-endpoint attestation proof machinery, dual-evidence confirmed removal and
operator absence attestation, sampled-absence provenance, and the Blocker-B
workload-incarnation research harness. Former ADR 0003-0006 are archived to
[`docs/archive/superseded-security-model/`](docs/archive/superseded-security-model/).

Unchanged: PVE autodiscovery, dynamic backend inventory, the R0 GET-only PVE runtime,
source health/freshness behavior, the Home Assistant snapshot contract and dynamic
device/entity behavior, least-privilege PVE credentials, TLS verification, secret
redaction, typed allowlisted operations, and **NO AUTO-UPDATE**.
