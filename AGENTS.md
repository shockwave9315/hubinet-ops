# Hubinet Ops — agent rules

Binding for every coding agent in this repository.

## Orientation

Read this file, then the one current document relevant to your task, then the
code. That is the whole reading set.

| Document | Read it when |
| --- | --- |
| `PRODUCT.md` | what the product is and its hard rules |
| `ARCHITECTURE.md` | how the current system is built |
| `STATUS.md` | what exists today and what is next |
| `README.md` | install and development entry points |

There is no ADR hierarchy, no phase/wave/blocker taxonomy, and no archive.
Historical material lives in Git history and is not a roadmap. If you find a
reference to a document that no longer exists, delete the reference.

## Product direction

```text
PVE autodiscovery -> dynamic backend inventory -> dynamic Home Assistant
  -> package scanning -> operator-approved update plan -> job-owned snapshot
  -> update -> healthcheck -> same-job rollback
```

The hard rules are in `PRODUCT.md` and are binding: NO AUTO-UPDATE; a changed
plan invalidates approval; a fresh Hubinet-owned snapshot per update job;
rollback only to that job's snapshot; Hubinet cleanup never touches manual
snapshots; a failed scan is unknown, never zero updates; failed or partial
discovery never deletes a resource.

Building the unbuilt parts is ordinary implementation work. It does not need a
design document written first.

## Threat model

Hubinet Ops runs in a **trusted, self-administered** Proxmox environment.

**TRUSTED**

- the Proxmox administrator/root;
- the Proxmox host;
- root inside a managed guest;
- the Hubinet operator;
- normal `apt`/`dpkg` behavior.

**OUT OF SCOPE — do not design defenses for these**

- a malicious root inside a managed guest deliberately trying to fool Hubinet;
- a malicious or compromised Proxmox root;
- a malicious administrator;
- deliberate modification of Hubinet's own state by the environment owner;
- formal security proofs intended to survive full administrative compromise.

Do not reintroduce, and do not require: workload-incarnation cryptographic
proof, source or node attestation, attestation epochs, relationship gates,
dual-evidence confirmed removal, trust-granting continuity states, hostile-root
APT race defenses, or pmxcfs/task-history completeness proofs. That
architecture was removed deliberately; see Git history if you need to know what
it said.

## Ordinary safety — always in force

Retiring the hostile-admin threat model does not relax normal engineering
safety:

- least-privilege PVE credentials (the exact verified minimum set);
- mandatory TLS verification; system-trust fallback only on explicit opt-in;
- secrets never in argv, logs, diagnostics, or the published snapshot;
- typed, allowlisted operations — never arbitrary shell command text, and never
  an API/topic/host-control path that accepts one;
- validate the current target/VMID immediately before any mutation;
- bearer authentication is required on every API endpoint except the
  deliberately unauthenticated minimal `/r0/v1/health` liveness probe, which
  exposes no inventory or credential data;
- concurrency protection against ordinary operational races (durable ownership
  CAS, single-flight, fencing, restart recovery);
- exact plan fingerprint and revalidation before an approved update runs;
- job-owned snapshots, same-job rollback, manual snapshots untouched;
- failed/unavailable discovery is never deletion; a failed scan is never zero.

Never add a static resource/VMID configuration concept: adding or removing a
PVE guest must never require a repository or config change.

## Repository workflow

- Pre-release authority schema versions are not migrated in place; a schema
  bump may require a fresh deployment and database. Do not add authority schema
  migrators unless the operator explicitly changes this product decision.
- Verify a clean worktree, fetch `origin`, and confirm the branch and its
  remote head before starting. Stop and ask if the worktree is dirty or history
  has diverged. If you are on `main`, create or switch to a feature branch
  before implementing anything.
- **Delivering assigned implementation work includes shipping it.** Once you
  have been explicitly assigned work on a feature branch, finishing it means
  making coherent commits, pushing them to that remote feature branch, and
  using the stage's existing Draft PR. Do not leave completed assigned work
  sitting only in the local worktree, and do not open a second PR when the
  current stage already has one.
- **Do not change review state or rewrite history unless explicitly told to:**
  no merging, marking ready for review, closing/reopening PRs, resolving review
  threads, editing PR title/body, `rebase`, `reset --hard`, force-push,
  auto-`stash`, or opening unrelated issues/PRs.
- Never commit tokens, passwords, webhook IDs, SSH keys, production addresses,
  runtime databases, or logs. `scripts/check_tracked_files.py` enforces this.

## Tests

Tests must enforce behavior, not restate it. Exercise failure, race, restart,
fencing, idempotency, and fail-closed paths, not only happy paths. Never weaken
a test or a fail-closed invariant to make a change pass.

- Tests never contact real Proxmox, LXC, Docker, Home Assistant, MQTT, or any
  private-network endpoint. Use the fake transport, fake clocks, temporary
  SQLite databases, and simulated process output.
- Never run real `apt`, `pct`, `ssh`, `systemctl`, or deployment operations from
  a test on the pytest host.
- A deployment script may execute only inside the repository's system-enforced
  smoke sandbox on an ephemeral CI runner. The sandbox is the security
  boundary: no network, no host PID namespace, no capabilities, no privileges,
  no host sockets/secrets, no writable host filesystem; non-root, read-only
  root and repository, one-shot writable workspace and `/tmp`. Inside it the
  harness sets `HUBINET_OPS_TEST_MODE=1`, uses an isolated `PATH` that does not
  inherit the host's, and replaces every privileged or external command with a
  fake. Do not run `tests/test_bootstrap_proxmox_0_5_smoke.py` locally with the
  marker forced on and report it as evidence.
- `scripts/validate_hermetic_shell_boundary.py` is a conservative lexical
  scanner and defense in depth, not the execution boundary. It must fail closed
  on host `PATH` access, absolute standard executable paths, `command -p`, and
  Bash `/dev/tcp` / `/dev/udp` networking. The deployment scripts must not read or modify
  `PATH` or reference `/bin`, `/sbin`, `/usr/bin`, `/usr/sbin` — the exact
  first-line `#!/usr/bin/env bash` shebang is the only exception.

Validation should be proportional. Targeted tests plus `git diff --check` for a
small change; the bounded affected family for a bugfix. Run the full validation
from `README.md` before publishing or merging a runtime change, or when
claiming done/ready/merge-safe. Green tests are evidence that the covered
behavior passed, not proof that no untested counterexample exists — do not
claim more than the evidence supports.

The Home Assistant suite is pinned separately and needs Python ≥ 3.14.2; Linux
CI is the real gate. Do not patch Home Assistant or fake `fcntl` to run it on
Windows.

## Review

**Finding standard.** A P1/P2 must demonstrate a concrete correctness,
regression, ordinary operational-safety, least-privilege, secret-handling,
injection, targeting, concurrency, or data-integrity failure, with a reachable
witness.

**Out of scope, and never a blocking finding:** anything whose required
attacker is a malicious PVE administrator/root, or a malicious root inside a
managed guest. Say so and move on.

A hardening idea that no current rule requires is a follow-up suggestion, not a
blocker. Label it as a new decision rather than inventing a blocker.

**STOP rule — one witness means one bounded family.** When you find one real
defect:

1. identify the semantic dimensions of that exact bug family;
2. inspect the sibling states that could bypass the same invariant;
3. fix it as one family-level rule, not one instance;
4. add regression coverage for the witness plus positive controls that keep
   legal behavior legal;
5. **stop.** Do not restart a broad audit or expand into unrelated code without
   a new concrete witness.

Do not opportunistically refactor adjacent code inside a targeted fix.

**Home Assistant polling.** HA may skip arbitrary published revisions. Never
infer `N -> N+1` adjacency, hidden intermediate state, or backend transaction
adjacency from two snapshots HA observed. Anything needing durable history
belongs in the backend, not in the integration.

## Closing a fix

Before calling a substantial fix complete, re-check the family: original
witness closed; legal path still legal; fail-closed path still fail-closed;
persistence/restart behavior retained; transaction atomicity retained; no scope
leakage. If a row is open, it is not done.
