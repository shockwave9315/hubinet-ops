# In-place Hubinet Ops update

`deploy/update-proxmox-0.5.sh` updates an **existing** Hubinet Ops
installation in place: install once, update many times. It is a separate
PVE-host operator action from `deploy/bootstrap-proxmox-0.5.sh` — never a
second bootstrap. It never invokes `deploy/install-0.5.0-fresh.sh`, never
recreates the LXC or changes its VMID/network, and never rotates the PVE
identity/token secret, the HA bearer, the host-control key, or the pinned
`known_hosts`.

Fresh bootstrap remains the only path for a genuinely new installation, an
explicit disaster-recovery rebuild, or deliberately rebuilding a test
installation. This updater assumes the CT named by `--vmid` already exists
and was created by `deploy/bootstrap-proxmox-0.5.sh`.

## Prerequisites

- A Proxmox VE host you can run commands on as root (the script must run
  **on** the PVE host itself, exactly like bootstrap).
- `pct`, `pveum`, `git`, and `python3` on the PVE host.
- An existing Hubinet Ops CT, created by `deploy/bootstrap-proxmox-0.5.sh`,
  currently running with `hubinet-ops` active and enabled.
- A **clean git checkout** of the target source on the PVE host (no
  uncommitted changes) at the exact commit you want to update to — the
  same source-provenance rules as bootstrap apply: real git checkout, one
  exact full 40-character commit SHA, never a non-git tarball.

## Usage

```bash
sudo ./update-proxmox-0.5.sh --vmid 110
```

This is interactive by default: it prints the exact plan (see below) and
asks one confirmation before staging or touching anything. If the target
authority schema is incompatible with the installed one, a **second,
dedicated confirmation** is required — the ordinary plan confirmation is
never sufficient for a destructive database reset.

Non-interactive (for scripted use):

```bash
sudo ./update-proxmox-0.5.sh --vmid 110 \
  --source-dir /path/to/checkout \
  --expected-sha <full-40-character-sha> \
  --non-interactive --yes
```

`--non-interactive` requires `--expected-sha`. `--yes` skips only the
*ordinary* plan confirmation — it never bypasses source-provenance
validation, and it never by itself authorizes a destructive authority
reset (see below).

Inspect the plan without changing anything:

```bash
sudo ./update-proxmox-0.5.sh --vmid 110 --dry-run
```

Normal and dry-run invocations are serialized per VMID. If another updater
process currently owns that VMID, the second invocation fails immediately
before ownership verification or planning. An update for a different VMID is
independent.

### Flags

| Flag | Meaning |
| --- | --- |
| `--vmid <N>` | **Required.** The existing Hubinet CT's VMID. |
| `--source-dir <path>` | Default: this script's own repository root. Must be a clean git checkout. |
| `--expected-sha <sha>` | The exact commit to update to. Required with `--non-interactive`. |
| `--non-interactive` | Fail closed instead of prompting for any missing required value. |
| `--yes` / `-y` | Skip the ordinary plan confirmation only. |
| `--allow-authority-reset` | Explicit non-interactive authorization for a destructive authority-database reset. Must be combined with `--yes`. |
| `--dry-run` | Inspect, classify, and print the plan; makes zero managed-state mutation. |

## What it verifies before touching anything

Given `--vmid`, the updater proves the CT is the expected installation
before any mutation:

1. the CT exists and is running, and has the paths a real installation has
   (config, app, venv, unit, authority database);
2. its host-control public key comment carries the exact
   `hubinet-ops-package-scan-vmid-<vmid>-<run-id>` marker shape bootstrap
   creates;
3. exactly one PVE-host `authorized_keys` line carries that same marker,
   and its forced-command path matches the expected Hubinet helper shape;
4. that helper file exists as an executable, root-owned file;
5. the PVE user/token comments carry the same run-id;
6. the token's effective PVE permissions are exactly `Sys.Audit,VM.Audit`;
7. **no package-update job is ACTIVE.** The updater asks the running
   installation's own authenticated `GET /r0/v1/package-update/active` route.
   Once workload mutation is live, replacing the backend or its privileged
   helpers while a job owns a snapshot, mutation, or rollback journal can pair
   a new backend with a half-replaced helper set for an operation already in
   flight, so this refuses outright. There is deliberately **no bypass flag**:
   let the update finish, or resolve it through the product's own
   `resume_update` / `rollback_update` controls, then run the updater again.

   A backend predating production activation answers 404 for that route. That
   is a real answer — such a backend has no update worker and cannot own a
   workload job — and the updater proceeds. Any *other* failure to ask the
   question stops the run: "we could not ask" is never read as "the answer was
   no".

Any mismatch stops the run before anything is staged or mutated. All seven
checks run in Phase U2, which is strictly before staging, before the service
is stopped, and before any helper, key, config file, or unit is touched.

This whole chain, plus a small bounded fingerprint of the approved plan
(the installed requirements.txt/unit/PVE-helper content, the authority
schema identity, and `backend_instance_id`), is re-verified again
*immediately before the first mutation* — after staging, right before the
service's boot activation is temporarily disabled. This catches a
legitimate PVE operator/tool action between planning and mutation (e.g. a
different CT restored as this same VMID, or a snapshot restore that rolls
live state backward while keeping the same installation identity). A
mismatch here stops the run the same way, before anything is mutated;
rerun planning/approval. An ordinary background discovery cycle while you
read the plan is never a mismatch.

## What the plan shows

Before staging or mutating anything, the updater prints:

- the VMID, installed source commit (or `unknown (pre-updater install)` for
  a pre-updater installation), and target source commit;
- `backend_instance_id` before the update;
- whether `requirements.txt`, the systemd unit, and the PVE host helper are
  changed (exact content comparison against the target commit) — unchanged
  artifacts are never touched, never rebuilt, never rewritten;
- the installed and target authority schema versions, and whether the
  authority action is `preserve` or a destructive `reset-required`;
- what will happen to the five package-update forced-command boundaries:
  unchanged, replaced in place, or newly provisioned (which is what upgrading
  a pre-activation installation into the activated lifecycle does);
- that no workload update job is active — verified before the plan is printed;
- whether Home Assistant re-enrollment will be required afterward.

## What is always preserved

An ordinary code-only update never touches: the LXC, its VMID, its
IP/network, the PVE user/role/token, the PVE token secret,
`/etc/hubinet-ops/inventory.yaml`, `/etc/hubinet-ops/agent.env`, PVE CA
trust material, the host-control private/public keys, pinned `known_hosts`,
any forced-command `authorized_keys` line, nftables, or the authority
database (when the schema is unchanged). The package-scan boundary is never
rotated or rewritten by this updater, and neither is any package-update
boundary whose helper already matches the target commit.

## Activating the update lifecycle on an existing installation

An installation bootstrapped before production activation has no
package-update boundaries. Updating it to an activation build provisions
them, and that is the one thing this updater does which creates NEW
privileged access paths:

- five root-owned forced-command helpers on the PVE host;
- five dedicated private keys inside the CT, one per boundary;
- five `authorized_keys` lines, each naming a different forced command;
- three root-only (`0700`) operation journals under `/var/lib/hubinet-ops/`;
- one appended `package_update:` block in `/etc/hubinet-ops/inventory.yaml`.

The configuration block is **appended**, never merged: it is one
self-contained top-level YAML key, so every other line of your configuration
stays byte-for-byte as it was.

**A failed activation update leaves no new privileged access path behind.**
Every artifact above is recorded in the run's durable journal *before* it
exists, and rollback removes exactly what this run created — helper, key, and
authorization together, with the authorization removed first so the shortest
possible window is also the safest ordering. It removes only what this run
created: an unrelated operator `authorized_keys` entry, and the package-scan
boundary from the original bootstrap, both survive untouched. A journal
directory that already existed is never removed either — it may hold another
operation's durable at-most-once evidence, and destroying that to tidy up
would be strictly worse than leaving an empty directory.

An already-activated installation whose helper content matches the target
commit is left completely alone: no key rotation, no re-authorization, no
rewrite.

## Destructive authority reset

Hubinet Ops's authority schema is pre-release: an incompatible schema has
no in-place SQL migration. If the target schema differs from the installed
one, the updater states this in the plan and requires explicit
authorization:

- **interactively**, a second confirmation dedicated to the reset (typing
  `reset`, not merely `y`);
- **non-interactively**, both `--yes` and `--allow-authority-reset` — `--yes`
  alone is never sufficient.

If authorized, the updater:

1. makes one coherent SQLite backup of the current authority database
   (the stdlib online backup API, integrity-checked and re-validated
   against the pre-reset identity), retained under
   `/var/lib/hubinet-ops/update-backups/<run-id>/authority.db` inside the
   CT and never auto-deleted;
2. removes the live authority database only after that backup is
   validated;
3. starts the target runtime, which creates its own fresh schema on next
   start — the updater never writes authority schema DDL itself.

The LXC is not recreated. Its VMID/IP/network, PVE identity/token secret,
HA bearer, config, TLS material, host-control key, and firewall are all
preserved. What is lost is only what lived in the authority database:
`backend_instance_id`, inventory/node/resource identity, retained
missing/replaced history, discovery-run history, package-scan history and
exact package rows, and exact-plan approvals.

**Home Assistant re-enrollment is required after a destructive reset**,
because `backend_instance_id` and every resource identity are regenerated
— the existing config entry will report `wrong_instance` until you add the
integration again. This is a manual, separate operator action; the updater
never attempts automatic backend adoption.

A code-only update (the common case) never touches the authority database
and never requires re-enrollment.

## Failure and rollback

The updater holds one kernel-backed PVE-host lock for the selected VMID from
before ownership/planning through final cleanup. The stable lock file is
`/var/lib/hubinet-ops/update-state/vmid-<vmid>.lock`; ownership is the open
`flock` descriptor, so SIGKILL or reboot releases it without PID-file stale
lock handling.

After ownership is first proven and before planning, the updater creates one
small durable journal at
`/var/lib/hubinet-ops/update-state/vmid-<vmid>.journal`. It contains no
credentials: only the VMID, update and installation run IDs, rollback-armed
state, bounded rollback facts/markers, and the authority backup reference if
one exists. Each destructive transition is preceded by a flushed temporary
journal write, atomic rename, and state-directory flush.

Before the old service is stopped, any failure (staging, source-provenance
recheck) leaves the existing installation completely untouched.

Note that when `requirements.txt` changes, building the new virtualenv is
part of the maintenance window rather than something prepared beforehand: a
virtualenv is not relocatable, so the environment is created directly at
`/opt/hubinet-ops/.venv` while the service is stopped. Expect a
dependency-changing update to take noticeably longer than an ordinary
code-only one, which does not touch the environment at all. A build failure
there rolls back to the pre-update environment and restarts the old
service.

### Boot activation during the update window

An update rewrites several pieces of one runtime, so there is a window in
which the installation on disk is deliberately incoherent. Your CT keeps
`onboot=1` and would come back by itself after a PVE host power loss, and an
enabled `hubinet-ops.service` would then be auto-started by systemd — against
a half-swapped installation, long before you had a chance to rerun the
updater.

The updater therefore **temporarily disables `hubinet-ops`'s boot
activation** for its mutation window. As the first mutation it re-proves the
unit is currently enabled, records that intent durably, runs
`systemctl disable hubinet-ops`, and then proves systemd's own
`UnitFileState` really is `disabled` before anything else is touched.

- Your CT's `onboot` setting is **not** changed, and the unit is never
  masked or replaced.
- `disable` does not stop a running service; the updater's own explicit stop
  does that a moment later.
- The updater still starts the service by hand for target acceptance —
  systemd permits starting a disabled unit — so the new build is fully
  exercised while it still cannot auto-start at boot.
- The unit stays disabled through target start, discovery/host-control/
  firewall acceptance, and installed-source marker activation.
- `systemctl enable hubinet-ops` is issued, and positively proven, only
  after the target is fully accepted with a coherent source marker — and on
  every rollback or recovery path before recovery is declared complete.

So if the PVE host or the CT reboots mid-update, Hubinet Ops simply does not
come up until the updater has finished recovering it. A finished, successful
update always leaves the service **enabled and active** again; if it did not,
the run failed and rolled back, or stopped hard and told you so.

Once boot activation or a service stop is attempted, a failure at any later point — including
a stop command that changed systemd state but returned failure, and a failure
partway through swapping any single artifact, not only one after that swap
fully completes — triggers a coherent rollback: every changed
artifact (app payload, virtualenv + `requirements.txt`, systemd unit, PVE
host helper, and the installed-source marker) is restored from its
retained rollback copy, and — if a destructive authority reset had already
happened — the validated pre-update database backup is restored in place
of the newly-created target database, so a failed update never leaves old
code paired with an incompatible new schema or a new source marker. Boot
activation is then re-enabled and proven, and the old service is restarted
and its liveness re-verified.

If rollback itself cannot complete, the updater stops hard, preserves every
rollback/backup artifact for manual recovery, and prints the exact state
left behind rather than claiming a false recovery.

After SIGKILL, host reboot, or another exit that bypassed the shell trap, the
next invocation takes the same VMID lock and checks this journal before it
starts a new ownership/planning pass. It re-verifies that VMID still carries
the same bootstrap ownership chain. If rollback was not armed, it removes
only that run's staged artifacts and proves the existing service enabled,
active, and healthy. If rollback was armed, it loads the prior run-id and
markers, restores the run-owned authority helper, and calls the same rollback
implementation described below. Once the
old service is positively restored, enabled, active, and healthy, recovery
marks the journal
recovered, performs final run-owned cleanup, removes the journal, and exits
successfully with a message requiring the update to be rerun. The requested
new target is deliberately not planned or activated in that recovery
invocation.

The run-owned authority helper lives at
`/tmp/hubinet-ops-authority-tool-<run-id>.py` inside the container, and a real
PVE/CT restart legitimately clears that volatile `/tmp`. Every remaining
recovery operation runs through that helper: the three-valued path-state
probes and, after a destructive authority reset, the fail-closed database
removal and validated-backup restore. So before any rollback, path-state, or
authority-database operation, recovery re-pushes the same bounded
`deploy/lib/hubinet-ops-authority-tool.py` to that same reconstructed
run-owned path and positively proves it usable. This is recovery
infrastructure only: it never starts Phase U2, never stages or activates
target application, configuration, or identity content, never pushes the
pre-update HTTP probe, and stays bounded to the loaded run ID. If the helper
cannot be restored or proven usable, recovery hard stops with the journal,
rollback artifacts, and authority backup preserved, and starts no new plan.

Rollback is also replayable. A first rollback can restore artifacts and then
hard stop at a later terminal step (re-enable, start, or the health proof),
which deliberately retains the active journal, so a later invocation
re-enters the same rollback for the same run ID. Every rollback helper
therefore tolerates already-restored state — inspecting the bounded set of
paths its own artifact owns — while still failing closed on an unknown path
state. The PVE host helper additionally keeps its canonical
`.rollback-<run-id>` copy unconsumed (it is copied to a run-owned restore
temporary that is atomically renamed onto the live path), so a retry retains
the original recovery material until terminal recovery cleans it up.

If recovery cannot re-prove ownership, restore and prove the run-owned
authority helper, prove non-running service state before a
rollback mutation, a required rollback path/postcondition, restored boot
activation, restored startup, or health, it exits non-zero and retains the
active journal plus rollback and
authority-backup artifacts. The diagnostic prints the VMID, interrupted run
ID, journal path, and authority backup path when applicable. Every later
invocation encounters the same recovery gate; no fresh plan can begin until
recovery succeeds or the operator manually restores a coherent installation
and resolves the recorded state.

Rollback first issues a stop request and positively proves through systemd
that the service is non-running before touching any managed file. Service
state, unit-file enablement, and rollback-path existence are three-valued: a
failed transport, failed probe, or malformed answer is unknown, never
"stopped", "disabled", "enabled", or "absent." A `disable`/`enable` request
that mutates state and still returns failure, or that reports success
without changing anything, is caught by that separate proof rather than
believed.
Every load-bearing live-path removal is independently proved absent before a
rollback rename. A restored unit must be successfully reloaded into systemd,
and a restored authority database must regain `hubinetops:hubinetops` ownership
and mode `0640`, before the old service is restarted. Failure of any of these
proofs stops rollback hard and preserves the remaining artifacts for manual
recovery.

This is ordinary operational recovery for legitimate updater races,
untrappable process death, and host restart under normal atomic-filesystem
assumptions. It does not claim resistance to a malicious administrator/root,
deliberate journal or lock modification, or hostile kernel/storage behavior.

## Acceptance

After activation and service start, the updater proves:

- for a schema-preserving update: a genuine discovery cycle completed
  *after* the restart (the committed run sequence must exceed the
  pre-update sequence), and `backend_instance_id` is unchanged;
- for a destructive reset: a genuine fresh discovery cycle completed, and
  `backend_instance_id` differs from the pre-update value;
- the PVE host-control forced-command boundary still rejects an unknown
  typed operation exactly as expected;
- `/etc/nftables.conf` is still byte-identical to its pre-update content,
  and `nftables` is still active.

## First Human0 caveat

The updater's generic (non-workload) in-place behaviour completed its first
real operator Human0 validation against CT110 — see `STATUS.md`, "In-place
product update lifecycle".

Its **activation** behaviour has not. Provisioning the five package-update
boundaries, the active-job fence, and the rollback that removes newly created
privileged access paths all have complete automated (hermetic, sandboxed)
validation and no real-host validation. Read this document and the plan output
carefully the first time you run it against a production installation, and see
`README.md`'s "Running your first real update" for the operator procedure that
validates the lifecycle itself.
