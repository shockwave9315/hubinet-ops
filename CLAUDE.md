# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repository is

Hubinet Ops is a policy-controlled Proxmox inventory, telemetry, lifecycle, snapshot, and
manually-approved APT maintenance service, with a Home Assistant frontend. Two tracks live
in this repo at once:

- **Legacy 0.4.x (production, `app/*` top-level modules)** — the stable, shipped service
  documented in `README.md` and `docs/architecture.md`. Production inventory is exactly
  VM/CT 100–110.
- **0.5 clean-break rewrite (`app/inventory/`, `custom_components/hubinet_ops/`)** —
  a from-scratch inventory/identity model under active development. It is **implemented but
  dormant**: nothing in production startup, HTTP, scheduling, or mutation authority uses it
  yet. Do not wire it into runtime without an explicit activation/cutover review. Current
  status is tracked in `docs/architecture/0.5-implementation-status.md`.

**Before any architecture or implementation work, read `AGENTS.md` in full.** It is the
binding rules file for every coding agent in this repo (identity model, mutation trust
boundary, fail-closed security invariants, test boundaries). This CLAUDE.md summarizes
where things live and how to run things; `AGENTS.md` and the ACCEPTED architecture docs
below are the actual authority and take precedence over anything here.

Normative 0.5 architecture, in order of authority:
1. `docs/architecture/adr/0001-resource-identity-incarnation.md` (ACCEPTED)
2. `docs/architecture/adr/0002-proxmox-discovery-reconciliation.md` (ACCEPTED)
3. `docs/architecture/0.5-foundation.md` (ACCEPTED — Phase 0 decisions; partly in Polish)
4. `docs/architecture/0.5-inventory-model.md` (ACCEPTED — materializes the two ADRs)
5. `docs/architecture/0.5-implementation-status.md` — current status, NOT an authority; if
   it conflicts with an ACCEPTED ADR, the ADR wins and the status doc must be corrected.

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
python -m compileall -q app tests
pytest -q
python -m py_compile deploy/managed/hubinet-maint deploy/pve/hubinet_ops_host_control.py deploy/pve/hubinet_ops_hostd.py
python scripts/validate_managed_profiles.py
python scripts/validate_yaml.py
python scripts/generate_ha_dashboard.py --check
bash -n deploy/upgrade-0.4.2-from-pve.sh
bash -n deploy/install-ha-0.4.2-from-pve.sh
bash -n deploy/pve/hubinet-ops-host
python scripts/check_tracked_files.py
```

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

The deployment "runtime smoke" (`tests/shell/run_runtime_smoke_sandbox.sh` /
`runtime_smoke_0_4_1.sh`) is **CI-only**: it runs a real deployment script inside a
Docker sandbox that only exists on an ephemeral GitHub-hosted runner carrying the
workflow-owned `HUBINET_OPS_EPHEMERAL_CI=1` marker. Never invoke it directly or run
deployment scripts against a real host from this environment.

Tests never contact real Proxmox, LXC/QEMU guests, Home Assistant, MQTT, or any
private-network endpoint — they use fake executors, fake clocks, temporary SQLite
databases, and stubbed commands.

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

## Architecture (legacy 0.4.x, currently production)

Request/data flow: telemetry loop and `monitoring_scheduler` call into `ResourceExecutor`,
which selects a validated adapter (LXC/APT, QEMU/HAOS read-only, or CT110
self-inspection). SQLite (`app/database.py`) is authoritative for plans, jobs, events, and
normalized resource state; MQTT (`app/mqtt.py`) and Home Assistant are projections only.

Manually-approved update lifecycle for CT101–CT109:
```
scan → waiting approval → preflight → snapshot (policy) → update → stabilization → verification → terminal result
```
VM100 (`haos`) and CT110 (`agent_self`, the backend-hosting container) never enter this
lifecycle; VM100 is observation-only plus typed Hubinet-owned QEMU snapshot create/
list/delete. CT110 self-inspection never recursively calls its own API/SSH.

Full mutation trust path (never shortcut this — see `AGENTS.md` for exact gates):
```
Home Assistant → Hubinet Ops API → backend policy/plans/jobs/locks/audit
→ typed host-control → hostd/forced-command → Proxmox
```
`deploy/pve/hubinet_ops_host_control.py` (forced-command wrapper) and
`deploy/pve/hubinet_ops_hostd.py` (durable hostd daemon) share one host-control
implementation that revalidates action, VMID, resource type, and every capability
(observation/managed/maintenance/lifecycle/host-control/snapshot-name) independently
of the backend. The PVE SSH key is scoped to that implementation only — no shell or
free-form command text is ever accepted anywhere in this path.

There is one narrow legacy break-glass exception: offline recovery for CT110 (the
backend-hosting container), which can bypass the backend only through a dedicated
recovery credential and only for typed offline force-stop/snapshot-restore ops. See
`docs/recovery.md` and the "Transitional legacy 0.4 break-glass exception" section of
`AGENTS.md` before touching this path.

Key legacy modules in `app/`: `service.py` (orchestration), `executor.py` +
`resource_adapters.py` (per-resource-type operations), `host_control.py` (typed PVE
client), `state.py` (normalized resource state), `database.py` (SQLite), `mqtt.py` +
`mqtt_budget.py` (telemetry/discovery, bounded churn), `ha_entities.py` (entity
generation), `security.py` (bearer auth), `config.py` (`config.yaml` loading/validation).

## Architecture (0.5 rewrite, dormant)

`app/inventory/` is an independently instantiable subsystem — a separate SQLite
"authority" database (schema v3, marker `hubinet_ops_0_5_authority`), not the legacy
`Database`/`OpsService`. It has its own identity model, summarized from ADR 0001/0002:

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

`custom_components/hubinet_ops/` is the Home Assistant custom integration consuming that
snapshot: `coordinator.py` (one `DataUpdateCoordinator`, one snapshot fetch per refresh),
`contract/` (structural validation — enums/models/primitives/source/resource/snapshot/
transition — the "Phase 0 snapshot oracle"), `api.py` (thin transport facade),
`config_flow.py`, `sensor.py`, `entity.py`, `diagnostics.py` (redacts secrets recursively).
The coordinator is explicitly **not** a reconciler: it never infers `missing` from a diff
between two polls, and it never assumes revision `N -> N+1` implies backend transaction
adjacency (publications can skip arbitrary intermediate states — see the "polling-gap
test" in `.agents/skills/hubinet-phase-boundary/SKILL.md` before adding any HA-side
invariant).

Whether new work belongs in the dormant Phase 1 backend (`app/inventory/`) or in Phase 0
HA-side validation/presentation (`custom_components/hubinet_ops/`) is a real design
question — use the `hubinet-phase-boundary` skill rather than guessing, especially for
anything touching freshness, reconciliation, run history, CAS/fencing, or authority.

## Repository map

- `app/` — legacy 0.4.x service (API, policy/service, adapters, SQLite, MQTT, state) plus
  the dormant `app/inventory/` 0.5 subsystem.
- `custom_components/hubinet_ops/` — Home Assistant custom integration (0.5 contract +
  coordinator + presentation); `vendor/home-assistant-core/` holds the reference
  `proxmoxve` integration these patterns were derived from (not imported at runtime).
- `config/config.example.yaml` — production-shaped resource inventory, no credentials.
- `deploy/pve/` — shared typed host control, forced-command wrapper, durable hostd,
  systemd unit, versioned allowlists (`*-vmids`, `resource-types`).
- `deploy/managed/` — fixed LXC executor (`hubinet-maint`), transactional installer,
  CT101–109 profiles.
- `deploy/upgrade-*.sh` / `deploy/install-ha-*.sh` — versioned transactional
  upgrade/install scripts, one pair per shipped release; each is validated with `bash -n`
  only (the real deployment smoke runs exclusively in the CI sandbox, see Commands).
  Never execute these against a real host from an agent session.
  `deploy/pve/hubinet-ops-self-update` handles CT110 self-update.
  `deploy/pve/hubinet_ops_release.py` builds release bundles.
- `home-assistant/` — legacy package/dashboard YAML and secret examples for the 0.4.x
  MQTT-based integration (distinct from `custom_components/hubinet_ops/`).
- `docs/architecture/` — 0.5 ADRs and status (authority for 0.5 work; see above).
  `docs/*.md` (non-`architecture/`) — 0.4.x design/API/security/deployment docs and
  per-version upgrade notes.
- `scripts/` — `generate_ha_dashboard.py` (deterministic Lovelace generator, `--check`
  mode), `migrate_config_0_4_*.py`, `validate_*.py` (YAML, managed profiles, HA secrets,
  rollout state, hermetic shell boundary, PVE snapshot policy), `check_tracked_files.py`
  (fails CI if `.env`/`config.yaml`/secrets/DBs/keys/logs are tracked in git).
- `tests/` — pytest suite (fake executors/clocks/SQLite only); `tests/fixtures/`,
  `tests/shell/` (shell-script test harness incl. the CI-only sandbox smoke runner).
  Test file naming: most are `test_<subsystem>.py`; version-scoped regression suites are
  `test_v0XY_<topic>.py` — check for one of those before adding a new file for a
  version-specific fix.
- `.agents/skills/` — repo-specific agent procedures (contract review, phase boundary,
  architecture change, test/evidence gate). Procedures never override `AGENTS.md` or an
  ACCEPTED ADR.

## Working conventions specific to this repo

- Never commit API tokens, MQTT passwords, webhook IDs, SSH keys, production addresses,
  runtime databases, or logs — `scripts/check_tracked_files.py` enforces this in CI, and
  the working tree already has many local test-artifact directories (`.pytest-tmp-*`,
  `.tmp-pytest-*`, `*.log`) that must stay untracked.
- Every `/api/v1` endpoint requires bearer auth; never add one that doesn't.
- Never add an API, MQTT topic, SSH path, or host-control operation that accepts
  arbitrary command text — host-control stays typed and allowlisted.
- Treat green tests as evidence, not proof — see `hubinet-test-gate` before declaring
  something done, ready, or merge-safe, and don't weaken an existing test or fail-closed
  invariant just to make a change pass.
