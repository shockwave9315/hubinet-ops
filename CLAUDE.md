# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

Hubinet Ops is a policy-controlled Proxmox inventory service with a native Home Assistant
frontend. This repository is **0.5-only**: the obsolete 0.2.x/0.3.x/0.4.x implementation,
deployment, and Home Assistant presentation surfaces have been retired from `main` as a
deliberate clean-break repository boundary — see `docs/architecture/0.5-foundation.md`
("Git history and old tags are the archive. `main` is not an archive of obsolete runtime
implementations."). Their historical source remains recoverable only through Git
history/tags; do not resurrect any of it into the current tree.

The current, single-track implementation lives in:

- **`app/inventory/`** — the durable 0.5 authority subsystem (identity, discovery,
  reconciliation, publication).
- **`app/inventory_runtime.py`, `app/inventory_runtime_config.py`,
  `app/inventory_scheduler.py`, `app/inventory_pve_transport.py`** — the R0 read-only
  runtime composition root, config loader, discovery scheduler, and production Proxmox
  transport. This is the current production entrypoint (`uvicorn
  app.inventory_runtime:app`) — **read-only**: no policy, jobs, mutation, endpoint
  activation/failover exists yet. Current status is tracked in
  `docs/architecture/0.5-implementation-status.md`.
- **`custom_components/hubinet_ops/`** — the native Home Assistant integration consuming
  the R0 backend over HTTP.

**Before any architecture or implementation work, read `AGENTS.md` in full.** It is the
binding rules file for every coding agent in this repo (identity model, mutation trust
boundary, fail-closed security invariants, test boundaries). This CLAUDE.md summarizes
where things live and how to run things; `AGENTS.md` and the ACCEPTED architecture docs
below are the actual authority and take precedence over anything here.

**`docs/architecture/README.md` is the documentation entry point** — authority
hierarchy, the default reading set, the task-to-document matrix, the archive policy, and
the acceptance/supersession ratchet rule. Read the minimum current material your task
needs; do not recursively read all architecture and research documents. Anything under
`docs/archive/` is historical, is authority for nothing, and must not be read by default
or treated as a roadmap.

## Current product intent

Full statement: `docs/product-intent.md`. Binding summary (also in `AGENTS.md`):

```text
STATIC 0.4.x -> PVE AUTODISCOVERY -> DYNAMIC BACKEND INVENTORY
  -> DYNAMIC HOME ASSISTANT RESOURCES / UI -> SAFE OPERATOR-DRIVEN UPDATE WORKFLOW
```

PVE autodiscovery, dynamic backend inventory, and dynamic Home Assistant representation
are implemented today, strictly read-only. The remaining product surface — read-only
package/update scanning and presentation, operator-approved update plans, job-owned
pre-update snapshots, health checks, and same-job rollback — is **not implemented yet**.
It is ordinary implementation work under the accepted inventory architecture: it does
**not** need a new ADR merely because it touches a managed guest.

**Threat model (binding, `AGENTS.md` "Threat model"):** Hubinet Ops targets a trusted,
self-administered Proxmox environment. TRUSTED: the Proxmox administrator/root, the
Proxmox host, root inside a managed LXC, the Hubinet operator, normal apt/dpkg behavior.
OUT OF SCOPE: a malicious root inside a managed guest, a compromised Proxmox root, an
administrator replacing Hubinet-owned state, and any proof intended to survive full
administrative compromise. Do not design defenses for those. The former attestation /
confirmed-removal / Blocker-B security-proof architecture is superseded and archived
under `docs/archive/superseded-security-model/`; its code, schema, and tests are gone.

Hard rules that remain binding: automatic package/update **scanning** is allowed but must
be read-only; the operator sees exact package/update detail; **NO AUTO-UPDATE** —
installing updates always requires explicit operator review and approval of the current
plan; a material plan change after approval invalidates that approval; each run takes a
fresh job-owned pre-update snapshot and may roll back only to its own; retention touches
only Hubinet-managed snapshots, never operator snapshots; a failed or unavailable
discovery is never deletion; a failed package scan is never zero updates.

Normative 0.5 architecture, in order of authority:
1. `docs/architecture/adr/0001-resource-identity-incarnation.md` (ACCEPTED)
2. `docs/architecture/adr/0002-proxmox-discovery-reconciliation.md` (ACCEPTED)
3. `docs/architecture/0.5-foundation.md` (ACCEPTED — Phase 0 decisions; partly in Polish)
4. `docs/architecture/0.5-inventory-model.md` (ACCEPTED — materializes ADR 0001/0002;
   its attestation/trust sections are superseded, see its own header note)
5. `docs/architecture/0.5-r0-read-only-runtime-activation.md` — the R0 read-only runtime
   activation design (19-item Phase 1 gate audit, composition-root/deployment decisions).
   Still an **active implementation contract**, not history: `app/inventory_runtime.py`,
   `inventory_runtime_config.py`, `inventory_scheduler.py`, `inventory_pve_transport.py`,
   `custom_components/hubinet_ops/transport_http.py`, `deploy/install-0.5.0-fresh.sh`,
   `config/inventory.example.yaml` and seven test modules cite it by name and section
6. `docs/operations/0.5-r0-operational-activation.md`,
   `docs/operations/0.5-ha-clean-break.md` — the operational activation runbook and the
   Home Assistant 0.4→0.5 clean-break/purge plan for a real deployed instance
7. `docs/architecture/0.5-implementation-status.md` — current status, NOT an authority;
   if it conflicts with an ACCEPTED ADR, the ADR wins and the status doc must be
   corrected.

ADRs 0003–0006 are **SUPERSEDED** and archived; do not read them and do not reintroduce
their concepts.

Skills under `.agents/skills/` encode repo-specific procedures on top of these rules:
`hubinet-contract-review` (review procedure), `hubinet-phase-boundary` (Phase 0 HA vs.
Phase 1 backend ownership), `hubinet-architecture-change`, `hubinet-test-gate` (evidence
required before calling work done/merge-safe).

## Commands

Install deps:
```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
```

Complete repository validation (mirrors CI) — run when the bounded test gate calls for it
(runtime change being published/merged, contract/property-family closure) or before a
release/final integration gate. It is **not** the default command to run after every small
or bounded edit; see "Agent working discipline" below for proportional validation during
iterative work.
```bash
python -m compileall -q app custom_components tests scripts
pytest -q
bash -n deploy/install-0.5.0-fresh.sh
for f in deploy/bootstrap-proxmox-0.5.sh deploy/lib/*.sh; do bash -n "$f"; done
for f in deploy/bootstrap-proxmox-0.5.sh deploy/lib/*.sh; do python scripts/validate_hermetic_shell_boundary.py "$f"; done
python scripts/validate_yaml.py
python scripts/check_tracked_files.py
```

`tests/test_bootstrap_proxmox_0_5_smoke.py` (the only file that executes
`deploy/bootstrap-proxmox-0.5.sh` for real) is excluded from ordinary local/CI
`pytest -q` runs by its own sandbox-marker skip — it only ever runs for real inside
`tests/shell/run_bootstrap_smoke_sandbox.sh`'s ephemeral-CI-only Docker sandbox.

Single test file / single test — the default during iterative bounded work:
```bash
pytest tests/test_inventory_reconciliation.py -q
pytest tests/test_inventory_reconciliation.py -k test_name -q
```

Home Assistant integration suite (pinned, separate dependency set — do not mix with
`requirements-dev.txt`):
```bash
python -m pip install -r requirements-ha-test.txt
python -m pytest -q --tb=short -o asyncio_mode=auto tests/test_hubinet_ops_integration.py
```
This suite requires exact `homeassistant==2026.8.1` on Python `>=3.14.2` (Linux). It does
**not** run on native Windows because Home Assistant imports POSIX `fcntl` at collection
time — that is a known, accepted harness limit; never patch Home Assistant/Python or fake
`fcntl` to work around it. Treat Linux CI as the real HA compatibility gate.

Tests never contact real Proxmox, Home Assistant, or any private-network endpoint — they
use a fake `ReadOnlyProviderTransport`, fake clocks, and temporary SQLite authority
databases. Never invoke a deployment script directly or run it against a real host from
this environment.

## Agent working discipline

This section is process discipline for working in this repo; it does not add or change
architecture. On any conflict, `AGENTS.md` and the ACCEPTED ADRs win.

### Prompt vs. architecture

Before substantial architecture or implementation work:

- read the full relevant ACCEPTED ADR surface for the bounded subsystem, not just the
  status doc;
- compare the task/prompt itself against `AGENTS.md` and the ACCEPTED ADRs;
- if the prompt conflicts with accepted architecture, stop, report the conflict, and follow
  the ADR instead — do not bend architecture to satisfy prompt wording (`AGENTS.md`,
  "Architecture source of truth").

When reasoning about a change, map it through: accepted invariant → enforcement layer →
persistence/transaction owner → reachable negative witness → regression test. Don't create
a separate design document unless explicitly asked for one.

### Bounded changes

For a concrete bug or review finding:

- isolate the smallest reachable witness;
- identify the exact accepted contract/security/runtime rule it violates;
- fix the bounded contract family, including symmetric/sibling paths in that same family;
- add regression coverage for the witness plus the relevant positive controls (legal
  polling gaps, audit/failure paths, monotonic skips);
- do not opportunistically refactor adjacent legacy or unrelated code;
- do not introduce new architecture as "hardening" unless the accepted model requires it —
  label it `NEW ARCHITECTURAL DECISION` instead (`hubinet-contract-review`).

Once that exact family is closed by code and regression evidence, stop — do not restart a
broad audit or expand into unrelated surface without new evidence (`AGENTS.md` Code Review
Rules; `hubinet-contract-review` "One witness means one bounded family review").

A P1/P2 finding needs a concrete reachable witness against an accepted contract/security/
runtime rule; documentation or completeness drift with no reachable runtime-contract failure
is normally lower severity, not a blocker.

### Git synchronization

Before starting work:

- verify the tracked worktree is clean;
- fetch `origin`;
- verify you're on the intended branch and know its remote head.

If the local branch is clean and only behind the exact same-history remote branch, it's safe
to fast-forward automatically:
```bash
git merge-base --is-ancestor HEAD origin/<branch> && git merge --ff-only origin/<branch>
```

Stop and ask instead of proceeding when:
- the tracked worktree is dirty;
- local and remote history have diverged;
- the expected remote branch/head changed unexpectedly.

Never use `rebase`, `reset --hard`, force-push, or an automatic `stash` unless explicitly
requested.

### GitHub / PR state

Being asked to do code work does not imply permission to mutate review/process state.
Unless explicitly requested, do not mark a PR ready/draft, resolve or reply to review
threads, edit PR title/body, merge, force-push, or open unrelated issues/PRs
(`hubinet-test-gate` "Merge safety"; `hubinet-contract-review` "Review writes").

### Windows / PowerShell UTF-8

When running under PowerShell, initialize UTF-8 explicitly before working with repo text,
since this repo's docs and skills contain non-ASCII content (e.g. Polish text in
`docs/architecture/0.5-foundation.md`):
```powershell
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
Get-Content -Raw -Encoding UTF8 <path>
```
Never treat mojibake produced by an implicit PowerShell console/file encoding as repository
file corruption — check the bytes with UTF-8 forced before concluding a file is broken.
This is unrelated to the separate native-Windows Home Assistant `fcntl` limitation covered
under Commands above — do not conflate the two.

### Proportional test gate

Validation should be proportional to the change, and remote CI (exact head) is the
mandatory merge-readiness evidence, not a substitute for judgment beforehand:

- **small/docs-only change**: targeted checks plus `git diff --check`; don't run the full
  suite by default;
- **production bugfix**: a minimal regression test for the witness plus the bounded
  affected-family tests; run the full suite only when the contract surface is broad or
  there's a concrete reason to suspect wider impact;
- **contract/property-family closure**: the targeted family plus the relevant Hypothesis/
  deterministic corpus;
- **publishing/merging a runtime change, or a release/final integration gate**: run the
  complete repository validation from the Commands section — this is where `AGENTS.md` and
  `hubinet-test-gate` require full pytest, not every intermediate edit.

Don't duplicate a long full local pytest run purely because CI will run it again, unless
scope is broad, there's a concrete reason, or the test-gate procedure requires it for this
specific claim (`DONE`/`READY`/`MERGE SAFE`). Green tests are evidence, not proof of
architectural correctness (`AGENTS.md`; `hubinet-test-gate`).

### Review/implementation closure

Before calling a substantial fix complete, explicitly re-check the bounded invariant family:
original witness closed; positive/legal path retained; negative/fail-closed path retained;
persistence/restart behavior retained where relevant; transaction/atomicity retained where
relevant; security/source-of-truth boundaries retained; no scope leakage into dormant/
runtime/legacy areas. If any row in that family isn't closed, don't claim completion.

## Architecture (0.5, current)

`app/inventory/` is an independently instantiable subsystem — its own SQLite "authority"
database (schema v6, marker `hubinet_ops_0_5_authority`). It has its own identity model,
summarized from ADR 0001/0002:

- A Proxmox VMID is only a reusable **slot locator** `(inventory_source_id, vmid)`, never
  durable identity. Durable identity is an opaque backend-generated `resource_id` (UUID)
  per inventory *incarnation*.
- `locator_generation` orders accepted handoffs of a slot; `resource_continuity_revision`
  is a separate monotonic security/concurrency token. Neither is derived from VMID, name,
  type, or node.
- Resource state is split into independent axes: presence, lifecycle, observational
  continuity, security continuity, detail status, node availability, state level —
  disjoint, no automatic promotion between them (`discovered != observed != managed !=
  maintenance`, plus a separate `break_glass` path).
- Discovery (`app/inventory/discovery.py`, `reconciliation.py`, `provider.py`) is strictly
  read-only: it can only create/update `discovered`-level facts, never grant management,
  trust, or destructive capability.
- `app/inventory/publication.py` assembles the canonical published snapshot (sources,
  nodes, resources, revisions) in one consistent SQLite read transaction; it's the producer
  side of the contract validated in `custom_components/hubinet_ops/contract/`.
- `app/inventory/store.py` / `authority.py` own the durable schema, CAS/fencing for
  discovery-run ownership, and the backend/source/global-revision bookkeeping.

`app/inventory_runtime.py` is the current production composition root
(`create_read_only_app`/`create_app_from_env`, `uvicorn app.inventory_runtime:app`). It
constructs the authority store/authority/publication objects, a production
`ProxmoxHttpTransport` (GET-only, mandatory TLS verification, no mutation-verb escape
hatch), the `R0Scheduler` discovery orchestrator, and the read-only `GET /r0/v1/health`,
`/backend`, `/snapshot` HTTP API (bearer auth, no mutation route of any kind). It has a
documented, test-enforced legacy-import denylist — see
`tests/test_r0_architecture_regression.py` and `tests/test_no_legacy_runtime_surface.py`
— and opens its own fresh/separate authority database with no legacy migration path.
There is no static resource/VMID configuration anywhere in this path: `app/
inventory_runtime_config.py` configures only how to identify and reach a Proxmox
*source*; every node/LXC/QEMU resource is discovered dynamically.

`custom_components/hubinet_ops/` is the Home Assistant custom integration consuming that
snapshot over HTTP (`transport_http.py`): `coordinator.py` (one `DataUpdateCoordinator`,
one snapshot fetch per refresh), `contract/` (structural validation — enums/models/
primitives/source/resource/snapshot/transition — the "Phase 0 snapshot oracle"), `api.py`
(thin transport facade), `config_flow.py`, `sensor.py`, `entity.py`, `diagnostics.py`
(redacts secrets recursively). The coordinator is explicitly **not** a reconciler: it
never infers `missing` from a diff between two polls, and it never assumes revision
`N -> N+1` implies backend transaction adjacency (publications can skip arbitrary
intermediate states — see the "polling-gap test" in
`.agents/skills/hubinet-phase-boundary/SKILL.md` before adding any HA-side invariant).

Whether new work belongs in the backend (`app/inventory/`) or in the HA-side
validation/presentation layer (`custom_components/hubinet_ops/`) is a real design
question — use the `hubinet-phase-boundary` skill rather than guessing, especially for
anything touching freshness, reconciliation, run history, CAS/fencing, or authority.
Mutation/policy/jobs/locks authority remains unimplemented — do not wire mutation
capability into any current module without an explicit activation/cutover review.

## Repository map

- `app/` — the 0.5 inventory/runtime subsystem only: `app/inventory/` (durable authority),
  `app/inventory_runtime.py` / `inventory_runtime_config.py` / `inventory_scheduler.py` /
  `inventory_pve_transport.py` (R0 read-only composition root, config, scheduler, and PVE
  transport).
- `custom_components/hubinet_ops/` — Home Assistant custom integration (0.5 contract +
  coordinator + presentation over HTTP).
- `config/inventory.example.yaml` — source-centric R0 bootstrap config (no static
  resource/VMID inventory); `.env.r0.example` — the paired secrets template.
- `deploy/hubinet-ops-0.5.service`, `deploy/install-0.5.0-fresh.sh`,
  `deploy/README-0.5-firewall.md` — the in-CT deployment path: a fresh,
  clean-install-only unit/installer bound to `0.0.0.0:8787`, paired with mandatory
  firewall-policy documentation. Never execute the installer against a real host from an
  agent session; it is validated with `bash -n` only, plus
  `tests/test_deploy_0_5_fresh_install.py`'s text-level checks.
- `deploy/bootstrap-proxmox-0.5.sh` + `deploy/lib/bootstrap-*.sh`,
  `deploy/README-bootstrap-proxmox-0.5.md` — the primary product-facing PVE-host
  entrypoint: automates next-free-VMID-auto-detected, fresh unprivileged Debian 13
  LXC creation, least-privilege PVE identity provisioning (effective permissions
  verified as an exact set — `{Sys.Audit, VM.Audit}` and nothing else, never a
  blacklist), PVE TLS trust (system-trust fallback only via explicit operator
  opt-in, never implicit), git-commit-provenance-gated source deployment invoking
  `install-0.5.0-fresh.sh` unmodified inside the new CT, source-centric config
  generation, and the mandatory nftables firewall boundary (exact rule
  content/order verified against resolved numeric addresses -- a hostname
  `--pve-endpoint` is resolved inside the CT before the ruleset is generated),
  in a fixed fail-closed phase order with no host mutation before a single upfront
  operator confirmation of the full plan; CT `onboot` is enabled only after a real,
  contract-grounded discovery-acceptance check (`deploy/lib/hubinet-ops-bootstrap-accept.py`,
  proving a committed/fresh/current result, not merely `health == "healthy"`) succeeds.
  PVE-identity rollback deletes only what a run can prove it owns (a random
  per-run ID embedded in PVE object comments), never merely an object matching
  the fixed name, closing a cluster-wide TOCTOU ownership race. Secrets (the PVE
  token, the R0 API bearer token) are never passed as a literal command-line
  argument anywhere in this path. `.github/workflows/bootstrap-smoke.yml` wires
  the compliant Docker sandbox into GitHub Actions (path-filtered, PR-triggered).
  Never execute it against a real host from an agent session. Two disjoint test
  files exist per AGENTS.md's deployment-script sandbox boundary:
  `tests/test_bootstrap_proxmox_0_5.py` (local-safe: `bash -n`, the restored
  `scripts/validate_hermetic_shell_boundary.py` lexical validator, and static
  content checks — never executes the real script, proven by an AST-based
  self-guard) and `tests/test_bootstrap_proxmox_0_5_smoke.py` (the only file that
  executes the real script, against the hermetic fake-command layer in
  `tests/_bootstrap_fake_pve.py` — fake `pct`/`pveum`/`pveam`/`pvesh`/`pvesm`/
  `nft`/tooling-provisioning/discovery-acceptance commands on a temporary `PATH`;
  every test hard-skips unless `HUBINET_OPS_SYSTEM_SANDBOX=1`, set only by
  `tests/shell/run_bootstrap_smoke_sandbox.sh`'s Docker-isolated, ephemeral-CI-only
  sandbox — see that script and `tests/shell/Dockerfile.bootstrap-smoke`/
  `bootstrap_smoke_sandbox_entrypoint.sh`). Do not run
  `test_bootstrap_proxmox_0_5_smoke.py` locally with the marker forced on and
  report it as merge-safety evidence — only a real run through the sandbox
  counts.
- `docs/architecture/` — the documentation index (`README.md`), ADR 0001/0002, the
  inventory model/foundation, the R0 activation design contract, and the current
  implementation status (authority for 0.5 work; see above).
  `docs/operations/` — the R0 operational activation runbook and HA clean-break/purge
  plan for a real deployed instance.
  `docs/product-intent.md` — the operator-stated current product target.
  `docs/archive/` — **non-authoritative history**: the superseded security-proof
  architecture (former ADR 0003–0006), superseded Family B / B-S1 research, postmortems,
  and the verbatim R0 activation chronology. Never read by default; never a roadmap.
- `scripts/` — `validate_yaml.py` (repository-wide YAML parse check),
  `check_tracked_files.py` (fails CI if `.env`/`config.yaml`/secrets/DBs/keys/logs are
  tracked in git).
- `tests/` — pytest suite (fake `ReadOnlyProviderTransport`/clocks/SQLite only). Test file
  naming is `test_<subsystem>.py`.
- `.agents/skills/` — repo-specific agent procedures (contract review, phase boundary,
  architecture change, test/evidence gate). Procedures never override `AGENTS.md` or an
  ACCEPTED ADR.

Obsolete 0.2.x/0.3.x/0.4.x implementation, deployment, and Home Assistant presentation
surfaces (the legacy `app/main.py`/`OpsService`/`Database`/MQTT composition root, static
VMID config/deploy/`home-assistant/` package-dashboard surfaces, and their version-scoped
tests) have been retired from this tree; do not recreate them here — see
`tests/test_no_legacy_runtime_surface.py` for the enforced boundary and Git history/tags
for the historical source.

## Working conventions specific to this repo

- Never commit API tokens, MQTT passwords, webhook IDs, SSH keys, production addresses,
  runtime databases, or logs — `scripts/check_tracked_files.py` enforces this in CI, and
  the working tree already has many local test-artifact directories (`.pytest-tmp-*`,
  `.tmp-pytest-*`, `*.log`) that must stay untracked.
- Every `/r0/v1` endpoint requires bearer auth; never add one that doesn't.
- Never add an API, MQTT topic, SSH path, or host-control operation that accepts
  arbitrary command text — a future mutation path must stay typed and allowlisted.
- Never add a static resource/VMID configuration concept anywhere in the current 0.5
  path — adding/removing a PVE guest must never require a repository or config change.
- Treat green tests as evidence, not proof — see `hubinet-test-gate` before declaring
  something done, ready, or merge-safe, and don't weaken an existing test or fail-closed
  invariant just to make a change pass.
