# Changelog

Hubinet Ops has not had a numbered release yet. Release entries begin with the
first release; until then this file records notable pre-release changes.
Earlier 0.2.x–0.4.x history was retired with the implementation it described
and remains in Git history and tags.

## Unreleased — security model reset

Hubinet Ops now targets a **trusted, self-administered** Proxmox environment.
The Proxmox administrator/root, the Proxmox host, root inside a managed guest,
the operator, and normal `apt`/`dpkg` behavior are trusted; resistance to a
hostile administrator of the managed environment is out of scope. See
`AGENTS.md`, "Threat model".

- **Retired the hostile-admin security-proof architecture.** Source
  attestation and attestation epochs, the relationship gate,
  candidate-endpoint attestation proofs, dual-evidence confirmed removal and
  operator absence attestation, sampled-absence provenance, and the
  workload-incarnation ("Blocker B") research harness were removed, along with
  their schema, tests, and documentation.
- **Authority database schema reset to v6.** No migration exists and none is
  planned before the first release. **Pre-release installations recreate the
  authority database:** stop the service, move `/var/lib/hubinet-ops/authority.db*`
  aside, and start it again. The fresh database mints a new
  `backend_instance_id` and new `resource_id`s, so remove the old Home
  Assistant config entry and re-enroll.
- **Automatic Debian/Ubuntu LXC package scanning.** Schema v7 adds durable scan
  attempts and exact package rows and rejects v6 without migration. A
  configurable scheduler uses a dedicated pinned SSH key and one forced PVE
  helper operation to refresh APT metadata and simulate upgrades. Failed scans
  publish unknown, never zero. Home Assistant adds summary sensors without
  recording the full package list as attributes.
- **Documentation reduced to `README.md`, `PRODUCT.md`, `ARCHITECTURE.md`,
  `STATUS.md`, and `AGENTS.md`.** The ADR/research/runbook hierarchy and the
  custom agent skills were deleted; Git history is the archive.
- **Package-scanning rule restated.** Automatic scanning is non-installing and
  non-destructive to workload packages: it may refresh package-manager metadata
  and run simulations, but never installs, upgrades, removes, autoremoves, or
  configures a workload package.

Unchanged: PVE autodiscovery, dynamic backend inventory, the GET-only PVE
runtime, source health/freshness behavior, the Home Assistant snapshot contract
and dynamic device/entity behavior, least-privilege PVE credentials, TLS
verification, secret redaction, typed allowlisted operations, and
**NO AUTO-UPDATE**.
