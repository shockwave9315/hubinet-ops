# NON-NORMATIVE RESEARCH / EVIDENCE

# Family B Research #2C — experiment #13 pre-execution package

## 1. Authority, result, and execution boundary

This package specifies an offline evidence format, a fail-closed analyzer, and
an operator runbook for a possible later **Family B experiment #13:
pagination / archive rotation / watch-scan omission**. It does not execute or
authorize that experiment. `B-S1`, the experiment states, and the analyzer
outcomes are research-local terminology, not architecture, API, persistence,
or runtime contracts.

Authority remains:

```text
explicit operator decisions
> ACCEPTED ADRs / accepted architecture
> AGENTS.md
> implementation status
> code/tests
> non-normative research/evidence
```

The fixed result is unchanged:

```text
B-S1:       PLAUSIBLE CANDIDATE / NOT PROVEN
Phase S:    DESIGN ONLY / REQUIRES CONTROLLED FALSIFICATION
Phase M:    NOT DESIGNED / NOT SOLVED
Family B:   UNRESOLVED / NOT FULLY AUDITED
Blocker B:  OPEN
WAVE B1:    DEFERRED / NOT AUTHORIZED
Phase 1C:   BLOCKED
R0:         GO / STRICTLY READ-ONLY
trusted:    GRANTED NOWHERE
```

This package creates no backend owner and changes no accepted architecture.
Any future B-S1 owner remains `DEFERRED / DORMANT BACKEND OWNER`; it must not be
wired into R0, application startup, PVE I/O, Home Assistant, or mutation paths.

This pass made zero PVE-node contact, read zero host `/var/log/pve` data, made
zero PVE API calls, ran no `pct`, `qm`, or `pvesh`, restarted no service,
created no guest or storage object, provisioned no fixture, and executed no
experiment. CT112 is a development environment, not a Family-B fixture.

### 1.1 Evidence labels

| Label | Meaning |
| --- | --- |
| `FACT-SOURCE` | Established in an identified immutable upstream source revision. |
| `FACT-DOC` | Established in an ACCEPTED architecture source or official version-relevant documentation. |
| `INFERENCE` | A bounded conclusion from cited facts, not an upstream or architecture contract. |
| `UNKNOWN` | Not established at ADR0005/ADR0006 security strength. |

Operator observations are provenance metadata only. They do not become a
separate authority class and cannot turn an inference into a source contract.

## 2. Exact applicability and source ledger

The repository base for this pass is merge commit
`cdbe8e07d4b5d62b7877aedda8e4e220b2c5a743` (merged PR #51). Current `main`
had not moved beyond that commit when this work began.

The future fixture must match the Research #2A.1 target ledger and prove the
loaded-code context before a capture is eligible:

| Component | Version | Immutable source revision |
| --- | --- | --- |
| `pve-manager` | 9.2.11 | [`f6997e698c7933ea8e62319e2bf1bf7262daa56a`](https://github.com/proxmox/pve-manager/tree/f6997e698c7933ea8e62319e2bf1bf7262daa56a) |
| `pve-cluster` | 9.1.6 | [`7091d92e594952dba65c1e57568b3d7cc244e960`](https://github.com/proxmox/pve-cluster/tree/7091d92e594952dba65c1e57568b3d7cc244e960) |
| `pve-common` | 9.2.1 | [`f665029eac78022e81810ab2e44eace57ade13fb`](https://github.com/proxmox/pve-common/tree/f665029eac78022e81810ab2e44eace57ade13fb) |
| `pve-access-control` | 9.1.1 | [`5ccd07d9302562b73374d331b63d25b04b86766c`](https://github.com/proxmox/pve-access-control/tree/5ccd07d9302562b73374d331b63d25b04b86766c) |
| `pve-ha-manager` | 5.2.5 | [`c73364c19d5317e6df5bb1c1b727d080a5e897ef`](https://github.com/proxmox/pve-ha-manager/tree/c73364c19d5317e6df5bb1c1b727d080a5e897ef) |
| `pve-storage` | 9.1.8 | [`cd5c90ccd9ffd14a9578f58bbf528e78120f8bf2`](https://github.com/proxmox/pve-storage/tree/cd5c90ccd9ffd14a9578f58bbf528e78120f8bf2) |
| `qemu-server` | 9.2.6 | [`e6352be67f70042a7433a3a3c712b36d02f9f7cb`](https://github.com/proxmox/qemu-server/tree/e6352be67f70042a7433a3a3c712b36d02f9f7cb) |
| `pve-container` | 6.1.13 | [`c8132559faedb76a56498d411bf3e024c1ff07e7`](https://github.com/proxmox/pve-container/tree/c8132559faedb76a56498d411bf3e024c1ff07e7) |
| `pve-guest-common` | 6.0.5 | [`191c23e385e5dbed1938b2d1d322196831ef9331`](https://github.com/proxmox/pve-guest-common/tree/191c23e385e5dbed1938b2d1d322196831ef9331) |

The load-bearing source paths for this package are:

- **FACT-SOURCE:** [`PVE/RESTEnvironment.pm`](https://github.com/proxmox/pve-common/blob/f665029eac78022e81810ab2e44eace57ade13fb/src/PVE/RESTEnvironment.pm)
  for worker creation, exact-log creation, archive-line construction, and the
  50,000-byte `index` rotation threshold;
- **FACT-SOURCE:** [`PVE/UPID.pm`](https://github.com/proxmox/pve-common/blob/f665029eac78022e81810ab2e44eace57ade13fb/src/PVE/UPID.pm)
  for the encoded UPID fields and exact-log path mapping;
- **FACT-SOURCE:** [`PVE/API2/Tasks.pm`](https://github.com/proxmox/pve-manager/blob/f6997e698c7933ea8e62319e2bf1bf7262daa56a/PVE/API2/Tasks.pm)
  for mutable `start`/`limit` enumeration;
- **FACT-SOURCE:** [`PVE/API2/Nodes.pm`](https://github.com/proxmox/pve-manager/blob/f6997e698c7933ea8e62319e2bf1bf7262daa56a/PVE/API2/Nodes.pm)
  for the `stopall`, `startall`, and `vncshell` candidate routes;
- **FACT-SOURCE:** [`PVE/API2/Qemu.pm`](https://github.com/proxmox/qemu-server/blob/e6352be67f70042a7433a3a3c712b36d02f9f7cb/src/PVE/API2/Qemu.pm)
  for the `qmstart` candidate route; and
- **FACT-DOC:** Linux man-pages 6.18
  [`inotify(7)`](https://git.kernel.org/pub/scm/docs/man-pages/man-pages.git/tree/man7/inotify.7?h=man-pages-6.18),
  dated 2026-02-14, for the userspace inotify interface and documented caveats.

The inotify document is an official interface description, not a PVE
completeness contract. No exact running-kernel source commit is claimed.

## 3. Question and independent evidence planes

The later experiment asks whether B-S1 can reach a false clean result while
deliberately generated tasks overlap mutable API offset pagination, exact-UPID
log creation, recursive scans, watch delivery/draining, active-to-archive
movement, `index` to `index.1` rotation, and cleanup pressure.

Every run has two independent planes:

```text
operation initiator                         proposed B-S1 observer
  | request_start -> durable GT record        | inotify events
  | execute one approved fixture action       | recursive exact-log scans
  | returned UPID -> durable GT record         | active/index/index.1 copies
  | request_end/finalize -> durable GT record  | API pages + exact-UPID reads
  +---------------- offline comparison --------+ candidate close/gap state
```

The **ground-truth plane** is written by the operation initiator from every
request and its returned UPID. It must not enumerate task history, consume the
B-S1 watcher, or infer missing requests from B-S1 evidence. The
**system-under-test plane** contains only what B-S1 could know. Sharing clocks
and run identifiers is allowed; sharing task discovery is not.

An analyzer result distinguishes:

- `ANALYZER_PASS_TESTED_INTERLEAVING`: every generator-window UPID and required
  subrun phenomenon was reconciled for this exact sealed run;
- `B_S1_GAP_DETECTED`: the observer or source surfaces reported a gap;
- `GENERATOR_WINDOW_ENUMERATION_OMISSION_WITNESS`: independent generator-window
  ground truth proves the candidate enumeration omitted a generated task
  without a required gap while candidate close accepted T1, but exact B-S1
  worker-body-start membership remains unknown;
- `HARNESS_INCOMPLETE`: capture, sealing, generator, heartbeat, or parse
  evidence is incomplete; and
- `ENVIRONMENT_INELIGIBLE`: the fixture/source/loaded-code context does not
  match this exact protocol.

None of these means `trusted`, `secure`, or “Family B solved.”

## 4. Capture format: `family-b-13-capture-v4`

One run is one explicit directory. JSON files are UTF-8; JSONL files contain
one object per line. Every load-bearing monotonic timestamp is nanoseconds from
one explicitly bound Linux `CLOCK_MONOTONIC` domain. UTC wall-clock strings are
provenance only and are never a precision-ordering substitute. No field is
described as a stock PVE cursor or generation number.

```text
run-<uuid>/
  manifest.json
  ground-truth.jsonl
  pre-t0-establishment.jsonl
  watch-events.jsonl
  scan-rounds.jsonl
  surface-observations.jsonl
  api-pages.jsonl
  exact-upid.jsonl
  harness-events.jsonl
  seal.json
```

The research-only parser is
`scripts/research/blocker_b_family_b_13_analyzer.py`. It accepts only:

```text
python scripts/research/blocker_b_family_b_13_analyzer.py analyze \
  --capture-dir ./fixture-data/run-001
```

It has no collection mode, networking, subprocess, credential, host discovery,
or mutation capability. It lexically rejects the host `/var/log/pve` tree
before stat or read, rejects symlink capture roots/files, and reads only the
explicit directory's fixed file set. It is not imported by production code.

### 4.1 Run manifest

`manifest.json` records:

| Field | Required content |
| --- | --- |
| identity | `schema_revision`, `experiment_id`, `run_uuid`, exact `protocol_revision`, exact `expected_b_s1_revision` |
| fixture | `fixture_kind`, placeholder-or-approved `fixture_id`, `node_identity`, `boot_id`; live analysis rejects a placeholder and CT112 |
| versions | installed/source `version_ledger` and `loaded_code_status` |
| environment | `kernel_context`, `filesystem_context`, start/end timestamps |
| processes | `reader_context`, `generator_context`, process identities and clock descriptions |
| clock | complete `clock_contract`: `CLOCK_MONOTONIC`, one domain id, fixture/node/boot/time-namespace binding, correlation state, and the domain id used by every timestamp-producing plane |
| boundary | quiescent candidate `t0_monotonic_ns`, candidate-close T1, a distinct bounded `experiment_generator_window`, committed `baseline_upids` plus hashed `baseline_observation`, and independently cross-checked `t0_quiescence` evidence |
| generator scope | complete `generator_contract`: contract revision, approval state, fixture/subrun, approved operation, expected task type, exact task-id policy, node, owner/auth identity, maximum count, and maximum duration |
| subrun scope | complete `subrun_contract`: contract revision, subrun id, required phenomena, and unique evidence id for each phenomenon |
| limits | disk/log limit and subrun identifier; generator count/duration limits are also bound by the generator contract |
| completeness | one explicit true/false marker for every JSONL stream and ground-truth finalization |
| candidate close | close state, monotonic time, event id, and exact normalized post-T0 enumerated known-UPID set |
| files | the exact logical-name-to-filename mapping |

`node_identity` is fixture provenance, not workload identity. `boot_id` is
captured as context, not treated as a PVE generation. Synthetic runs use
`fixture_kind=synthetic` and an explicit synthetic generator contract. A later
real run uses `disposable_pve` only after the fixture contract is satisfied. A
missing, incomplete, mismatched, or unapproved generator contract makes a
disposable-PVE capture `ENVIRONMENT_INELIGIBLE`.

`baseline_upids` is the only allowed background category. Its complete
`baseline_observation` carries capture start/end and commit times before T0,
the same normalized set, and JSON raw evidence whose SHA-256 and parsed set are
recomputed. A baseline exact log must still exist at close. Every newly
observed post-T0 UPID must bind to the approved ground-truth generator; there
is no implicit third category for ambient or unexplained work in this initial
dedicated-fixture protocol.

Baseline membership does not imply quiescence. The `t0_quiescence` commit is
the logical T0: `committed_at_monotonic_ns` must equal `t0_monotonic_ns`, with
no grace interval between them. It must reference complete local `active`,
`index`, and `index.1` observations captured after the baseline scan. The
referenced active set must be empty. Every baseline UPID must have exactly one
finalized supported/in-scope or classified-out-of-scope record referencing
final, readable exact evidence captured no later than the quiescence
commit/T0; the analyzer derives which of the two classifications is valid by
decoding node/type/id/owner against the generator scope. `pending_upids` must
be empty.
Missing, active, pending, non-final, unclassified, late, or mismatched evidence
prevents positive close. Historical finalized exact/archive records may remain;
retention is not worker liveness.

### 4.2 Shared monotonic clock contract

`clock_contract` uses revision `family-b-13-clock-contract-v1`. It binds
`clock_kind=CLOCK_MONOTONIC`, one `clock_domain_id`, the manifest fixture,
node identity and boot ID, a time-namespace identity, and an explicit
correlation state. Its participant map must bind manifest T0/T1 boundaries,
reader, generator, pre-T0 establishment, watch, scan, surface, API, exact, and
harness timestamps to that same domain. Synthetic captures use an explicit
synthetic shared domain. A `disposable_pve` capture requires a verified single
shared domain on the same fixture node, boot, and relevant time namespace.

Missing or mismatched clock evidence is `ENVIRONMENT_INELIGIBLE`. v4 does not
invent offsets between unrelated monotonic clocks and does not use UTC to
repair them. A generator on another host or time namespace is ineligible until
a separately reviewed cross-clock correlation protocol exists. The analyzer
validates clock eligibility before comparing generator windows, API overlap,
rotation timing, or watch/scan/surface order.

For a future `disposable_pve` capture, the separately reviewed
collector/preflight must derive the clock-domain and time-namespace evidence
from the actual fixture environment. Arbitrary operator-entered strings are not
proof of that binding. This package does not implement that collector.

### 4.3 Pre-T0 watch-first establishment

`pre-t0-establishment.jsonl` is a distinct sealed protocol stream. It records a
contiguous establishment sequence; task-root and bucket `watch_installed`
records with watcher sequence and monotonic time; root-watch `bucket_created`
events and masks; immediate `PRE_T0_BUCKET_RESCAN` records; and explicitly
phased `PRE_T0_BASELINE` scan rounds. `t0_quiescence` references the task-root
watch installation, the exact terminal pair of baseline scan sequences, and
the final pre-T0 watch-drain watermark.

Physical JSONL order in this stream is itself sealed evidence. Every declared
ordinal here is chronology-bearing: `establishment_sequence` is order-compared
against the terminal fixed point, and `watcher_sequence` is compared against the
drain watermark. Each must therefore equal its own physical capture position --
`establishment_sequence` over the whole stream, and `watcher_sequence` and
`baseline_scan_sequence` over their own record subsequences. A set-contiguity
check would accept a permutation, letting a self-declared ordinal relabel a
watcher event captured after the terminal fixed-point scan as an earlier one and
hide it beneath the drain watermark. Any permutation is `HARNESS_INCOMPLETE`
before fixed-point reasoning begins. Independently of every ordinal, a watcher
event whose monotonic time falls after the terminal scan end and at or before T0
invalidates the fixed point.

`ground-truth.jsonl` is bound the same way. The generator durably appends
`request_start` before issuing the operation and closes with the finalizer, so a
stream that physically records a `request_end` before its own `request_start`,
or appends after the finalizer, is `HARNESS_INCOMPLETE`. Physical append order
is the evidence that `request_start` is a causal lower bound at all; a declared
`generator_sequence` cannot reorder it.

One generator process appends that stream and CLOCK_MONOTONIC cannot run
backwards within a single participant, so `monotonic_ns` taken in physical order
across `request_start`, `request_end`, and the finalizer must be nondecreasing.
Equal adjacent values are legal at timer resolution; a reversal is
`HARNESS_INCOMPLETE`. Without this a physically late `request_start` could
backdate itself and admit completeness-bearing evidence recorded before the
request was actually initiated. Overlapping in-flight requests remain legal:
only the capture stream, not the operations, is ordered. This orders capture
records only and never identifies worker body start.

Each child-watch installation declares `bucket_origin` as either
`existing_at_root_install` or `root_event`. A `root_event` installation must
reference the exact creation-event watcher sequence; an initially existing
bucket cannot carry such a trigger or be installed after baseline enumeration
begins. These are capture protocol assertions, not stock PVE fields.

Before positive close, the analyzer machine-checks that the task-root watch was
installed before baseline capture; every bucket in the baseline scan has an
installed child watch before the selected fixed-point rounds; every lazy/new
bucket has a root event, child-watch installation and affected-directory
rescan; and the selected terminal baseline rounds are consecutive, complete,
ordered, equal to each other and to the committed baseline set. Their second
watermark must drain every relevant watcher sequence, and both rounds and all
required rescans must finish no later than the quiescence commit/logical T0.
Every pre-T0 watcher record at or before T0 must be covered. An event after the
selected terminal scan invalidates that fixed point and requires another
rescan/fixed-point establishment before T0; it cannot be excluded by an earlier
commit timestamp. Final local
surface, active/pending, classification and exact-status quiescence checks then
remain independently required.

This sealed establishment stream exclusively owns watcher evidence at or
before logical T0, including an event exactly at T0. Such an event is never
duplicated or reused in `watch-events.jsonl` to prove candidate-interval
enumeration. The post-T0 candidate watch stream and this establishment stream
therefore have a strict, no-grace partition.

This evidence proves only that the candidate watch-first protocol steps were
recorded and passed these capture checks. It does not prove inotify, kernel, or
filesystem completeness; those source-completeness semantics remain
`UNKNOWN`.

### 4.4 Ground-truth events

`ground-truth.jsonl` contains paired `request_start` and `request_end` records
for each contiguous, generator-local sequence, followed by exactly one
`generator_finalized` record.

Each request record carries `generator_sequence`, `request_id`, `operation`,
`monotonic_ns`, `wall_timestamp`, and `generator_process_identity`.
`request_end` additionally carries `returned_upid`, `expected_task_type`,
`expected_task_id`, `outcome`, checked `within_generator_window`, and
`generator_window_relation` (`inside_generator_window`,
`after_generator_window`, or
`ambiguous`). The analyzer derives this experiment-generator relation from
request timestamps and the explicit generator-window bounds, which must fit
inside the quiescent-T0/candidate-close envelope. Matching numeric boundaries
does not merge their semantics. `after_generator_window` is outside the
generator window even if the record claims otherwise; `ambiguous` is
outside the positive set and latches a gap. The record separately carries
`b_s1_body_start_membership=unknown` and `body_start_evidence=null`. This v4
protocol rejects a capture that tries to derive worker-body membership from
request timing or self-assert it. The finalizer carries
`last_sequence`, `total_operations`, generator identity, timestamps, and
`durable_flush_complete`.

For the exact UPID returned by an in-window request, `request_start` is also an
independent causal impossibility lower bound: completeness-bearing evidence for
that returned UPID must end strictly after the request was initiated. Equality
between independently recorded monotonic timestamps does not establish
happens-after ordering and fails closed. This does not identify worker body
start, change `b_s1_body_start_membership=unknown`, or support a B-S1 NO-GO
conclusion.

A failed, timed-out, ambiguously answered, unpaired, duplicated, or
non-contiguous request makes the harness incomplete. It is never interpreted
as absence of a task.

### 4.5 Watch events

`watch-events.jsonl` records `watcher_sequence`, `event_type`, kernel mask
names and raw numeric mask, watch descriptor, cookie, watched path, filename,
normalized UPID when parseable, `queue_overflow`, watch add/remove and
invalidation state, monotonic/wall timestamps, and raw read-buffer ordering.
`IN_Q_OVERFLOW`, `IN_IGNORED`, `IN_UNMOUNT`, `IN_DELETE_SELF`, and
`IN_MOVE_SELF` are preserved even without a filename.

Candidate-interval watch semantics use exactly
`T0 < monotonic_ns <= T1`. A `watch-events.jsonl` record at or before T0 is
`HARNESS_INCOMPLETE` because it belongs to the pre-T0 establishment plane. A
record after T1 may remain as structurally validated diagnostic evidence, but
it cannot affect discovery, deletion, observer GAP latching, exact-UPID watch
provenance, 13D/13E/13F evidence, or terminal watch-drain success. There is no
grace interval. A candidate-interval record carrying an in-window generated
UPID is incomplete when its event time is at or before that UPID's independent
`request_start`; temporal eligibility requires a strictly later event.

The Linux UAPI `struct inotify_event.mask` integer is the primitive. The v4
analyzer pins the numeric definitions from Linux
`include/uapi/linux/inotify.h`: `IN_ACCESS=0x00000001`,
`IN_MODIFY=0x00000002`, `IN_ATTRIB=0x00000004`,
`IN_CLOSE_WRITE=0x00000008`, `IN_CLOSE_NOWRITE=0x00000010`,
`IN_OPEN=0x00000020`, `IN_MOVED_FROM=0x00000040`,
`IN_MOVED_TO=0x00000080`, `IN_CREATE=0x00000100`,
`IN_DELETE=0x00000200`, `IN_DELETE_SELF=0x00000400`,
`IN_MOVE_SELF=0x00000800`, `IN_UNMOUNT=0x00002000`,
`IN_Q_OVERFLOW=0x00004000`, `IN_IGNORED=0x00008000`, and
`IN_ISDIR=0x40000000`. These are the complete accepted output bits for this
bounded protocol. Watch-configuration flags and every unknown raw bit are
rejected rather than discarded.

The analyzer decodes that integer itself. The declared textual `mask` must be
a duplicate-free exact set match, and `queue_overflow` must equal whether the
decoded set contains `IN_Q_OVERFLOW`. Discovery, deletion, observer-loss/GAP,
13D rename, 13E, and subrun semantics use only the decoded set. Where a watch
filename is an exact normalized UPID, the analyzer derives that UPID from the
filename and requires the declared `normalized_upid` to match; either-sided
omission or disagreement is incomplete. Pre-T0 lazy-bucket creation records
likewise carry and decode `raw_mask`; their textual create/move plus
`IN_ISDIR` names are cross-checks only.

`raw_order` is a harness-assigned, one-based capture-order sequence. The v4
analyzer requires JSONL physical order, `watcher_sequence`, and `raw_order` to
be the same contiguous `1..N` sequence. This checks that sealed records retain
the observer's raw capture order; it is not a kernel cursor, an inotify
completeness proof, or a theorem about causal ordering inside the kernel.

### 4.6 Scan rounds

`scan-rounds.jsonl` records a contiguous monotonic `scan_sequence`, `round_id`,
start/end times, the sorted exact normalized UPID set, bucket set,
per-directory and per-file stat/inode metadata, unreadable and malformed
entries, `complete`, and `watch_drained_through_sequence`. A capture-supplied
consistency marker is annotation only; the analyzer computes the terminal
fixed point. Positive close requires two consecutive terminal complete scans
with identical normalized sets and no relevant watcher sequence left undrained.
Watcher sequence/time and each scan's drain watermark are cross-checked so a
scan cannot claim an event that occurred after it ended. A disappearing exact
log or unreadable/malformed/inconsistent scan latches a gap.

A scan containing an in-window generated UPID is causally eligible for that
UPID only when `scan_end_monotonic_ns > request_start_monotonic_ns`. Its start
may precede request initiation because a scan may span the creation boundary.
An earlier or equal scan end is `HARNESS_INCOMPLETE` before the UPID enters
enumeration or exact scan provenance.

The JSONL records themselves must physically occur in ascending
`scan_sequence`; out-of-order capture is `HARNESS_INCOMPLETE`. After parsing,
all watermark monotonicity, scan-time ordering, disappearance, adjacency, and
terminal fixed-point semantics are evaluated over sorted `scan_sequence`,
never raw iteration order. A nonzero drain watermark must reference an existing
watcher sequence or the capture is incomplete.

### 4.7 Active and archive observations

`surface-observations.jsonl` records a contiguous `observation_sequence` and
one or more captures for each of `active`, `index`, and `index.1`: capture
start/end, raw evidence, parsed normalized UPID set, read/parse completeness,
size, inode/device/mtime metadata, and SHA-256.
Missing `index.1` is represented explicitly as an absent-but-completely-read
surface before first rotation, not silently omitted. The analyzer recomputes
SHA-256 from the captured UTF-8 `raw_evidence`; hashes support comparison but
do not prove an atomic snapshot.

For v4, `raw_evidence` is exactly the captured UTF-8 file content, with no
JSON wrapper, prefix, annotation, or collector-generated record envelope. The
only accepted serializations are deliberately narrow and source-specific:

```text
active  := *(UPID SP ("0" / "1")
             [SP 8UPPER-HEX [SP NONSPACE-STARTING-STATUS]] LF)
index   := *(UPID SP 8UPPER-HEX SP NONSPACE-STARTING-STATUS LF)
index.1 := *(UPID SP 8UPPER-HEX SP NONSPACE-STARTING-STATUS LF)
```

The `active` shape is the pinned `write_active_workers()` representation; the
archive shape is the pinned `sprintf("%s %08X %s\n", ...)` representation.
An empty string is the only zero-entry encoding and is also used with the
explicit absent-but-completely-read `index.1` metadata. Every nonempty record
must be LF-terminated. Missing final LF, CRLF, malformed fields, invalid UPID,
or a duplicate UPID makes the capture `HARNESS_INCOMPLETE`.

The offline analyzer parses the exact UPID set from these sealed bytes. The
declared `normalized_upids` array is parsed separately, must contain no
duplicates, and must equal the raw-derived set exactly. Only the raw-derived
set enters enumeration, exact-UPID discovery provenance, T0 active quiescence,
handoff, or rotation obligations. This format intentionally replaces the
earlier ambiguous raw-content description; no real v4 capture exists and no
backward compatibility is required.

A surface containing an in-window generated UPID is causally eligible for that
UPID only when `capture_end_monotonic_ns > request_start_monotonic_ns`. Its
capture may span request initiation. An earlier capture end is
`HARNESS_INCOMPLETE`, and equality is equally ambiguous and incomplete, before
the raw-derived UPID enters enumeration, handoff or rotation semantics, or
exact active/archive provenance. This also prevents a T0-quiescence surface
from manufacturing evidence for a request recorded at the same timestamp.

### 4.8 API pages

`api-pages.jsonl` records source profile (`active`, `archive`, or `all`),
`start_offset`, `limit`, normalized returned UPIDs, request identity,
request/response times, completion, and `restart_reason` (or explicit `null`).
For a pagination subrun, records also carry the declared `phenomenon_id` and a
contiguous `page_sequence`; the analyzer requires multiple offsets for one
source. Offsets and page numbers are harness fields only. Duplicate pages are
retained verbatim. Pagination alone never establishes completeness.

### 4.9 Exact-UPID observations

`exact-upid.jsonl` records a contiguous `observation_sequence`, the known UPID,
capture start/end, presence,
readability, `previously_known`, discovery source/reference, raw status result,
raw log result, each result's recomputed SHA-256, and final-status
interpretation. Valid discovery sources are committed baseline, a prior watch
event, a completed scan, or a prior local active/archive observation. Exact-UPID
reads confirm and finalize only an already enumerated UPID; they never add one
to the completeness-bearing enumeration set. Missing discovery provenance is
`HARNESS_INCOMPLETE`.

Watch provenance additionally requires a strict candidate-interval watch.
Scan and surface provenance inherits their respective end-time causal checks.
Thus an exact record cannot rehabilitate temporally impossible discovery
evidence and remains confirmation-only.

A final status counts only when the known exact evidence is present and
readable, both status and log raw results are available, their hashes match,
their parsed fields match the raw evidence, stopped/status content is
structurally consistent with the interpretation, and capture completed no later
than candidate-close T1.
A known exact log becoming absent/unreadable is a gap. A task temporarily
absent from `active`, `index`, and `index.1` is not missing if a legitimate
watch or scan discovery remains and its exact evidence is preserved and
reconciled.

### 4.10 Harness events

`harness-events.jsonl` records a contiguous `harness_sequence`, observer
process identity, process start/stop/crash, sequenced heartbeats, capture
finalization, analyzer version, scheduled interleavings, injected synthetic
overflow, dropped-input simulation, and explicit gap signals. Every event must
bind to `reader_context.process_identity`. The analyzer considers every
heartbeat in the open interval, requires the last pre-close heartbeat to be
healthy and fresh, and enforces start <= T0 < close < finalization <= stop. An
explicit structurally valid unhealthy or stale heartbeat is observer-health
loss and yields `B_S1_GAP_DETECTED`. A missing, malformed, non-contiguous, or
wrong-identity heartbeat stream is instead `HARNESS_INCOMPLETE`, as are
incoherent process boundaries, crash, missing finalizer, or version mismatch.
Neither class can produce a positive result.

## 5. Ground-truth generator contract

A manifest carries a machine-readable `family-b-13-generator-contract-v1`
object. It binds `approval_state`, `fixture_id`, `subrun_id`, one
`approved_operation`, one `expected_task_type`, an exact task-id policy,
`expected_node`, `expected_owner`, `maximum_operation_count`, and
`maximum_duration_seconds`. The analyzer decodes every returned and newly
observed UPID and checks node, task type, task id, and owner/auth identity
against this contract. Ground truth cannot authenticate itself with
`within_generator_window`, task type/id strings, or a returned UPID alone.

The operation initiator independently proves that it issued a request and
received a UPID during the experiment-generator window. It does not observe
the worker operation body's first instruction. Request start/end, UPID
starttime, exact-log creation, and fork time are therefore not relabeled as
B-S1 body start. No safe body-start proof is implemented or accepted by this
v4 analyzer.

A future generator must satisfy all of these conditions:

1. It is separately approved for one disposable fixture and one subrun.
2. It has immutable maximum operation-count and duration bounds and exits on
   either bound.
3. It operates only on explicitly reserved disposable objects. No production
   identifier is embedded in this package.
4. It assigns a monotonically increasing generator-local sequence and durable
   request id before every attempt.
5. It durably appends and flushes `request_start` **before** the operation.
6. It records and durably flushes a returned UPID immediately, before any next
   operation; the record is sourced from the initiator response, not PVE task
   enumeration or B-S1.
7. It pairs the request end, outcome, expected task type/id, and timestamps,
   and closes with an independently durable contiguous-sequence finalizer.
8. A failure, timeout, disconnect, unknown response, flush failure, sequence
   gap, or lost finalizer stops generation and yields `HARNESS_INCOMPLETE`.
9. It never substitutes a guessed UPID and never uses an observer-detected
   UPID to repair ground truth.

The synthetic suite uses the explicit `synthetic` approval state. A future
`disposable_pve` run requires a separately approved complete contract for that
exact fixture/subrun. This schema does not approve `stopall` or any other live
operation; the candidate selection below remains conditional.

Durable here means flushed to the approved fixture evidence volume and copied
into the sealed capture; it is not a claim of crash-proof distributed commit.

## 6. Cheapest-safe task-generator research

No generator command is authorized here. No real VMID or storage was selected.

| Candidate | Real UPID / RESTEnvironment worker | Guest or storage mutation | Expected duration | Repeatability / cleanup | Hundreds or thousands on disposable node | Scope risk |
| --- | --- | --- | --- | --- | --- | --- |
| node `stopall` with one explicitly reserved, verified-absent VMID and `force-stop=false` | Yes, `stopall` | None **only while the reserved slot remains absent** | Short worker | High; no object cleanup when filter is empty | Plausible under explicit load/disk bounds | Race or configuration error could target a newly present guest; strict absence guard is load-bearing |
| node `startall` with an empty/filtered set | Yes, `startall` | Unsafe for this purpose: source removes stale backup locks before the filter | Short if empty | Cleanup ambiguous | No | Broadens into guest-lock lifecycle mutation |
| `qmstart` against an already-running disposable QEMU guest | Yes, `qmstart` | Normally fails in worker without starting it, but a race could start a stopped guest | Short failure | Requires a running guest and power authority | Possible but noisy | Broadens into guest power lifecycle and race control |
| node `vncshell` | Yes, `vncshell` | No guest/storage mutation | Proxy/session bounded; not minimal | Repeated proxy/ticket/port cleanup | Operationally possible, not preferred | Adds console, port, ticket, authentication, and timeout behavior unrelated to #13 |
| `aptupdate` | Yes | Mutates package caches and may use network | Network-dependent | Cleanup and determinism poor | No | Broadens into network/package lifecycle |
| create/destroy, snapshot, backup, restore, clone | Yes | Yes | Variable | High cleanup/storage burden | Not safely assumed | Becomes a guest/storage lifecycle experiment |

**FACT-SOURCE:** the pinned `stopall` route filters its guest list to an
explicit VMID before entering the genuine `fork_worker('stopall', ...)`; with
an absent ID the worker loop is empty. **INFERENCE:** this is the cheapest safe
stock candidate identified for task-record pressure because it reaches the
same RESTEnvironment worker/log/archive machinery without intended guest or
storage mutation.

Selection is therefore **CONDITIONAL CANDIDATE / NOT YET AUTHORIZED**:
`stopall` with a fixture-reserved absent slot, `force-stop=false`, and
independent pre-request and post-request proof that the slot remained absent.
Any observed guest, unexpected task type, or inability to make that absence
check independent and race-safe stops the run. Fixture review may reject the
candidate and leave generator selection open; no fallback generator is
preapproved.

## 7. Index-rotation volume estimate

**FACT-SOURCE:** `log_task_result()` appends
`sprintf("%s %08X %s\n", $upid, $endtime, $status)` to `index`, closes the
append, then rotates by renaming `index` to `index.1` when observed size is
strictly greater than `50,000` bytes. The source comment says “about 1000
entries”; it is not an exact count.

For one entry:

```text
archive_line_bytes = len(UPID) + 1 + 8 + 1 + len(status) + 1
                   = len(UPID) + 11 + len(status)

len(UPID) = 28 + len(node) + len(pstart_hex) + len(type)
               + len(id) + len(user)
```

`pstart_hex` is eight or nine characters under the pinned decoder, and node,
type, id, user, and status lengths vary. For the conditional `stopall`
candidate (`type` length 7, empty id, short `OK` status), the source-format
floor with one-byte node/user values and an eight-byte `pstart` is 58 bytes per
line. A more realistic planning envelope with 8–20 byte node names and 8–32
byte users is about **72–109 bytes per line**. From an empty index, those sizes
imply roughly **459–695 entries** for the planning envelope and **863 entries**
at the source-format floor before the first append that crosses 50,000 bytes.
These are estimates, not source guarantees.

The future preflight must measure the actual starting size and observed line
sizes. Reserving a separately approved ceiling of **1,000 successful short
entries** covers the source-format floor from an empty index, but the actual
target must be recomputed as:

```text
ceil((50,001 - observed_start_size) / minimum_observed_line_size)
```

and bounded by the approved operation and disk limits. Rotation is accepted
only when actual `index`/`index.1` stat, inode, hash, content, and watch evidence
shows it. Count never substitutes for observation because starting size,
variable line/status length, concurrent tasks, reaper timing, and rotation
races all affect the crossing point.

## 8. Linux watch boundary

The intended future primitive is direct Linux inotify:
`inotify_init1(IN_NONBLOCK | IN_CLOEXEC)` followed by explicit
`inotify_add_watch()` calls. A convenience recursive watcher is insufficient
unless it exposes the same raw masks and watch lifecycle.

Likely directory mask:

```text
IN_CREATE | IN_MOVED_TO | IN_CLOSE_WRITE | IN_ATTRIB |
IN_DELETE | IN_MOVED_FROM | IN_DELETE_SELF | IN_MOVE_SELF | IN_UNMOUNT
```

`IN_ONLYDIR` should protect directory watch installation. `IN_MASK_CREATE` may
be used when supported to avoid clobbering an existing mask. The reader must
also preserve returned `IN_IGNORED`, `IN_Q_OVERFLOW`, and `IN_ISDIR` bits.

| Claim | Classification | Consequence |
| --- | --- | --- |
| inotify directory monitoring is not recursive | `FACT-DOC` | Every existing and newly created task bucket needs its own explicit watch. |
| New files/subdirectories may appear between child-directory creation and watch attachment | `FACT-DOC` | Attach the child watch, then scan it immediately; the interval remains an adversarial race for #13F. |
| Queue overflow emits `IN_Q_OVERFLOW` with watch descriptor `-1`; excess events are dropped | `FACT-DOC` | Latch `GAP`; intentional overflow ends that bounded subrun after evidence capture. |
| Watch removal/unmount yields `IN_IGNORED`; unmount also yields `IN_UNMOUNT` | `FACT-DOC` | Latch `GAP`, record watch lifecycle, and stop. |
| `IN_DELETE_SELF`/`IN_MOVE_SELF` indicates loss or movement of a watched object | `FACT-DOC` | Treat as invalidation until a full gap-aware rebuild; never silently reattach and close clean. |
| Event names may be stale by consumption time; move pairs are not atomically queued | `FACT-DOC` | Preserve raw cookie/order and reconcile by scans/exact logs, not event counting. |
| Identical unread events can coalesce | `FACT-DOC` | Inotify cannot be ground truth or a reliable task counter. |
| Mounting over a watched directory may emit no event and hide descendants | `FACT-DOC` | Fixture mount topology is frozen/recorded; a change or ambiguity makes the run ineligible/gapped. |
| Scan-after-watch closes every creation race | `UNKNOWN` | It is a mitigation to falsify, not a proof. |
| A passing #13 run proves universal watcher completeness | `UNKNOWN` and explicitly not claimed | PASS applies only to enumerated interleavings on the exact kernel/filesystem/context. |

## 9. Analyzer decision contract

The analyzer validates environment eligibility, fixed filenames, JSON/JSONL
shape, source ledger, independent ground truth, contiguous request pairs,
generator scope, heartbeat/process ordering, candidate close state, required
surface profiles, a computed scan/watch fixed point, exact-UPID confirmation,
subrun obligations, and the capture seal.

Before any positive interval result, the analyzer also cross-checks a quiescent
T0: task-root and existing-bucket watches precede the explicitly phased
baseline enumeration; lazy buckets require a root event, child watch, and
affected-directory rescan; the referenced consecutive complete baseline scans
have equal normalized sets and drain all relevant pre-T0 watch events; and all
of this finishes by the quiescence commit, which exactly defines logical T0.
Any watcher record through T0 that follows the selected terminal scan
invalidates that fixed point. The referenced local surface captures and every
required baseline exact finalization also finish no later than T0; the active
and pending sets are empty; and every retained baseline UPID
is finalized and classified. A retained historical log or archive line is not
treated as a running worker. These checks prove protocol execution, not
universal kernel/filesystem completeness.

The completeness-bearing `enumerated_known` set is the union of task-log watch
discovery, recursive exact-log scans, and local parsed active/`index`/`index.1`
observations. `exact_confirmed` is separate: exact-UPID records can confirm only
an already enumerated or committed-baseline UPID. API results remain
corroborative and cannot become completeness authority. The analyzer compares
all post-T0 enumerated UPIDs with ground truth in the experiment-generator
window; an extra UPID, unknown task type/id/node/owner, or a candidate-close set
that omits an observed UPID latches a gap. This comparison does not establish
B-S1 worker-body-start membership.

The union is built only after structural/raw-projection validation and temporal
eligibility. Watch discovery uses the strict candidate interval; watch, scan,
and surface evidence for each in-window generated UPID must meet that UPID's
request-start causal lower bound. Request timing is not added to the
enumeration set and remains no evidence of worker body start.

Each `family-b-13-subrun-contract-v1` declares the phenomenon the subrun must
exercise and an evidence id. The analyzer then cross-checks that id against raw
capture structure:

| Subrun | Machine obligation |
| --- | --- |
| 13A | nonempty in-window generated UPID set plus legitimate enumeration/reconciliation |
| 13B | sequenced pages for one source with multiple actual offsets and at least one strict half-open interval overlap (`page_start < request_end` and `page_end > request_start`) with an in-window generator request schedule; equality-only boundary contact is insufficient |
| 13C | an in-window generated target present in ordered active then archive surface observations |
| 13D | old `index` hash and device/inode reappear as `index.1`, new `index` differs, matching rename watch evidence exists, captured rotation content includes an in-window generated UPID, and the marker binds every in-window generator sequence while rotation occurs inside the generated run |
| 13E | a matching raw overflow or invalidation/loss signal strictly after T0 and at or before candidate close; the exact qualifying watch record must also cause the observer GAP, and the successful expected classification is `B_S1_GAP_DETECTED` |
| 13F | one in-window generated target/watch event inside the referenced scan interval and present in that scan |
| 13G | at least two phenomena explicitly selected by this run contract, each independently satisfying its corresponding generated-run/raw check |

A subrun label or task count is never phenomenon evidence. In particular, 13D
cannot pass without observed rotation, and 13E cannot emit a plain analyzer
PASS after its required in-interval signal. A matching 13E signal only after
candidate close does not exercise 13E for that run and cannot satisfy its
obligation; no claim is made about which dropped events a post-close overflow
represents. Every positive subrun except 13E requires at
least one in-window generated operation; baseline history alone cannot satisfy
13A–13D, 13F, or 13G. 13E may use no unrelated generated task because its
intended successful result is the approved injected gap signal.
For 13G, PASS covers only the exact selected `required_phenomena` in that run's
contract; it does not imply that every phenomenon in the narrative combined
pressure scenario occurred.

Decision precedence is fail-closed:

1. context mismatch -> `ENVIRONMENT_INELIGIBLE`;
2. missing/corrupt/unsealed/ambiguous harness or ground truth ->
   `HARNESS_INCOMPLETE`;
3. watcher, scan, source, T1-ordering, cleanup, or explicit gap ->
   `B_S1_GAP_DETECTED`;
4. complete generator-window ground truth omitted by the allowed enumeration
   surfaces, with no gap and an otherwise accepted close, but without
   independent body-start membership ->
   `GENERATOR_WINDOW_ENUMERATION_OMISSION_WITNESS`;
5. only complete reconciliation for the exact captured interleaving ->
   `ANALYZER_PASS_TESTED_INTERLEAVING`.

API UPIDs are deliberately corroborative. Duplicate pages are tolerated only
when watch/scan/local-surface enumeration plus exact confirmation independently
reconciles every ground-truth UPID. Repeating mutable pagination cannot heal
its own omission.

## 10. Synthetic adversarial suite

The tests use temporary synthetic directories only. They neither import nor
exercise a collector.

| # | Synthetic capture | Required analyzer result |
| --- | --- | --- |
| 1 | Ground truth equals reconciled B-S1 known set | `ANALYZER_PASS_TESTED_INTERLEAVING`; architecture effect remains `NONE` |
| 2 | Generator-window UPID absent from every B-S1 observation, body-start membership unknown | `GENERATOR_WINDOW_ENUMERATION_OMISSION_WITNESS` |
| 3 | Watch queue overflow | `B_S1_GAP_DETECTED` |
| 4 | Watch invalidation/loss | `B_S1_GAP_DETECTED` |
| 5 | Duplicate API pages, independently reconciled | PASS for that interleaving |
| 6 | Offset omission repeated without independent evidence | generator-window enumeration witness, not pagination healing |
| 7 | Task absent from temporary active/archive surfaces but legitimately scan/watch-discovered and retained in exact evidence | Not missing merely due to surface handoff |
| 8 | Known exact log deleted by cleanup | `B_S1_GAP_DETECTED` |
| 9 | Unknown pre-enumeration log deleted without watch signal | generator-window enumeration witness |
| 10 | Surviving overlap anchors around a removed unknown intermediate log | generator-window enumeration witness; anchors do not clean the run |
| 11 | Valid heartbeat explicitly unhealthy/stale before close | `B_S1_GAP_DETECTED` |
| 11a | Missing/malformed/wrong-identity heartbeat evidence or incoherent process lifecycle | `HARNESS_INCOMPLETE` |
| 12 | Incomplete/corrupt capture file | `HARNESS_INCOMPLETE` |
| 13 | Generator sequence gap or missing finalizer | `HARNESS_INCOMPLETE` |
| 14 | Source/version mismatch | `ENVIRONMENT_INELIGIBLE` |
| 15 | Late task ambiguous around synthetic T1 | `B_S1_GAP_DETECTED`; no universal ordering claim |

Additional guards prove the analyzer rejects `/var/log/pve` before file open,
completes with network and subprocess entry points patched to fail, and
contains no network, subprocess, or PVE-command collection path.

Targeted review regressions additionally cover exact-only records without
discovery provenance; lying fixed-point annotations; an undrained watch event;
an unexpected valid UPID; each 13A–13G label without its required phenomenon;
unhealthy or wrong-process heartbeats and early process stop; absent/unreadable
exact evidence with a claimed final status; analyzer-source and surface-hash
mismatches; false generator-window membership after T1; missing
disposable-fixture generator scope; and a generated UPID with the wrong owner.
The suite also rejects a live/pending baseline at T0, request-timing claims of
body-start membership, baseline-only positive subruns, and baseline-only
handoff/race targets. Positive controls exercise historical finalized baseline
quiescence, computed drained equal scans, and raw-evidence-backed
13B/13C/13D/13F/13G obligations.
v4 regressions additionally reject post-T0 or missing root-watch establishment,
different pre-T0 baseline scan sets, an undrained pre-T0 event, and a lazy
bucket without its child watch/rescan. A positive lazy-bucket control reaches a
drained watch-first pre-T0 fixed point. Clock-contract regressions reject a
missing disposable-fixture contract, mismatched generator/reader domains, and
a boot-ID mismatch; one explicit shared synthetic clock domain may continue.
The baseline-classification regression also locks owner/auth identity into the
existing node/type/id/owner comparison.
The final boundary regressions reject a quiescence commit earlier than T0 and a
watch event after the selected terminal fixed point but at or before T0. The
positive control commits quiescence exactly at T0 after a fully drained fixed
point, with normal generated work beginning afterward.
Final independent-review regressions reject matching 13E signals that occur
only after candidate close, with zero or normal generated work and with an
unrelated benign in-window watch; an in-window matching signal remains a GAP.
The P1 corrective matrix additionally reproduces active, `index`, and
`index.1` self-asserted normalized-UPID discovery; raw nonempty/declared empty
and raw A/declared B disagreement; duplicate and malformed raw/declared surface
forms; exact raw/declared positive controls; unchanged raw bytes with a changed
projection; and 13C/13D referenced-surface disagreement. Its inotify matrix
crosses raw/text discovery, deletion, overflow, every accepted observer-loss
bit, `IN_ISDIR` lazy-directory creation/move, queue-overflow boolean mismatch,
unknown raw bits, and watch filename/normalized-UPID disagreement. Correct raw
agreement preserves discovery, deletion, GAP, handoff, rotation, and lazy
bucket behavior; every disagreement is incomplete before semantic use.
The temporal P1 matrix reproduces the pre-T0 sole-watch false PASS, rejects an
event exactly at T0 and a post-T0 event before the returned UPID's request
start, rejects equality at request start on every completeness-bearing plane,
and admits watch evidence strictly after request start through T1. It also
rejects post-close watch discovery, scans ending at or before request start,
and active/`index`/`index.1` captures ending at or before request start, while
admitting scan and surface captures that strictly span request initiation.
Every negative case keeps exact provenance from rehabilitating the impossible
evidence.
They also prove ordered disappearance detection, reject shuffled scan JSONL
for both set and watermark histories, retain an ordered-scan positive control,
fail closed on a missing watcher referenced by a nonzero watermark, enforce
raw watch capture order, and distinguish valid stale-heartbeat GAP from missing
heartbeat-stream incompleteness.

## 11. Exact false-clean witness boundary

A B-S1-killing witness must preserve all of the following in the sealed run:

1. one independent ground-truth `request_start`/`request_end` pair and returned
   normalized UPID from the generator;
2. proof the operation and UPID are within the declared subrun's experiment
   generator window;
3. the full B-S1 watch, scan, active/archive, API, and exact-UPID streams showing
   that UPID was omitted from the allowed enumeration set or was once known and
   then lost;
4. absence of every required gap signal in the complete watch/harness/source
   record;
5. the candidate close record proving the same logic would otherwise accept
   `T1` as `CLOSED_COMPLETE`; and
6. independent evidence that the worker operation body—not merely its request,
   UPID starttime, exact-log creation, or fork—began inside the committed B-S1
   `[T0,T1]` body-start interval; and
7. manifest, source/loaded-code ledger, file hashes, analyzer revision/source
   hash, analyzer commit metadata, and overall seal hash preserving the
   evidence used for those facts.

Requirement 6 is not available from the #13 operation initiator or current
source evidence. This v4 analyzer therefore cannot emit a precise B-S1-killing
witness or exact-scope B-S1 rejection from request timing alone.

Instead it preserves the UPID, generator sequence, derived generator-window
membership, omission, close state, and absence of a recorded gap as
`GENERATOR_WINDOW_ENUMERATION_OMISSION_WITNESS`, explicitly recording
`b_s1_body_start_membership=UNKNOWN`. That is a serious research-local
enumeration falsification result and must stop later subruns, but it is not the
precise body-start-scoped B-S1 consequence. A future protocol may add an
independently reviewed body-start proof; it must not synthesize one from
adjacent timestamps.

## 12. Run sealing and integrity boundary

Sealed raw evidence is the primitive evidence. Every normalized field that can
affect completeness is only a deterministic parsed projection; it never
authenticates itself. The offline analyzer recomputes such projections from the
sealed primitive whenever this harness defines a raw representation and
requires exact raw/normalized agreement. A disagreement is
`HARNESS_INCOMPLETE`, before the record can enter a PASS-bearing set or GAP
obligation. In v4 this rule applies directly to local surface raw bytes versus
`normalized_upids`, watch `raw_mask` versus textual `mask` and
`queue_overflow`, watch filename versus `normalized_upid`, and exact status/log
raw text versus parsed terminal fields.

After capture close and before analysis, the later sealer writes `seal.json`
with the schema/run UUID, analyzer revision, SHA-256 of the exact analyzer
source-file bytes, repository commit, and the filename, byte size, and SHA-256
of `manifest.json` and every JSONL evidence file. It then hashes the canonical
sorted seal payload as `overall_manifest_hash`. The analyzer recomputes every
file value and its own source hash before parsing.

`analyzer_commit` remains provenance metadata only: this offline analyzer does
not independently prove that the named repository commit contains the source
bytes. The checked source SHA-256 binds analysis eligibility to the concrete
analyzer file used. Hashing raw bytes proves only post-capture byte integrity;
it is not proof that the capture-side parser or any declared normalized field
was correct. These hashes make only the basic post-capture integrity claim
below.

Completed captures become read-only in the operator evidence store; any later
annotation is a separately hashed file outside the sealed directory. SHA-256
here detects post-capture byte changes under ordinary evidence handling. It
does not establish author identity, trusted time, non-repudiation, or resistance
to an actor able to replace both capture and seal.

## 13. Later disposable-fixture contract

No fixture is instantiated or selected by this package. A later approval must
name a fixture satisfying all of these requirements:

- a non-production PVE node with no production dependencies, HA production
  integration, private workload, or real workload data;
- exact target package/source and loaded-code baseline from section 2;
- known and recorded kernel, boot ID, mount topology, filesystem, storage
  backend, clock, and retention configuration;
- console/recovery access independent of the experiment;
- explicit CPU, RAM, evidence-disk, log-space, task-count, task-rate, and total
  duration limits;
- only explicitly reserved disposable guest IDs if a separately approved
  generator requires guests; no production host IDs are chosen here;
- an identified cleanup owner, evidence owner, experiment start/stop window,
  and post-run health acceptance criteria; and
- reboot permission only for a separately approved subrun that requires it.

CT112 explicitly does not satisfy this contract and must not be used.

## 14. Immediate stop conditions

The later operator stops generation and prevents positive close if:

- any production dependency, real workload, or unapproved identifier appears;
- ground-truth durability, pairing, sequencing, or completeness is lost;
- inotify reports overflow/invalidation, except that an intentional bounded
  13E injection captures the event and then stops that subrun;
- disk/log space crosses the preapproved safety threshold;
- task count, rate, duration, or archive volume crosses its approved bound;
- any unexpected task type or target appears;
- service/node health degrades outside the approved fixture bounds;
- collector, protocol, analyzer, source, installed package, or loaded-code
  context mismatches;
- mount/watch topology changes unexpectedly; or
- cleanup or evidence ownership becomes ambiguous.

## 15. Later operator runbook skeleton

Every PVE-mutating or task-generating action remains **NOT AUTHORIZED / TEMPLATE
ONLY** until the exact subrun has separate operator approval. This document
intentionally provides no executable generator or destructive cleanup command.

0. Obtain separate written approval for one fixture, generator, bounds, and
   subrun; record the approver and window.
1. Verify fixture identity, isolation, absence of production dependencies, and
   that it is not CT112.
2. Record installed packages, immutable source mapping, boot ID, and loaded-code
   preflight. Record and verify the single-node/single-boot/single-time-namespace
   `CLOCK_MONOTONIC` contract for every timestamp-producing participant before
   making any cross-plane ordering comparison; mismatch makes the run
   ineligible. The later reviewed collector/preflight must derive this evidence
   from the fixture environment, never accept operator-entered identifiers as
   proof.
3. Record filesystem/mount context and disk/log free-space; set numeric stop
   thresholds before starting any process.
4. Start the independent ground-truth writer; verify durable test write,
   monotonic sequence initialization, generator-contract binding, and
   operation/duration caps.
5. Start the B-S1 observer; install the task-root and all existing-bucket
   watches before baseline enumeration. For every lazy/new bucket, preserve the
   root event, attach its child watch, and immediately rescan it. Keep heartbeat
   and gap latches active.
6. Record two consecutive complete `PRE_T0_BASELINE` scans with equal normalized
   sets and a second watermark draining every relevant pre-T0 watch event.
   Reference those exact rounds from `t0_quiescence`; then capture
   active/index/index.1, obtain pre-T0 final exact/classification evidence for
   every baseline UPID, prove active/pending empty, and atomically commit
   quiescence as logical T0 with identical monotonic values and no intervening
   grace interval.
   Also record API profiles and the explicit subrun contract/evidence ids.
7. Execute exactly one approved bounded subrun. Any generator action at this
   stage is **NOT AUTHORIZED / TEMPLATE ONLY** until that separate approval.
8. Stop generation, drain watch events, complete consecutive final scans and
   provenance-bound exact-UPID reads, capture all surfaces, record candidate T1
   close/gap, and finalize streams after close but before observer shutdown.
9. Copy and seal evidence; verify file list, sizes, SHA-256 values, analyzer
   revision/source hash, commit metadata, and overall manifest hash. Preserve
   the immutable original.
10. Move a copy to an offline workstation and invoke only the explicit
    `analyze --capture-dir` command from section 4.
11. Record exactly one research-local classification and its evidence. A v4
    generator-window enumeration witness stops the sequence but does not claim
    an exact body-start-scoped B-S1 consequence; PASS only enumerates that
    interleaving.
12. The identified cleanup owner performs the separately approved fixture
    cleanup. No cleanup command or real identifier is supplied here.
13. Verify fixture service, storage, disk/log space, console access, and absence
    of residual experiment objects; preserve health evidence separately.

## 16. Independently approvable subruns

Recommended early-kill order is **13A -> 13F -> 13B -> 13C -> 13D -> 13E ->
13G**. Any generator-window enumeration omission witness stops and skips all
later runs. A harness failure must be corrected and rerun under a new UUID; it
does not justify moving on.

### 13A — low-volume watch/log ordering sanity

- **Question:** Does the observer preserve every generator-returned UPID and
  exact log across simple start/completion ordering?
- **Prerequisites:** full fixture/preflight; conditional generator approved.
- **Target:** 10–50 tasks; short/low-pressure duration category.
- **Evidence:** all streams, especially raw watch order, ground truth, exact
  logs, and two scan fixed points.
- **Falsification:** one generator-window UPID omitted/lost without gap, or close
  before reconciliation.
- **PASS means:** these generated-window low-volume orderings reconciled.
- **PASS does not mean:** pagination, rotation, overflow, cleanup, or universal
  watcher completeness is proven, or body-start interval membership was
  established.
- **Stops/burden:** all global stops; low operational burden.
- **Skip rule:** never skip; a kill skips 13F–13G.

### 13F — scan/watch creation-race adversary

- **Question:** Can task or lazy-bucket creation between directory scan and
  explicit child-watch installation escape without a gap?
- **Prerequisites:** 13A PASS; controllable observer scheduling/instrumentation
  that does not alter PVE source.
- **Target:** 10–100 tasks across deliberately enumerated timing windows;
  short/burst duration category.
- **Evidence:** watch-add times, raw events, bucket/inode scans, ground truth,
  exact logs, candidate state transitions.
- **Falsification:** omitted UPID with no gap, including a create-before-watch
  and delete-before-scan witness.
- **PASS means:** only the explicitly scheduled in-window generated creation
  interleavings survived.
- **PASS does not mean:** every scheduler/kernel/filesystem interleaving is covered.
- **Stops/burden:** stop on unplanned watch loss; moderate instrumentation burden.
- **Skip rule:** skip after any prior kill; otherwise run early because it can
  cheaply reject the candidate.

### 13B — mutable API offset pagination

- **Question:** Can inserts/completions while `start`/`limit` pages advance
  cause an omission that the independent planes fail to expose?
- **Prerequisites:** 13A and 13F PASS; explicit small page limit and enumerated
  generator/page schedules.
- **Target:** 50–200 tasks spanning at least 3–10 pages; short/medium category.
- **Evidence:** every API request/page/restart plus ground truth, watch, scans,
  and exact reads.
- **Temporal obligation:** at least one page interval strictly overlaps an
  in-window generator request: `page_request_start < generator_request_end`
  and `page_response_end > generator_request_start`. Equality at either edge
  is ambiguous between independently recorded monotonic planes and does not
  exercise pagination during generator activity.
- **Falsification:** missing UPID with no gap; pagination repetition alone may
  not repair the classification.
- **PASS means:** tested page-movement schedules overlapped and reconciled the
  approved in-window generator schedule independently.
- **PASS does not mean:** offset pagination is a snapshot/cursor or is complete
  under arbitrary concurrency.
- **Stops/burden:** normal stops; low-to-moderate task/log burden.
- **Skip rule:** skip after a kill.

### 13C — active-to-archive handoff

- **Question:** Can a UPID disappear between active publication, exact log, and
  archive observation without B-S1 preserving it or latching a gap?
- **Prerequisites:** earlier PASS; controllable mix of short and bounded longer
  workers without adding a new lifecycle experiment.
- **Target:** 25–100 tasks; short/medium transition category.
- **Evidence:** dense active/index/exact captures, completion watches, API
  active/archive/all pages, ground truth.
- **Falsification:** unexplained omitted/lost UPID at handoff.
- **PASS means:** generated-target handoff timings reconciled, including temporary
  absence from active/archive after prior watch/scan discovery while exact
  confirmation remained readable.
- **PASS does not mean:** handoff is atomic, durable across crash, or complete
  for other task types.
- **Stops/burden:** normal stops; moderate sampling burden.
- **Skip rule:** skip after a kill or if a safe duration mix is unavailable.

### 13D — `index` to `index.1` rotation

- **Question:** Does actual threshold crossing/rename cause omission or false
  close during scans, watches, and pagination?
- **Prerequisites:** earlier PASS; measured line sizes/starting index; approved
  disk and operation cap sufficient for one observed rotation.
- **Target:** computed crossing volume with a planning ceiling of 1,000 short
  tasks from an empty/small index; medium/high-volume category, never an
  unbounded loop.
- **Evidence:** per-entry ground truth and generator-sequence binding,
  stat/inode/hash/raw content before and after actual rotation, watch renames,
  scans, pages, and exact logs.
- **Falsification:** any UPID omitted/lost without gap, or anchors hide an
  intermediate deletion.
- **PASS means:** one or more explicitly observed crossings inside the approved
  generated run reconciled with no ambient pressure task.
- **PASS does not mean:** count predicts rotation, retention is indefinite, or
  all filesystems/kernels behave identically.
- **Stops/burden:** tight disk/log health limits; high record volume.
- **Skip rule:** skip after a kill or if the approved cap cannot safely cross.

### 13E — watcher overflow/invalidation

- **Question:** Does B-S1 reliably latch and preserve a gap when the kernel
  reports intentional bounded overflow or watch invalidation?
- **Prerequisites:** earlier PASS; separately approved, isolated injection
  method and recovery plan. A synthetic injection may precede any kernel test.
- **Target:** minimum activity needed to produce the approved signal; bounded
  fault-injection duration, not a promised count.
- **Evidence:** raw overflow/invalidation event, watch descriptor lifecycle,
  observer latch, heartbeats, final scans and health.
- **Falsification:** candidate reaches `CLOSED_COMPLETE` after the signal or
  silently clears/replaces the gap.
- **PASS means:** the tested signal forced `B_S1_GAP_DETECTED`.
- **PASS does not mean:** overflow can always be induced, every loss emits a
  signal, or completeness is restored afterward.
- **Stops/burden:** stop immediately after signal capture; moderate/high fault
  burden and separately approved recovery.
- **Skip rule:** skip after any kill; kernel injection may be omitted if it
  cannot be bounded safely, leaving that question open.

### 13G — combined bounded pressure

- **Question:** Can combined page movement, rapid transitions, real rotation,
  watch drain, recursive scans, and approved cleanup pressure yield a false
  close despite the earlier isolated results?
- **Prerequisites:** 13A/13F/13B/13C/13D PASS and 13E gap behavior understood;
  a new explicit combined-load approval.
- **Target:** no more than a separately approved maximum and only the minimum
  needed for the explicitly combined schedules; high-pressure bounded duration
  category. This protocol supplies no default 13G task count.
- **Evidence:** every stream, actual rotation and retention markers, resource
  health telemetry, and enumerated schedule ledger.
- **Falsification:** any generator-window enumeration omission witness or
  failure to latch a required gap.
- **PASS means:** only the combined phenomena explicitly selected in that
  run's `required_phenomena` contract reconciled; it does not mean every
  phenomenon named in this narrative scenario was exercised.
- **PASS does not mean:** Phase S proven universally, Phase M designed, Family B
  solved, Blocker B closed, or trust granted.
- **Stops/burden:** all stops with the tightest count/rate/disk/health bounds;
  highest operational burden.
- **Skip rule:** mandatory skip after any earlier kill or unresolved harness,
  source, generator, fixture, or safety condition.

## 17. Unresolved before any live experiment

The following remain open and require a separate reviewed approval:

- name and validate a disposable fixture satisfying section 13;
- prove installed and loaded-code context, kernel/filesystem/mount topology, and
  reader permissions for that fixture;
- accept or reject the conditional absent-slot `stopall` generator, including
  a race-safe absence guard and exact request method;
- select non-production identifiers only after fixture allocation;
- implement and review a fixture-only collector/generator; this PR intentionally
  implements neither;
- define numeric CPU/RAM/disk/log/task-rate/count/duration stop limits;
- define exact enumerated interleavings and T0/T1 clock-correlation procedure;
- decide whether and how cleanup pressure or kernel overflow/invalidation can be
  induced without broadening the experiment;
- review raw-evidence capture atomicity limitations and evidence-store sealing;
- obtain explicit operator approval for each subrun and any PVE action; and
- complete the later architecture review if evidence ever supports changing
  B-S1 or the accepted status. A successful experiment alone changes nothing.
