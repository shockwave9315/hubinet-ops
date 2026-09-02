#!/usr/bin/env python3
"""Dark forced-command PVE boundary for ONE real workload package mutation.

**Not deployed.** Bootstrap and the product updater never install this file,
never provision an `authorized_keys` forced-command entry for it, and never
grant it or the identity that would run it any PVE privilege. It exists in
this repository only so hermetic tests can exercise crash-safe package
mutation end to end; see `app/package_update_mutation.py`,
`app/package_update_mutation_host_control.py`, and `ARCHITECTURE.md`,
"Crash-safe package mutation".

It is a separate file and a separate, deliberately STRONGER logical
privilege boundary than the other three helpers:

- `deploy/hubinet-package-scan-helper.py` is the only helper production ever
  deploys, and stays scan-only;
- `deploy/hubinet-package-snapshot-helper.py` submits/seals one PVE snapshot
  and never touches a package;
- `deploy/hubinet-package-update-helper.py` promises NO workload mutation at
  all, and that promise is kept: no real package command was added to it.

This is the only file in the repository that can change a workload package,
and it can do exactly one thing to one guest, exactly once per job.

## The one real package command

```text
env LC_ALL=C DEBIAN_FRONTEND=noninteractive
  apt-get -y
    -o APT::Get::Upgrade-Allow-New=false
    -o APT::Get::Remove=false
    -o APT::Get::Force-Yes=false
    -o APT::Get::allow-downgrades=false
    -o APT::Get::allow-remove-essential=false
    -o APT::Get::allow-change-held-packages=false
    -o APT::Get::AllowUnauthenticated=false
    -o APT::Ignore-Hold=false
    -o Dpkg::Options::=--force-confdef
    -o Dpkg::Options::=--force-confold
    -o DPkg::Pre-Install-Pkgs::=/run/hubinet-ops/package-mutation/<op>/verify-action-set
    -o DPkg::Tools::Options::/run/hubinet-ops/package-mutation/<op>/verify-action-set::Version=3
    upgrade
```

Fixed argv. No shell. No caller-supplied option, package name, version, or
command text ever reaches it -- the approved package material is used only
to *refuse* the mutation, never to build it, so there is structurally no
value a package name could take that changes what runs. `<op>` is this
job's own canonical `mutation_operation_id` UUID and is the only
interpolated value anywhere in the command line.

## The pre-dpkg action gate

The two hook options install this operation's own exact action gate (see
`build_action_set_verifier`). APT locking does not span two `apt-get`
invocations, so between PREPARE's simulation and this command an ordinary
actor can complete an `apt-get update`, release a hold, add a source, or
change a pin, and the real resolver can then legitimately choose a
DIFFERENT action set while every installed version still matches the
approved plan. The gate makes this invocation's OWN resolved action stream
the thing that must equal the authority-accepted material, and APT aborts
before dpkg receives any package operation if it does not.

It does not replace the post-state completion proof, which stays: the gate
PREVENTS unapproved material reaching dpkg; the post-state proof PROVES the
exact approved transition actually completed.

**Why it cannot exceed the approved plan** (traced in current upstream
`apt-team/apt`, not assumed):

- `apt-get upgrade` is `DoUpgrade` -> `DoUpgradeNoNewPackages` ->
  `APT::Upgrade::Upgrade(FORBID_REMOVE_PACKAGES|FORBID_INSTALL_NEW_PACKAGES)`
  -> `pkgAllUpgradeNoNewPackages` (`apt-private/private-upgrade.cc`,
  `apt-pkg/upgrade.cc`). That resolver marks install ONLY for packages with
  `I->CurrentVer != 0 && Cache[I].InstallVer != 0`, with `AutoInst=false`
  (so it never pulls a new dependency in), and resolves everything left over
  by *keep* (`ResolveByKeepInternal`). Installing a new package and removing
  an existing one are therefore structurally impossible, not merely
  discouraged.
- `apt-get -s upgrade` -- the simulation this stage proves the plan from --
  takes the identical path: `-s` only replaces the final package manager
  with `pkgSimulate` at the very end of `InstallPackages`
  (`apt-private/private-install.cc`), after the resolver has already run.
  So the simulation is the real plan for the same cache inputs, not an
  approximation of it.
- The `-o` options above are belt-and-braces against a guest
  `apt.conf.d` snippet flipping a default in the dangerous direction
  (`Upgrade-Allow-New` would let `upgrade` install new packages;
  `Ignore-Hold` would let it change held ones). Command-line `-o` wins over
  configuration files. `APT::Get::Remove=false` makes any planned removal a
  hard error before dpkg is invoked at all.
- `-y` does not change the resolver. It replaces the confirmation prompt and
  turns an essential removal, a downgrade, or a held-package change into a
  hard pre-mutation error instead of a question.

**Why it cannot hang** (traced in current upstream dpkg and apt):

- dpkg only ever prompts about a conffile that was BOTH modified locally and
  changed by the package (`conffoptcells`, `src/main/configure.c`); on
  end-of-file at that prompt it does not default -- it calls `ohshit("end of
  file on <standard input> at conffile prompt")` and aborts mid-transaction.
  This helper's stdin is `/dev/null`, so a conffile policy is mandatory, not
  cosmetic. `--force-confdef --force-confold` resolves every such case
  without a prompt, **keeping the operator's file** and leaving the
  distributor's as `<file>.dpkg-dist`.
- `DEBIAN_FRONTEND=noninteractive` puts debconf in its non-interactive
  frontend, and `needrestart` reads that same variable to disable its own
  interactive service-restart prompt (`$debian_noninteractive`,
  upstream `needrestart`).
- Every command runs with stdin on `/dev/null`, no controlling terminal, and
  a bounded wall-clock timeout. A timed-out mutation is killed and journaled
  as a terminal failure -- never left to be resubmitted.

## At-most-once, across crashes

One job owns one deterministic `mutation_operation_id` (derived by the
backend from immutable authority facts). Every operation for it is journaled
on THIS host, by that id, with fsynced atomic renames, and submission,
sealing, and preparation are serialized by a non-blocking per-VMID `flock`:

```text
absent -> intent -> sealed_not_submitted        (durably never submitted)
                 -> submitted -> terminal_success | terminal_failure
```

`absent` is the ordinary state before the backend has armed anything.
Preparation is deliberately read-only in the durable sense too -- it writes
NO journal record -- so `intent` means exactly one thing: an already-armed
operation reached this submit-capable boundary and has not yet crossed
`submitted`. Both pre-submission states are sealable, under the same lease,
into the durable `sealed_not_submitted` fence.

`submitted` is written and fsynced BEFORE the package command is launched,
and is never resubmitted from -- it is the genuinely uncertain window. The
real command is run by a detached runner in its own session, reparented to
PID 1, holding the per-VMID lease for its whole life, so an SSH disconnect,
a client timeout, or a backend crash cannot terminate it and cannot cause a
second invocation. If the runner dies anyway, the journal simply stays at
`submitted` with the lease free: durably uncertain, never retried.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import subprocess
import sys
import time
import uuid
from typing import Any


MAX_REQUEST_BYTES = 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 6 * 1024 * 1024
MAX_INVENTORY_OUTPUT_BYTES = 32 * 1024 * 1024
#: Bound for every read-only/bookkeeping command. Never the mutation itself.
COMMAND_TIMEOUT_SECONDS = 300.0
#: Bound for the one real package command, run by the detached runner well
#: outside any SSH round trip or backend transaction.
MUTATION_COMMAND_TIMEOUT_SECONDS = 3600.0
#: Bounded tail of the mutation's own output retained as durable evidence.
MAX_RETAINED_OUTPUT_BYTES = 64 * 1024
#: Structural bound on how large an approved plan this boundary will act on.
MAX_EXPECTED_PACKAGES = 5000

JOURNAL_DIRECTORY = Path("/var/lib/hubinet-ops/package-mutation-operations")

NODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,62}")
FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}")
ARCHITECTURE_RE = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")
APT_VERSION_RE = re.compile(
    r"apt ([0-9]+)\.([0-9]+)\.([0-9]+)"
    r"(?:[~+.-][A-Za-z0-9.+:~-]*)? \([A-Za-z0-9_-]+\)"
)
MINIMUM_APT_VERSION = (2, 1, 16)
BUSY_PATTERNS = (
    "could not get lock",
    "unable to acquire the dpkg frontend lock",
    "is another process using it",
    "could not open lock file",
)

OPERATIONS = (
    "prepare_exact_package_mutation",
    "execute_exact_package_mutation",
    "seal_mutation_never_submitted",
    "inspect_package_mutation_state",
)

#: dpkg's own mid-transaction status words. A guest carrying any of these
#: before the mutation cannot be reasoned about, so the mutation is refused.
DPKG_UNFINISHED_STATUS_WORDS = frozenset(
    {
        "half-installed",
        "unpacked",
        "half-configured",
        "triggers-awaited",
        "triggers-pending",
    }
)

#: Hubinet-owned ephemeral staging root INSIDE the managed guest. `/run` is
#: a tmpfs on every supported Debian-family system, so nothing staged here
#: survives a guest reboot, and the whole root is removed and recreated
#: under this guest's per-VMID lease immediately before each submission --
#: a crash leftover is therefore harmless and can never authorize a later
#: job. The path is a code-owned literal; only the canonical operation UUID
#: (`_canonical_uuid`) is interpolated.
GUEST_STAGING_ROOT = "/run/hubinet-ops/package-mutation"
GUEST_VERIFIER_NAME = "verify-action-set"
GUEST_MANIFEST_NAME = "expected-actions"

#: First line of the staged manifest. It binds those exact bytes to exactly
#: one mutation operation, and the verifier -- which carries the same
#: operation id as its own literal -- refuses anything else.
MANIFEST_HEADER = "hubinet-ops-package-mutation-actions v1"

#: Strict grammar for every value that becomes a manifest field. The
#: manifest is a whitespace-separated record format, so a name or version
#: carrying whitespace, a newline, or a non-printable byte could otherwise
#: forge an extra approved action. Debian's own package-name and version
#: grammars are narrower than these, so a legitimate row always passes and
#: anything else fails closed before a single file is staged.
MANIFEST_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+._-]*")
MANIFEST_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+.~:_-]*")

#: The fixed options of the ONE real workload package command, minus the
#: two that name this operation's own pre-dpkg action gate.
_MUTATION_BASE_OPTIONS: tuple[str, ...] = (
    "env",
    "LC_ALL=C",
    "DEBIAN_FRONTEND=noninteractive",
    "apt-get",
    "-y",
    "-o",
    "APT::Get::Upgrade-Allow-New=false",
    "-o",
    "APT::Get::Remove=false",
    "-o",
    "APT::Get::Force-Yes=false",
    "-o",
    "APT::Get::allow-downgrades=false",
    "-o",
    "APT::Get::allow-remove-essential=false",
    "-o",
    "APT::Get::allow-change-held-packages=false",
    "-o",
    "APT::Get::AllowUnauthenticated=false",
    "-o",
    "APT::Ignore-Hold=false",
    "-o",
    "Dpkg::Options::=--force-confdef",
    "-o",
    "Dpkg::Options::=--force-confold",
)


def guest_staging_directory(mutation_operation_id: str) -> str:
    """This operation's own staging directory inside the guest."""

    operation = _canonical_uuid(mutation_operation_id, "mutation_operation_id")
    return f"{GUEST_STAGING_ROOT}/{operation}"


def guest_verifier_path(mutation_operation_id: str) -> str:
    return f"{guest_staging_directory(mutation_operation_id)}/{GUEST_VERIFIER_NAME}"


def guest_manifest_path(mutation_operation_id: str) -> str:
    return f"{guest_staging_directory(mutation_operation_id)}/{GUEST_MANIFEST_NAME}"


def mutation_argv(mutation_operation_id: str) -> tuple[str, ...]:
    """The complete, fixed argv of the ONE real workload package command.

    Built here, never from the request: the approved material is only ever
    used to REFUSE the mutation, never to construct it, so there is
    structurally no value a package name or version could take that changes
    what runs.

    The two trailing options install this operation's own pre-dpkg action
    gate (see :func:`build_action_set_verifier`). Both values are code-owned
    literals plus one canonical UUID; neither carries a package value, a
    request-provided option, or any shell fragment. APT natively invokes a
    `DPkg::Pre-Install-Pkgs` command through a shell, but the command here
    is a single bare path with no metacharacter, no argument, and no
    expansion, so that native shell step has nothing left to interpret --
    which is why this is not an "arbitrary command string across a
    privileged boundary": there is no request-derived text in it at all.

    The hook command MUST stay a bare path: APT keys
    `DPkg::Tools::Options::<cmd>::Version` on the exact command string, and a
    command containing a space does not resolve its own version option --
    APT then falls back to protocol Version 1, which the verifier rejects.
    Command-line `-o` is applied after every configuration file, so an
    ordinary guest `apt.conf`/`apt.conf.d` snippet can neither `#clear` this
    hook out of the list nor pin it back to an older protocol.
    """

    verifier = guest_verifier_path(mutation_operation_id)
    return (
        *_MUTATION_BASE_OPTIONS,
        "-o",
        f"DPkg::Pre-Install-Pkgs::={verifier}",
        "-o",
        f"DPkg::Tools::Options::{verifier}::Version=3",
        "upgrade",
    )


def build_expected_action_manifest(
    mutation_operation_id: str, packages: list[dict[str, str]]
) -> str:
    """Render the exact V3 action set the approved plan is allowed to produce.

    One canonical, whitespace-separated record per action, in APT's own
    Version 3 field order:

    ```text
    <name> <old version> <old arch> <ma> <dir> <new version> <new arch> <ma> <action>
    ```

    Every approved upgrade contributes exactly two actions -- the unpack of
    its `.deb` and its configure -- because that is what APT's Version 3
    stream emits for an upgraded binary, verified against real APT output.

    Three fields are deliberately canonicalized rather than bound:

    - the two MultiArch-type fields become `-`. APT reports the MultiArch
      type *of the very version being acted on*, and that legitimately
      differs between the installed and candidate versions of one package
      (observed: `becomesall 2.0 amd64 none < 2.1 all foreign`). PREPARE
      cannot learn the candidate's MultiArch type from the simulation, so
      binding it would fail-close on legal upgrades while adding nothing:
      (name, version, architecture) is already the complete binary identity
      dpkg acts on. The verifier still requires each field to be one of
      APT's four documented type words, so a malformed record fails closed.
    - the `.deb` path becomes the class token `UNPACK`. Where APT cached the
      archive is not part of the approved transition, and the archive's
      contents are already pinned by name, version, and architecture.

    Both architecture fields ARE bound exactly, and to the same value: the
    canonical simulation parser (`app/package_scan.py`) refuses any row
    whose candidate architecture differs from its proven installed
    architecture, so no approved row can ever need two. This matters because
    a Version 3 record carries the package name WITHOUT its architecture
    qualifier, so the architecture fields are the only thing distinguishing
    `foo:amd64` from `foo:i386`.
    """

    operation = _canonical_uuid(mutation_operation_id, "mutation_operation_id")
    lines = [f"{MANIFEST_HEADER} {operation}"]
    for package in packages:
        name = package["package_name"]
        architecture = package["architecture"]
        installed = package["installed_version"]
        candidate = package["candidate_version"]
        if not MANIFEST_NAME_RE.fullmatch(name):
            raise MutationError(
                "malformed_plan",
                "approved package name cannot be expressed as an exact action",
            )
        if not ARCHITECTURE_RE.fullmatch(architecture):
            raise MutationError(
                "malformed_plan",
                "approved package architecture cannot be expressed as an "
                "exact action",
            )
        for version in (installed, candidate):
            if not MANIFEST_VERSION_RE.fullmatch(version):
                raise MutationError(
                    "malformed_plan",
                    "approved package version cannot be expressed as an "
                    "exact action",
                )
        if installed == candidate:
            raise MutationError(
                "malformed_plan",
                "approved package material is not an upgrade",
            )
        for action in ("UNPACK", "CONFIGURE"):
            lines.append(
                f"{name} {installed} {architecture} - < "
                f"{candidate} {architecture} - {action}"
            )
    return "".join(f"{line}\n" for line in lines)


def build_action_set_verifier(mutation_operation_id: str) -> str:
    """Render this operation's pre-dpkg action gate.

    ## What it is

    A `DPkg::Pre-Install-Pkgs` hook running APT's protocol **Version 3**. APT
    invokes every such hook once per `pkgDPkgPM::Go()`, BEFORE the loop that
    invokes dpkg, passing the complete resolved action list for the whole
    transaction; if a hook exits non-zero APT aborts and dpkg never receives
    a single package operation. Both properties are upstream behaviour
    (`apt-pkg/deb/dpkgpm.cc`: `RunScriptsWithPkgs("DPkg::Pre-Install-Pkgs")`
    ahead of the dpkg loop, `SendPkgsInfo` over the entire `List`, and
    `_error->Error("Failure running script ...")` returning false) and were
    re-verified against a real `apt` in an isolated APT root: a hook exiting
    1 left the dpkg invocation count at zero.

    ## Why it exists

    APT locking does not span two separate `apt-get` invocations. Between
    this stage's PREPARE simulation and the real upgrade, an ordinary actor
    can complete an `apt-get update`, change a pin, add a source, or release
    a hold, so the real resolver can legitimately choose a *different* action
    set while every installed version still matches the approved plan. The
    post-state proof would notice afterwards -- but the unapproved package
    would already be installed. This gate makes the real invocation's own
    resolved action stream the thing that must equal the authority-accepted
    material, before dpkg is reached.

    ## Runtime

    `/bin/sh` plus `sort`, `tail`, and `printf`. `dash` (which provides
    `/bin/sh`) and `coreutils` are both `Essential: yes` under Debian
    Policy, so this adds no product prerequisite to the supported guest
    contract and needs no Python, Perl, or awk inside the guest. The script
    takes no argument and reads the stream from stdin -- APT's default
    `InfoFD` -- deliberately avoiding `<&$APT_HOOK_INFO_FD`, which is a
    syntax error in `dash` and would abort the mutation for the wrong
    reason.

    Only two code-owned literals are substituted: this operation's canonical
    UUID and the staging path derived from it. No package name, version, or
    other request value ever enters the script text.
    """

    operation = _canonical_uuid(mutation_operation_id, "mutation_operation_id")
    directory = guest_staging_directory(operation)
    return f"""#!/bin/sh
# Hubinet Ops exact package-mutation action gate.
#
# Generated per operation by deploy/hubinet-package-mutation-helper.py and
# staged into this guest's tmpfs. Never edited in place, never reused across
# operations. Refusing here aborts APT before dpkg receives any package
# operation.
set -u
LC_ALL=C
export LC_ALL

OPERATION='{operation}'
DIR='{directory}'
MANIFEST="$DIR/{GUEST_MANIFEST_NAME}"
WANT="$DIR/expected.sorted"
GOT="$DIR/observed.sorted"

refuse() {{
    echo "hubinet-ops: refusing package mutation before dpkg: $1" >&2
    exit 1
}}

[ -f "$MANIFEST" ] || refuse "expected action manifest is missing"

# The manifest header binds these bytes to exactly this operation, so a
# leftover manifest from any other operation can never authorize this one.
IFS= read -r header < "$MANIFEST" || refuse "expected action manifest is empty"
[ "$header" = "{MANIFEST_HEADER} $OPERATION" ] ||
    refuse "expected action manifest belongs to another operation"

# 1. Protocol. APT silently falls back to its highest supported version when
#    a newer one is requested, and a hook command it cannot key an option to
#    simply gets Version 1, so the marker is REQUIRED to read Version 3.
IFS= read -r protocol || refuse "hook protocol stream was empty"
[ "$protocol" = "VERSION 3" ] ||
    refuse "hook protocol is not Version 3"

# 2. APT's configuration space, terminated by one empty line. Every special
#    character in it is %-encoded, so no key or value can contain a raw
#    newline and this terminator is unambiguous.
saw_blank=no
while IFS= read -r line; do
    if [ -z "$line" ]; then
        saw_blank=yes
        break
    fi
done
[ "$saw_blank" = yes ] || refuse "hook protocol stream had no action section"

# 3. The exact action stream APT is about to hand dpkg, normalized into the
#    manifest's own canonical form.
while read -r pkg oldv olda oldm dir newv newa newm action rest; do
    [ -n "$pkg" ] || continue
    [ -z "$rest" ] || refuse "action record has unexpected trailing fields"
    [ -n "$oldm" ] && [ -n "$newm" ] && [ -n "$action" ] ||
        refuse "action record is truncated"
    for multiarch in "$oldm" "$newm"; do
        case "$multiarch" in
            same|foreign|allowed|none) ;;
            *) refuse "action record has an unknown MultiArch type" ;;
        esac
    done
    case "$action" in
        '**CONFIGURE**') class=CONFIGURE ;;
        '**REMOVE**') refuse "APT planned to remove an installed package" ;;
        /*.deb) class=UNPACK ;;
        *) refuse "APT planned an action this product does not perform" ;;
    esac
    printf '%s %s %s - %s %s %s - %s\\n' \\
        "$pkg" "$oldv" "$olda" "$dir" "$newv" "$newa" "$class"
done > "$GOT.raw" || refuse "could not read the resolved action stream"

# 4. Exact action-set equality against the authority-accepted material.
#    Both sides are sorted here, in this guest, under the same collation, so
#    the comparison is an exact multiset equality that does not depend on
#    the order APT happens to plan its transaction in.
tail -n +2 "$MANIFEST" | sort > "$WANT" ||
    refuse "could not read the approved action set"
sort < "$GOT.raw" > "$GOT" || refuse "could not order the resolved action set"
[ -s "$WANT" ] || refuse "approved action set is empty"
[ -s "$GOT" ] || refuse "resolved action set is empty"

exec 3< "$WANT" || refuse "could not open the approved action set"
exec 4< "$GOT" || refuse "could not open the resolved action set"
while :; do
    want=''
    got=''
    IFS= read -r want <&3
    want_read=$?
    IFS= read -r got <&4
    got_read=$?
    if [ "$want_read" -ne 0 ] && [ "$got_read" -ne 0 ]; then
        break
    fi
    [ "$want_read" -eq 0 ] && [ "$got_read" -eq 0 ] ||
        refuse "resolved action set is not the approved action set"
    [ "$want" = "$got" ] ||
        refuse "resolved action differs from the approved plan"
done
exec 3<&-
exec 4<&-

exit 0
"""

NATIVE_ARCHITECTURE_ARGV = ("env", "LC_ALL=C", "dpkg", "--print-architecture")
INSTALLED_INVENTORY_ARGV = (
    "env",
    "LC_ALL=C",
    "dpkg-query",
    "-W",
    "-f=${Package}\\t${Architecture}\\t${Version}\\t${db:Status-Status}\\n",
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_exceeded: bool = False


#: A bounded command runner. `stdin` carries structured payload BYTES for
#: the staging commands only; it is never command text and never argv.
Runner = Callable[..., CommandResult]


class RequestError(ValueError):
    """The request itself is not a well-formed package-mutation request."""


class MutationError(RuntimeError):
    """A package-mutation operation could not be carried out safely."""

    def __init__(self, classification: str, message: str) -> None:
        super().__init__(message)
        self.classification = classification
        self.message = message


def _run_bounded(
    argv: tuple[str, ...], timeout: float, max_output: int, stdin: bytes = b""
) -> CommandResult:
    process = subprocess.Popen(  # noqa: S603 - every argv shape is fixed above
        argv,
        stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        close_fds=True,
    )
    assert process.stdout is not None
    assert process.stderr is not None
    if stdin:
        # Structured payload bytes only -- never command text, never argv.
        # Written before the output pump starts, which is safe because the
        # only commands given a payload stream it straight to a file (`dd`,
        # or `ssh` forwarding to it) and produce no output of their own, so
        # there is no output backpressure to deadlock against. A child that
        # exits early closes the pipe instead of blocking, and that shows up
        # as a non-zero return code below rather than an exception here.
        assert process.stdin is not None
        try:
            process.stdin.write(stdin)
        except BrokenPipeError:
            pass
        finally:
            try:
                process.stdin.close()
            except BrokenPipeError:
                pass
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    output = {"stdout": bytearray(), "stderr": bytearray()}
    started = time.monotonic()
    timed_out = False
    exceeded = False
    try:
        while selector.get_map():
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                timed_out = True
                process.kill()
                break
            for key, _ in selector.select(min(remaining, 0.2)):
                chunk = os.read(key.fileobj.fileno(), 65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output[key.data].extend(chunk)
                if len(output["stdout"]) + len(output["stderr"]) > max_output:
                    exceeded = True
                    process.kill()
                    break
            if exceeded:
                break
    finally:
        selector.close()
        process.wait(timeout=5)
    return CommandResult(
        process.returncode,
        bytes(output["stdout"][: max_output + 1]),
        bytes(output["stderr"][: max_output + 1]),
        timed_out,
        exceeded,
    )


# ---------------------------------------------------------------------------
# Request validation
# ---------------------------------------------------------------------------


def _canonical_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise RequestError(f"{field} must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise RequestError(f"{field} must be a canonical UUID") from exc
    if parsed.int == 0 or str(parsed) != value:
        raise RequestError(f"{field} must be a canonical UUID")
    return value


def _positive_integer(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise RequestError(f"{field} must be a positive integer")
    return value


def _bounded_text(value: Any, field: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise RequestError(f"{field} is invalid")
    return value


def _canonical_json(payload: Any) -> str:
    return json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    )


def plan_fingerprint(packages: list[dict[str, str]]) -> str:
    """Recompute the backend's own exact material plan fingerprint.

    Byte-identical to `app.inventory.authority.package_plan_fingerprint`:
    SHA-256 over compact, ASCII, key-sorted JSON of the material quadruples
    sorted by ``(package_name, architecture)``. Recomputing it here means the
    material this boundary is asked to act on and the digest the journal
    binds can never disagree -- a request carrying a fingerprint that does
    not describe its own package list is refused rather than journaled.
    """

    payload = [
        {
            "architecture": package["architecture"],
            "candidate_version": package["candidate_version"],
            "installed_version": package["installed_version"],
            "package_name": package["package_name"],
        }
        for package in sorted(
            packages, key=lambda row: (row["package_name"], row["architecture"])
        )
    ]
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _validate_expected_packages(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise RequestError("expected_packages must be a non-empty list")
    if len(value) > MAX_EXPECTED_PACKAGES:
        raise RequestError("expected_packages exceeded its structural bound")
    packages: list[dict[str, str]] = []
    identities: set[tuple[str, str]] = set()
    for row in value:
        if not isinstance(row, Mapping) or set(row) != {
            "package_name",
            "architecture",
            "installed_version",
            "candidate_version",
        }:
            raise RequestError("expected_packages row has the wrong shape")
        name = _bounded_text(row["package_name"], "package_name", max_length=300)
        architecture = row["architecture"]
        if not isinstance(architecture, str) or not ARCHITECTURE_RE.fullmatch(
            architecture
        ):
            raise RequestError("expected_packages row architecture is invalid")
        installed = _bounded_text(
            row["installed_version"], "installed_version", max_length=500
        )
        candidate = _bounded_text(
            row["candidate_version"], "candidate_version", max_length=500
        )
        identity = (name, architecture)
        if identity in identities:
            raise RequestError(
                "expected_packages contains a duplicate (package_name, architecture)"
            )
        identities.add(identity)
        packages.append(
            {
                "package_name": name,
                "architecture": architecture,
                "installed_version": installed,
                "candidate_version": candidate,
            }
        )
    return packages


def validate_request(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != {
        "request_version",
        "operation",
        "target",
        "context",
        "operation_identity",
        "expected_packages",
        "prepared_evidence_digest",
    }:
        raise RequestError("request must have the exact package-mutation shape")
    if payload["request_version"] != 1:
        raise RequestError("unsupported request version")
    operation = payload["operation"]
    if operation not in OPERATIONS:
        raise RequestError("unknown host-control operation")

    target = payload["target"]
    if not isinstance(target, Mapping) or set(target) != {"vmid", "expected_node"}:
        raise RequestError("target must have the exact package-mutation shape")
    vmid = target["vmid"]
    if type(vmid) is not int or not 100 <= vmid <= 999_999_999:
        raise RequestError("vmid must be a valid PVE integer VMID")
    expected_node = target["expected_node"]
    if not isinstance(expected_node, str) or not NODE_RE.fullmatch(expected_node):
        raise RequestError("expected_node is invalid")

    context = payload["context"]
    if not isinstance(context, Mapping) or set(context) != {
        "backend_instance_id",
        "job_id",
        "resource_id",
        "binding_id",
        "locator_generation",
        "resource_continuity_revision",
    }:
        raise RequestError("context must have the exact package-mutation shape")
    normalized_context = {
        "backend_instance_id": _canonical_uuid(
            context["backend_instance_id"], "backend_instance_id"
        ),
        "job_id": _canonical_uuid(context["job_id"], "job_id"),
        "resource_id": _canonical_uuid(context["resource_id"], "resource_id"),
        "binding_id": _canonical_uuid(context["binding_id"], "binding_id"),
        "locator_generation": _positive_integer(
            context["locator_generation"], "locator_generation"
        ),
        "resource_continuity_revision": _positive_integer(
            context["resource_continuity_revision"], "resource_continuity_revision"
        ),
    }

    identity = payload["operation_identity"]
    if not isinstance(identity, Mapping) or set(identity) != {
        "mutation_operation_id",
        "plan_fingerprint",
    }:
        raise RequestError(
            "operation_identity must have the exact package-mutation shape"
        )
    mutation_operation_id = _canonical_uuid(
        identity["mutation_operation_id"], "mutation_operation_id"
    )
    fingerprint = identity["plan_fingerprint"]
    if not isinstance(fingerprint, str) or not FINGERPRINT_RE.fullmatch(fingerprint):
        raise RequestError("plan_fingerprint is invalid")

    packages = _validate_expected_packages(payload["expected_packages"])
    if plan_fingerprint(packages) != fingerprint:
        raise RequestError(
            "expected_packages do not hash to the declared plan_fingerprint"
        )

    prepared = payload["prepared_evidence_digest"]
    if operation == "execute_exact_package_mutation":
        if not isinstance(prepared, str) or not FINGERPRINT_RE.fullmatch(prepared):
            raise RequestError("prepared_evidence_digest is invalid")
    elif prepared is not None:
        raise RequestError(
            "prepared_evidence_digest is only meaningful when executing"
        )

    return {
        "operation": operation,
        "vmid": vmid,
        "expected_node": expected_node,
        "context": normalized_context,
        "mutation_operation_id": mutation_operation_id,
        "plan_fingerprint": fingerprint,
        "expected_packages": packages,
        "prepared_evidence_digest": prepared,
    }


def request_fingerprint(request: Mapping[str, Any]) -> str:
    """Canonical hash of the exact request this operation identity commits to.

    Every fact that makes the operation *this* job's operation on *this*
    guest incarnation is inside: a request that differs in any of them is a
    different request and is refused rather than allowed to reuse the
    journal.
    """

    return hashlib.sha256(
        _canonical_json(
            {
                "vmid": request["vmid"],
                "expected_node": request["expected_node"],
                "mutation_operation_id": request["mutation_operation_id"],
                "plan_fingerprint": request["plan_fingerprint"],
                "context": dict(request["context"]),
            }
        ).encode("ascii")
    ).hexdigest()


def evidence_digest(evidence: Mapping[str, str]) -> str:
    """Canonical hash of the exact read-only evidence a prepare returned."""

    return hashlib.sha256(
        _canonical_json(
            {
                "os_release": evidence["os_release"],
                "native_architecture": evidence["native_architecture"],
                "installed_inventory": evidence["installed_inventory"],
                "simulation_stdout": evidence["simulation_stdout"],
            }
        ).encode("utf-8")
    ).hexdigest()


# ---------------------------------------------------------------------------
# Durable per-operation journal
# ---------------------------------------------------------------------------


JOURNAL_PHASES = (
    "intent",
    "sealed_not_submitted",
    "submitted",
    "terminal_success",
    "terminal_failure",
)

#: Phases that carry the mutation's own durable result evidence, and the
#: only phases allowed to.
TERMINAL_PHASES = ("terminal_success", "terminal_failure")


def _result_is_success(result: Mapping[str, Any]) -> bool:
    """The single definition of "this mutation command succeeded".

    Used by the writer and the reader, so a terminal record can never be
    written under one rule and read under another. Deliberately requires
    readable post-state evidence too: a zero exit with no independent
    post-state proves nothing the backend could act on, so it is a failure
    of the OPERATION even though the command itself exited cleanly.
    """

    return (
        result["exit_code"] == 0
        and result["timed_out"] is False
        and bool(result["post_installed_inventory"])
        and bool(result["post_native_architecture"])
    )


class OperationJournal:
    """Atomic, fsynced, per-operation package-mutation journal on the host."""

    def __init__(self, directory: Path = JOURNAL_DIRECTORY) -> None:
        self._directory = Path(directory)

    @property
    def directory(self) -> Path:
        return self._directory

    def _path(self, mutation_operation_id: str) -> Path:
        # The id is a validated canonical UUID, so this never escapes.
        return self._directory / f"op-{mutation_operation_id}.json"

    def ensure_directory(self) -> None:
        self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def read(self, mutation_operation_id: str) -> dict[str, Any] | None:
        path = self._path(mutation_operation_id)
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise MutationError(
                "journal_unreadable", "package mutation journal is unreadable"
            ) from exc
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise MutationError(
                "journal_corrupt", "package mutation journal is corrupt"
            ) from exc
        if (
            not isinstance(record, dict)
            or record.get("journal_version") != 1
            or record.get("mutation_operation_id") != mutation_operation_id
            or record.get("phase") not in JOURNAL_PHASES
            or not isinstance(record.get("request_fingerprint"), str)
            or type(record.get("vmid")) is not int
        ):
            raise MutationError(
                "journal_corrupt", "package mutation journal is corrupt"
            )
        phase = record["phase"]
        # A phase is only usable with exactly the facts that phase promises.
        # A record that carries mutation result evidence in a pre-submission
        # phase, or a terminal record missing its evidence, is contradictory
        # and must never degrade into something that looks safe to act on.
        if phase in ("intent", "sealed_not_submitted", "submitted"):
            if record.get("result") is not None:
                raise MutationError(
                    "journal_corrupt",
                    "package mutation journal phase carries incompatible evidence",
                )
        else:
            result = record.get("result")
            if (
                not isinstance(result, dict)
                or type(result.get("exit_code")) is not int
                or not isinstance(result.get("pre_installed_inventory"), str)
                or not isinstance(result.get("post_installed_inventory"), str)
                or not isinstance(result.get("post_native_architecture"), str)
                or not isinstance(result.get("timed_out"), bool)
            ):
                raise MutationError(
                    "journal_corrupt",
                    "package mutation journal records a terminal phase without "
                    "its result evidence",
                )
            # The phase and the evidence must agree exactly, in BOTH
            # directions, so a failure can never be read as a success by
            # trusting one and ignoring the other.
            if (phase == "terminal_success") != _result_is_success(result):
                raise MutationError(
                    "journal_corrupt",
                    "package mutation journal terminal phase contradicts its "
                    "own recorded result",
                )
        # The prepared-evidence binding is meaningful only while the
        # operation is still preparable. Present at `intent`, absent
        # everywhere else -- anything else is a contradiction, not a
        # harmless leftover.
        has_digest = isinstance(record.get("prepared_evidence_digest"), str)
        if (phase == "intent") != has_digest or (
            record.get("prepared_evidence_digest") is not None and not has_digest
        ):
            raise MutationError(
                "journal_corrupt",
                "package mutation journal phase contradicts its prepared "
                "evidence binding",
            )
        return record

    def write(self, record: Mapping[str, Any]) -> None:
        """Atomically replace the journal and fsync data, entry, and directory."""

        self.ensure_directory()
        path = self._path(str(record["mutation_operation_id"]))
        temporary = path.with_name(path.name + ".tmp")
        payload = _canonical_json(dict(record)).encode("utf-8")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
        directory_descriptor = os.open(self._directory, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)


class VmidMutationLease:
    """Kernel `flock` serializing package mutation per VMID.

    Non-blocking on purpose: a lease held by someone else is EVIDENCE (an
    operation is in flight), never something to wait behind while a backend
    holds its own writer lock.

    The lease is also the liveness signal for the detached runner. The runner
    inherits this exact open file description across `fork`, so the lock
    stays held for its whole life; the parent therefore hands it over with
    :meth:`detach`, which closes only its own descriptor and deliberately
    never unlocks -- unlocking would release the lock for the runner too.
    """

    def __init__(self, vmid: int, directory: Path = JOURNAL_DIRECTORY) -> None:
        self._path = Path(directory) / f"vmid-{int(vmid)}.lock"
        self._descriptor: int | None = None

    @property
    def descriptor(self) -> int | None:
        return self._descriptor

    def __enter__(self) -> VmidMutationLease:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._descriptor = os.open(
            self._path, os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o600
        )
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(self._descriptor)
            self._descriptor = None
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise MutationError(
                    "operation_in_progress",
                    "another package mutation operation holds this guest's lease",
                ) from exc
            raise
        return self

    def detach(self) -> None:
        """Hand the lease to a forked runner: close, never unlock."""

        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None

    def __exit__(self, *_exc: object) -> None:
        if self._descriptor is not None:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            os.close(self._descriptor)
            self._descriptor = None


def lease_is_held(vmid: int, directory: Path) -> bool:
    """Probe whether some invocation currently holds this guest's lease."""

    try:
        with VmidMutationLease(vmid, directory):
            return False
    except MutationError as exc:
        if exc.classification == "operation_in_progress":
            return True
        raise


# ---------------------------------------------------------------------------
# Bounded PVE / guest commands
# ---------------------------------------------------------------------------


def _command(
    runner: Runner,
    argv: tuple[str, ...],
    *,
    max_output: int = MAX_COMMAND_OUTPUT_BYTES,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
    stdin: bytes = b"",
) -> CommandResult:
    result = runner(argv, timeout, max_output, stdin)
    if result.timed_out:
        raise MutationError("timeout", "package mutation command timed out")
    if result.output_exceeded:
        raise MutationError(
            "execution_failed", "package mutation command output exceeded its bound"
        )
    return result


def _local_node(runner: Runner) -> str:
    result = _command(
        runner,
        ("pvesh", "get", "/cluster/status", "--output-format", "json"),
        max_output=1 * 1024 * 1024,
    )
    if result.returncode != 0:
        raise MutationError(
            "execution_failed", "could not read local PVE cluster status"
        )
    try:
        rows = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MutationError(
            "execution_failed", "local PVE cluster status was malformed"
        ) from exc
    if not isinstance(rows, list):
        raise MutationError(
            "execution_failed", "local PVE cluster status was malformed"
        )
    local_nodes = [
        row.get("name")
        for row in rows
        if isinstance(row, Mapping)
        and row.get("type") == "node"
        and row.get("local") in (1, True)
    ]
    if (
        len(local_nodes) != 1
        or not isinstance(local_nodes[0], str)
        or not NODE_RE.fullmatch(local_nodes[0])
    ):
        raise MutationError("execution_failed", "local PVE node identity is ambiguous")
    return local_nodes[0]


def revalidate_live_target(runner: Runner, vmid: int, expected_node: str) -> None:
    """Re-read live PVE state immediately before anything that matters.

    A VMID is an execution locator, never durable identity. What PVE itself
    can prove -- that exactly one guest holds this VMID, that it is an LXC,
    that it is on the node this job froze, and that it is running -- is
    proven here every time. The backend's own `resource_id`, binding, and
    continuity revision are authority facts PVE does not know; they travel
    in the request context and are fenced by the journal's request
    fingerprint.
    """

    result = _command(
        runner,
        (
            "pvesh",
            "get",
            "/cluster/resources",
            "--type",
            "vm",
            "--output-format",
            "json",
        ),
        max_output=4 * 1024 * 1024,
    )
    if result.returncode != 0:
        raise MutationError(
            "execution_failed", "could not read current PVE target state"
        )
    try:
        rows = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise MutationError(
            "execution_failed", "current PVE target state was malformed"
        ) from exc
    if not isinstance(rows, list):
        raise MutationError(
            "execution_failed", "current PVE target state was malformed"
        )
    matches = [
        row for row in rows if isinstance(row, Mapping) and row.get("vmid") == vmid
    ]
    if len(matches) != 1:
        raise MutationError("guest_unavailable", "guest is missing or unavailable")
    row = matches[0]
    if row.get("type") != "lxc":
        raise MutationError(
            "unsupported_resource_type", "current PVE resource is not an LXC guest"
        )
    if row.get("node") != expected_node:
        raise MutationError("stale_target", "guest node changed after job issuance")
    if row.get("status") != "running":
        raise MutationError("guest_unavailable", "guest is not running")


def _run_guest_command(
    runner: Runner,
    vmid: int,
    expected_node: str,
    local_node: str,
    tail: tuple[str, ...],
    *,
    max_output: int = MAX_COMMAND_OUTPUT_BYTES,
    timeout: float = COMMAND_TIMEOUT_SECONDS,
    stdin: bytes = b"",
) -> CommandResult:
    """Run one fixed ``pct exec`` shape on whichever node currently holds it.

    Identical routing contract to the scan and execution helpers: ``tail`` is
    always one of this file's own fixed argv shapes, and a non-local guest is
    routed to its expected cluster member over root's existing passwordless
    inter-node SSH trust Proxmox itself provisions -- no new Hubinet
    credential on that node, and no request-provided or arbitrary text ever
    reaches either command.

    **This dispatcher owns the live-target invariant.** A VMID is an
    execution locator, not identity: PVE can free one and reuse it for an
    unrelated guest at any moment. So every single guest command -- the
    architecture read, the dpkg inventory reads on both sides of the
    mutation, the staging of the action gate, `apt-get update`, the
    simulation, and the one real package command -- is preceded here by its
    own fresh :func:`revalidate_live_target`. Callers cannot opt out and
    cannot amortize one check across two commands, which is what made a
    "validate once, then run several commands" caller able to send its
    second command to a replacement guest.

    :func:`revalidate_live_target` and :func:`_local_node` issue `pvesh`
    commands on the host through :func:`_command` directly, never through
    this dispatcher, so the invariant cannot recurse.
    """

    revalidate_live_target(runner, vmid, expected_node)
    inner = ("pct", "exec", str(vmid), "--", *tail)
    if expected_node == local_node:
        result = _command(
            runner, inner, max_output=max_output, timeout=timeout, stdin=stdin
        )
    else:
        argv = (
            "ssh",
            "-T",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            f"root@{expected_node}",
            shlex.join(inner),
        )
        result = _command(
            runner, argv, max_output=max_output, timeout=timeout, stdin=stdin
        )
    if result.returncode == 255:
        raise MutationError(
            "execution_failed", "could not execute package command in guest"
        )
    return result


def _decode(result: CommandResult) -> tuple[str, str]:
    try:
        return result.stdout.decode("utf-8"), result.stderr.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MutationError(
            "execution_failed", "package mutation command output was not UTF-8"
        ) from exc


def _package_failure(stage: str, stderr: str) -> MutationError:
    lowered = stderr.lower()
    if any(pattern in lowered for pattern in BUSY_PATTERNS):
        return MutationError("package_manager_busy", "APT or dpkg is busy")
    if stage == "metadata_refresh":
        return MutationError("metadata_refresh_failed", "APT metadata refresh failed")
    return MutationError("simulation_failed", "APT upgrade simulation failed")


def _parse_apt_version(text: str) -> tuple[int, int, int]:
    lines = text.splitlines()
    if not lines or not (match := APT_VERSION_RE.fullmatch(lines[0])):
        raise MutationError("execution_failed", "guest APT version output was malformed")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def parse_dpkg_inventory(text: str) -> tuple[dict[tuple[str, str], str], list[str]]:
    """Parse dpkg's own fixed TSV inventory into installed and unfinished rows.

    Deliberately NOT an APT parser: this reads dpkg's four fixed fields, and
    is used only to REFUSE a mutation whose starting state is not what the
    approved plan assumed. The authoritative material parsing -- and the
    completion proof -- stay in the backend's one canonical implementation,
    which re-derives everything from the raw inventory text this helper
    returns verbatim.
    """

    installed: dict[tuple[str, str], str] = {}
    unfinished: list[str] = []
    seen: set[tuple[str, str]] = set()
    for raw_line in text.splitlines():
        if not raw_line:
            continue
        fields = raw_line.split("\t")
        if len(fields) != 4:
            raise MutationError(
                "malformed_plan", "guest installed package inventory is malformed"
            )
        name, architecture, version, status = fields
        if not name or not ARCHITECTURE_RE.fullmatch(architecture):
            raise MutationError(
                "malformed_plan", "guest installed package inventory is malformed"
            )
        identity = (name, architecture)
        if identity in seen:
            raise MutationError(
                "malformed_plan",
                "guest installed package inventory contains a duplicate "
                "(package_name, architecture)",
            )
        seen.add(identity)
        if status in DPKG_UNFINISHED_STATUS_WORDS:
            unfinished.append(f"{name}:{architecture}")
            continue
        if status != "installed":
            continue
        installed[identity] = version
    return installed, unfinished


def _read_guest_state(
    runner: Runner, vmid: int, expected_node: str, local_node: str
) -> tuple[str, str]:
    """Read the guest's native architecture and complete dpkg inventory."""

    native = _run_guest_command(
        runner, vmid, expected_node, local_node,
        NATIVE_ARCHITECTURE_ARGV,
        max_output=4096,
    )
    native_architecture, _ = _decode(native)
    if native.returncode != 0:
        raise MutationError(
            "execution_failed", "could not determine guest native architecture"
        )
    inventory = _run_guest_command(
        runner, vmid, expected_node, local_node,
        INSTALLED_INVENTORY_ARGV,
        max_output=MAX_INVENTORY_OUTPUT_BYTES,
    )
    installed_inventory, _ = _decode(inventory)
    if inventory.returncode != 0:
        raise MutationError(
            "execution_failed", "could not read guest installed package inventory"
        )
    return native_architecture, installed_inventory


def stage_action_set_gate(
    runner: Runner,
    request: Mapping[str, Any],
    local_node: str,
) -> dict[str, str]:
    """Stage this operation's pre-dpkg action gate inside the guest.

    Runs strictly BEFORE the journal reaches `submitted`, so a staging
    failure is an ordinary pre-submission refusal: nothing was mutated, the
    operation stays at `intent`, and recovery can still seal it.

    Every step is a fixed argv shape; the only interpolated values are this
    file's own literal paths and the canonical operation UUID. **No package
    name or version is ever an argument.** The manifest and the verifier
    travel as structured payload BYTES on the command's stdin, never as
    command text, argv, or a shell fragment, so there is no value the
    approved material could take that changes what executes.

    Staleness is closed by construction. The whole Hubinet staging root is
    removed and this operation's directory recreated under this guest's
    per-VMID lease, so a crash leftover from any earlier operation is gone
    before anything can consult it; and even if one somehow survived, the
    manifest header and the verifier's own literal both name an operation
    id, so a foreign manifest is refused rather than obeyed.

    Finally the staged bytes are read back and their digests compared to
    what was sent. That is what makes the gate integrity-bound to *this*
    operation rather than to whatever happens to sit at the path.
    """

    mutation_operation_id = request["mutation_operation_id"]
    vmid = request["vmid"]
    expected_node = request["expected_node"]
    directory = guest_staging_directory(mutation_operation_id)
    manifest_path = guest_manifest_path(mutation_operation_id)
    verifier_path = guest_verifier_path(mutation_operation_id)
    manifest = build_expected_action_manifest(
        mutation_operation_id, request["expected_packages"]
    ).encode("utf-8")
    verifier = build_action_set_verifier(mutation_operation_id).encode("utf-8")

    def _step(tail: tuple[str, ...], failure: str, *, stdin: bytes = b"") -> str:
        result = _run_guest_command(
            runner,
            vmid,
            expected_node,
            local_node,
            tail,
            max_output=64 * 1024,
            stdin=stdin,
        )
        if result.returncode != 0:
            raise MutationError("execution_failed", failure)
        stdout, _ = _decode(result)
        return stdout

    _step(
        ("env", "LC_ALL=C", "rm", "-rf", GUEST_STAGING_ROOT),
        "could not clear the guest package-mutation staging root",
    )
    _step(
        ("env", "LC_ALL=C", "mkdir", "-p", "-m", "0700", directory),
        "could not create the guest package-mutation staging directory",
    )
    _step(
        ("env", "LC_ALL=C", "dd", f"of={manifest_path}", "status=none"),
        "could not stage the approved action manifest",
        stdin=manifest,
    )
    _step(
        ("env", "LC_ALL=C", "dd", f"of={verifier_path}", "status=none"),
        "could not stage the package-mutation action gate",
        stdin=verifier,
    )
    _step(
        ("env", "LC_ALL=C", "chmod", "0500", verifier_path),
        "could not make the package-mutation action gate executable",
    )

    observed = _step(
        ("env", "LC_ALL=C", "sha256sum", manifest_path, verifier_path),
        "could not verify the staged package-mutation action gate",
    )
    digests: dict[str, str] = {}
    for line in observed.splitlines():
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 2 or not FINGERPRINT_RE.fullmatch(fields[0]):
            raise MutationError(
                "execution_failed",
                "guest returned a malformed staged action gate digest",
            )
        digests[fields[1]] = fields[0]
    expected = {
        manifest_path: hashlib.sha256(manifest).hexdigest(),
        verifier_path: hashlib.sha256(verifier).hexdigest(),
    }
    if digests != expected:
        # The bytes at the paths the real command will consult are not the
        # bytes this operation produced. Refuse before submission rather
        # than run a mutation gated by something else.
        raise MutationError(
            "mutation_state_mismatch",
            "staged package-mutation action gate does not match this operation",
        )
    return expected


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------


def _response(
    request: Mapping[str, Any],
    state: str,
    *,
    running: bool = False,
    reason: str | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "response_version": 1,
        "ok": True,
        "context": dict(request["context"]),
        "mutation_operation_id": request["mutation_operation_id"],
        "operation_state": state,
        "running": running,
    }
    if reason is not None:
        payload["reason"] = reason[:500]
    if evidence is not None:
        payload["evidence"] = dict(evidence)
    return payload


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------


def _phase_state(record: dict[str, Any] | None) -> str:
    return "absent" if record is None else str(record["phase"])


def _require_matching_request(
    record: dict[str, Any] | None, fingerprint: str
) -> dict[str, Any] | None:
    if record is not None and record["request_fingerprint"] != fingerprint:
        raise MutationError(
            "operation_request_mismatch",
            "this operation identity was journaled with a different request",
        )
    return record


def _inspect(
    request: Mapping[str, Any], journal: OperationJournal
) -> dict[str, Any]:
    """Read the durable operation state. Never submits, never seals.

    Deliberately performs no PVE or guest I/O at all: recovering evidence
    about an operation that may already have mutated packages must work when
    the guest is unreachable, and must never depend on anything the guest
    could be doing right now.
    """

    fingerprint = request_fingerprint(request)
    record = _require_matching_request(
        journal.read(request["mutation_operation_id"]), fingerprint
    )
    state = _phase_state(record)
    running = state == "submitted" and lease_is_held(
        request["vmid"], journal.directory
    )
    evidence = None
    if record is not None and state in TERMINAL_PHASES:
        evidence = record["result"]
    return _response(
        request,
        state,
        running=running,
        reason=(
            "a package mutation is running right now"
            if running
            else None
        ),
        evidence=evidence,
    )


def _seal_never_submitted(
    request: Mapping[str, Any], journal: OperationJournal
) -> dict[str, Any]:
    """Durably forbid this exact operation from ever mutating packages.

    Performs no PVE or guest I/O, so a moved or unreachable guest never
    prevents the backend from proving that no mutation happened and
    releasing its one global destructive slot. Serialized against every
    submitter by the same non-blocking per-VMID lease: if a submitter took
    the lease first it durably reached `submitted` before launching
    anything, and this seal then refuses.

    `absent` and `intent` seal identically, and `absent` is the ordinary
    case: the backend arms BEFORE calling the host at all, so a backend that
    died between arming and executing leaves exactly this -- a durable
    write-ahead checkpoint with no host record. Holding the lease is what
    makes converting that absence into a fence sound rather than an
    inference: no submitter can be between its own `submitted` write and its
    runner launch while this call owns the lease.
    """

    fingerprint = request_fingerprint(request)
    with VmidMutationLease(request["vmid"], journal.directory):
        record = _require_matching_request(
            journal.read(request["mutation_operation_id"]), fingerprint
        )
        if record is None:
            journal.write(
                {
                    "journal_version": 1,
                    "mutation_operation_id": request["mutation_operation_id"],
                    "request_fingerprint": fingerprint,
                    "vmid": request["vmid"],
                    "expected_node": request["expected_node"],
                    "phase": "sealed_not_submitted",
                }
            )
        elif record["phase"] == "intent":
            sealed = {
                key: value
                for key, value in record.items()
                if key != "prepared_evidence_digest"
            }
            sealed["phase"] = "sealed_not_submitted"
            journal.write(sealed)
        elif record["phase"] != "sealed_not_submitted":
            # A mutation crossed the door first. Return the exact durable
            # phase so the backend stays fenced and recoverable, and never
            # treat this as a release proof.
            return _response(
                request,
                str(record["phase"]),
                # This invocation holds the lease, so no runner can be alive
                # for this guest right now; probing would only observe our
                # own lease.
                running=False,
                reason=(
                    "package mutation crossed submission before it could be sealed"
                ),
                evidence=(
                    record["result"]
                    if record["phase"] in TERMINAL_PHASES
                    else None
                ),
            )
        return _response(
            request,
            "sealed_not_submitted",
            reason="host durably sealed this package mutation before submission",
        )


def _prepare(
    runner: Runner, request: Mapping[str, Any], journal: OperationJournal
) -> dict[str, Any]:
    """Take the lease and produce the fresh execution-time evidence, read-only.

    This is the mutation stage's own execution-time proof material: a fixed
    APT metadata refresh, a fixed upgrade SIMULATION, and the two fixed
    read-only dpkg identity commands -- exactly the non-mutating contract
    `PRODUCT.md`, "What package scanning may do" already allows, and exactly
    the same commands the dark execution-plan gate uses. Nothing here can
    change a workload package.

    It is also PURELY read-only in the durable sense: it writes no journal
    record at all. Preparation happens BEFORE the backend's write-ahead
    arming transaction, so a durable record written here would be
    mutation-operation state created for an operation that may never be
    armed -- and, being immutable once written, would turn any ordinary
    pre-arm transient (a package scan still RUNNING, a lost PREPARE
    response, a backend that died before arming) into an operation identity
    that can never be prepared again without a backend restart. Preparation
    is therefore repeatable by construction: the durable journal's first
    record is created by `_execute`, the only path that may submit, from the
    digest the backend's arming transaction actually accepted.

    The evidence digest is still returned, and is still what binds the
    mutation to material the backend proved -- it simply becomes a durable
    host fact at the submit-capable boundary rather than before it.
    """

    fingerprint = request_fingerprint(request)
    vmid = request["vmid"]
    expected_node = request["expected_node"]
    with VmidMutationLease(vmid, journal.directory):
        record = _require_matching_request(
            journal.read(request["mutation_operation_id"]), fingerprint
        )
        if record is not None:
            # ANY durable record means this operation already reached the
            # submit-capable boundary: `intent` is written by `_execute`
            # after the backend armed, and every later phase follows it. So
            # this is not a repeatable pre-arm preparation any more --
            # preparing again would be meaningless and must never look like
            # permission to mutate.
            #
            # Reported as the durable phase with NO evidence rather than as
            # an error, so the backend can route it to the existing
            # pre-submission seal instead of leaving the job holding the one
            # global destructive slot with nothing able to resolve it. The
            # seal decision still belongs to this journal: `_seal_never_
            # submitted` refuses once the phase has moved past `intent`.
            return _response(
                request,
                str(record["phase"]),
                # We hold the lease, so no runner is alive for this guest.
                running=False,
                reason="package mutation operation is past preparation",
                evidence=(
                    record["result"] if record["phase"] in TERMINAL_PHASES else None
                ),
            )

        local_node = _local_node(runner)

        os_result = _run_guest_command(
            runner, vmid, expected_node, local_node,
            ("env", "LC_ALL=C", "cat", "/etc/os-release"),
            max_output=64 * 1024,
        )
        os_release, _ = _decode(os_result)
        if os_result.returncode != 0:
            raise MutationError(
                "guest_unavailable", "guest OS release metadata is unavailable"
            )

        apt_version_result = _run_guest_command(
            runner, vmid, expected_node, local_node,
            ("env", "LC_ALL=C", "apt-get", "--version"),
            max_output=64 * 1024,
        )
        apt_version_stdout, _ = _decode(apt_version_result)
        if apt_version_result.returncode != 0:
            raise MutationError(
                "execution_failed", "could not determine guest APT version"
            )
        if _parse_apt_version(apt_version_stdout) < MINIMUM_APT_VERSION:
            raise MutationError(
                "unsupported_os",
                "guest APT version does not support strict metadata refresh",
            )

        # Execution-time candidate state must be current, not a reuse of the
        # original scan's stale indexes. Writing APT's own index and cache
        # metadata is explicitly non-mutating for workload packages.
        update = _run_guest_command(
            runner, vmid, expected_node, local_node,
            (
                "env", "LC_ALL=C",
                "DEBIAN_FRONTEND=noninteractive", "apt-get", "update", "-qq",
                "--error-on=any",
            ),
        )
        _, update_stderr = _decode(update)
        if update.returncode != 0:
            raise _package_failure("metadata_refresh", update_stderr)

        # Simulation only (`-s`). This is the LAST metadata refresh before
        # the mutation: the real command deliberately never refreshes again,
        # so it resolves against exactly the index state this simulation was
        # computed from.
        simulation = _run_guest_command(
            runner, vmid, expected_node, local_node,
            ("env", "LC_ALL=C", "DEBIAN_FRONTEND=noninteractive", "apt-get", "-s", "upgrade"),
        )
        simulation_stdout, simulation_stderr = _decode(simulation)
        if simulation.returncode != 0:
            raise _package_failure("simulation", simulation_stderr)

        native_architecture, installed_inventory = _read_guest_state(
            runner, vmid, expected_node, local_node
        )

        evidence = {
            "os_release": os_release,
            "native_architecture": native_architecture,
            "installed_inventory": installed_inventory,
            "simulation_stdout": simulation_stdout,
        }
        digest = evidence_digest(evidence)
        # `absent` is the literal truth: this operation has no durable host
        # state, and preparing again is legal. Nothing here is a promise
        # that a mutation may happen -- only the backend's arming
        # transaction can make that decision.
        return _response(
            request,
            "absent",
            reason="fresh execution-time evidence prepared; no package changed",
            evidence={**evidence, "prepared_evidence_digest": digest},
        )


def _execute(
    runner: Runner, request: Mapping[str, Any], journal: OperationJournal
) -> dict[str, Any]:
    """Cross the submission boundary at most once, then detach the runner.

    This is also where this operation's durable host state BEGINS. The
    backend reaches here only after its write-ahead arming transaction
    committed `mutation_may_have_started` and re-proved that this caller
    carries the exact `accepted_prepared_evidence_digest` that transaction
    accepted, so the digest presented here is the accepted one by
    construction. The first thing this does, under the lease, is journal
    `intent` bound to that digest -- after which the binding is immutable
    and no later caller can substitute another, which is the host half of
    "only the accepted evidence may submit".

    Returns as soon as `submitted` is durable. It NEVER waits for the package
    command, so the backend may hold its one authority writer lock across
    this call without ever holding it across a package mutation.
    """

    fingerprint = request_fingerprint(request)
    vmid = request["vmid"]
    expected_node = request["expected_node"]
    lease = VmidMutationLease(vmid, journal.directory)
    with lease:
        record = _require_matching_request(
            journal.read(request["mutation_operation_id"]), fingerprint
        )
        if record is None:
            # The FIRST durable record for this operation identity, created
            # by the only path that may ever submit, from the digest the
            # backend's arming transaction accepted. Absence is the normal
            # state here: preparation is read-only and durable-free, so
            # nothing precedes this write.
            #
            # It is a separate fsynced record rather than a direct jump to
            # `submitted` because it binds the digest before any guest I/O:
            # every pre-flight fence below can refuse, and a crash in that
            # window leaves an operation whose accepted digest is already
            # frozen and which the existing seal still resolves.
            record = {
                "journal_version": 1,
                "mutation_operation_id": request["mutation_operation_id"],
                "request_fingerprint": fingerprint,
                "vmid": vmid,
                "expected_node": expected_node,
                "phase": "intent",
                "prepared_evidence_digest": request["prepared_evidence_digest"],
            }
            journal.write(record)
        phase = record["phase"]
        if phase == "sealed_not_submitted":
            # The durable seal is terminal with respect to submission, and is
            # obeyed before any PVE or guest read. A delayed request that
            # lost the race to a recovery seal must never mutate anything.
            return _response(
                request,
                "sealed_not_submitted",
                reason=(
                    "host durably sealed this package mutation before submission"
                ),
            )
        if phase in TERMINAL_PHASES:
            return _response(
                request,
                phase,
                reason="package mutation already reached a terminal result",
                evidence=record["result"],
            )
        if phase == "submitted":
            # The genuinely uncertain window. NEVER resubmit.
            return _response(
                request,
                "submitted",
                # We hold the lease, so the runner that wrote `submitted` is
                # gone without a terminal record: durably uncertain, and
                # still never resubmitted.
                running=False,
                reason=(
                    "package mutation was already submitted; inspect the "
                    "operation to observe its terminal result"
                ),
            )
        if phase != "intent":
            raise MutationError(
                "journal_corrupt",
                "package mutation journal is in an unrecognized phase",
            )
        if record.get("prepared_evidence_digest") != request[
            "prepared_evidence_digest"
        ]:
            # A pre-existing `intent` froze this operation's accepted digest.
            # A caller presenting a different one is never served it, no
            # matter how it obtained it.
            raise MutationError(
                "mutation_state_mismatch",
                "package mutation request does not carry the exact prepared "
                "evidence this operation journaled",
            )

        # -------------------------------------------------------------
        # PRE-SUBMISSION WINDOW
        #
        # The journal is at `intent`, and the fsynced `intent -> submitted`
        # write below happens strictly BEFORE the package command is
        # launched. Everything here either refuses without mutating, or
        # crosses that boundary exactly once.
        # -------------------------------------------------------------
        local_node = _local_node(runner)
        native_architecture, pre_inventory = _read_guest_state(
            runner, vmid, expected_node, local_node
        )
        installed, unfinished = parse_dpkg_inventory(pre_inventory)
        if unfinished:
            raise MutationError(
                "mutation_state_mismatch",
                "guest has unfinished dpkg package state; refusing to mutate "
                f"({len(unfinished)} package(s))",
            )
        drifted = [
            f"{package['package_name']}:{package['architecture']}"
            for package in request["expected_packages"]
            if installed.get(
                (package["package_name"], package["architecture"])
            )
            != package["installed_version"]
        ]
        if drifted:
            # The guest's installed state moved away from what the operator
            # approved, between the backend's proof and this instant. The
            # approved mutation is no longer the mutation that would happen,
            # so nothing is mutated.
            raise MutationError(
                "mutation_state_mismatch",
                "guest installed package state no longer matches the approved "
                f"plan for {len(drifted)} package(s)",
            )

        # The pre-dpkg action gate is staged while the journal is still at
        # `intent`, so a staging failure refuses cleanly and remains
        # sealable. It also means that if this VMID is reused by another
        # guest before the detached runner reaches `apt-get`, that guest
        # simply has no verifier at the hook path -- APT then fails the
        # hook and aborts before dpkg, independently of the runner's own
        # final target revalidation.
        stage_action_set_gate(runner, request, local_node)

        journal.write(
            {
                "journal_version": 1,
                "mutation_operation_id": request["mutation_operation_id"],
                "request_fingerprint": fingerprint,
                "vmid": vmid,
                "expected_node": expected_node,
                "phase": "submitted",
            }
        )
        _spawn_detached_runner(
            request,
            journal,
            lease,
            local_node=local_node,
            pre_native_architecture=native_architecture,
            pre_installed_inventory=pre_inventory,
            runner=runner,
        )
        return _response(
            request,
            "submitted",
            running=True,
            reason="package mutation submitted to a detached host runner",
        )


def _spawn_detached_runner(
    request: Mapping[str, Any],
    journal: OperationJournal,
    lease: VmidMutationLease,
    *,
    local_node: str,
    pre_native_architecture: str,
    pre_installed_inventory: str,
    runner: Runner,
) -> None:
    """Double-fork the runner that owns this mutation's fate.

    The grandchild is a session leader's child, reparented to PID 1, with
    stdio on `/dev/null`, so closing the SSH channel neither signals it nor
    waits on it. It inherits the per-VMID lease's open file description, so
    the lease stays held for exactly as long as the mutation runs and a
    concurrent invocation can observe "a mutation is running right now"
    without any PID bookkeeping.

    Correctness never depends on the runner surviving. If it is killed
    anyway (a host reboot, say), the journal simply stays at `submitted`
    with the lease free, which is durably UNCERTAIN and is never retried.
    """

    pid = os.fork()
    if pid > 0:
        # Parent: hand the lease over WITHOUT unlocking (the lock lives on
        # the open file description both processes now share), and reap the
        # intermediate child so no zombie is left on the PVE host.
        lease.detach()
        os.waitpid(pid, 0)
        return

    # Intermediate child.
    try:
        os.setsid()
        if os.fork() > 0:
            os._exit(0)
    except BaseException:
        os._exit(1)

    # Runner. From here on nothing may reach the SSH channel.
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull, 0)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        if devnull > 2:
            os.close(devnull)
        run_mutation(
            request,
            journal,
            local_node=local_node,
            pre_native_architecture=pre_native_architecture,
            pre_installed_inventory=pre_installed_inventory,
            runner=runner,
        )
    except BaseException:
        # A runner that cannot even journal its own outcome leaves the
        # operation at `submitted`: durably uncertain, never resubmitted.
        os._exit(1)
    os._exit(0)


def run_mutation(
    request: Mapping[str, Any],
    journal: OperationJournal,
    *,
    local_node: str,
    pre_native_architecture: str,
    pre_installed_inventory: str,
    runner: Runner = _run_bounded,
) -> dict[str, Any]:
    """Run the one real package command and journal exactly one terminal result.

    Called only by the detached runner, only from journal phase `submitted`,
    and only once per operation identity. It records the mutation's own exit
    evidence together with the independent dpkg readings from both sides of
    it, because an exit code alone can never prove the approved mutation
    completed.
    """

    vmid = request["vmid"]
    expected_node = request["expected_node"]
    timed_out = False
    exit_code = 1
    output_tail = ""
    try:
        # `_run_guest_command` revalidates the live target immediately
        # before dispatching, so this -- the last practical instant before
        # the one real package command -- is where a VMID that was freed
        # and reused after `submitted` was journaled is caught. If it
        # fails, `apt-get` is never launched and the MutationError below
        # journals a truthful terminal failure. It is deliberately NOT
        # sealed as never-submitted: `submitted` is already durable, so the
        # backend can no longer use the pre-submission release contract,
        # and this operation keeps its ownership and its fence.
        result = _run_guest_command(
            runner, vmid, expected_node, local_node,
            mutation_argv(request["mutation_operation_id"]),
            timeout=MUTATION_COMMAND_TIMEOUT_SECONDS,
        )
        exit_code = int(result.returncode)
        stdout, stderr = _decode(result)
        output_tail = (stdout + stderr)[-MAX_RETAINED_OUTPUT_BYTES:]
    except MutationError as exc:
        timed_out = exc.classification == "timeout"
        output_tail = exc.message[:MAX_RETAINED_OUTPUT_BYTES]
    except Exception as exc:  # noqa: BLE001 - an unlaunchable command is a failure
        # Recorded as a terminal FAILURE, never as uncertainty: a terminal
        # failure retains the backend's ownership, snapshot, and rollback
        # authority exactly like uncertainty does, and durably resolving the
        # operation is strictly better than leaving it in the `submitted`
        # window that nothing can ever move out of.
        output_tail = f"package mutation runner failed: {type(exc).__name__}"

    # Independent post-state, read even when the command failed: a failed
    # mutation may still have changed packages, and that evidence is exactly
    # what the later healthcheck/rollback stage needs.
    post_native_architecture = ""
    post_inventory = ""
    try:
        post_native_architecture, post_inventory = _read_guest_state(
            runner, vmid, expected_node, local_node
        )
    except MutationError:
        # Post-state unreadable. The terminal record still commits, with
        # empty post-state evidence, so the backend proves nothing and keeps
        # the job fenced rather than inferring success.
        pass

    result_evidence = {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "output_tail": output_tail,
        "pre_native_architecture": pre_native_architecture,
        "pre_installed_inventory": pre_installed_inventory,
        "post_native_architecture": post_native_architecture,
        "post_installed_inventory": post_inventory,
    }
    journal.write(
        {
            "journal_version": 1,
            "mutation_operation_id": request["mutation_operation_id"],
            "request_fingerprint": request_fingerprint(request),
            "vmid": vmid,
            "expected_node": expected_node,
            "phase": (
                "terminal_success"
                if _result_is_success(result_evidence)
                else "terminal_failure"
            ),
            "result": result_evidence,
        }
    )
    return journal.read(request["mutation_operation_id"]) or {}


def handle_request(
    payload: Any,
    *,
    runner: Runner = _run_bounded,
    journal: OperationJournal | None = None,
) -> dict[str, Any]:
    request = validate_request(payload)
    operation_journal = journal if journal is not None else OperationJournal()
    context = request["context"]
    try:
        if request["operation"] == "inspect_package_mutation_state":
            return _inspect(request, operation_journal)
        if request["operation"] == "seal_mutation_never_submitted":
            return _seal_never_submitted(request, operation_journal)
        if request["operation"] == "prepare_exact_package_mutation":
            return _prepare(runner, request, operation_journal)
        if request["operation"] == "execute_exact_package_mutation":
            return _execute(runner, request, operation_journal)
        raise MutationError("execution_failed", "unknown host-control operation")
    except MutationError as exc:
        return {
            "response_version": 1,
            "ok": False,
            "context": dict(context),
            "mutation_operation_id": request["mutation_operation_id"],
            "error": {
                "classification": exc.classification,
                "message": exc.message[:500],
            },
        }


def main() -> int:
    if os.environ.get("SSH_ORIGINAL_COMMAND"):
        response = {
            "response_version": 1,
            "ok": False,
            "context": {},
            "error": {
                "classification": "execution_failed",
                "message": "remote command text is not accepted",
            },
        }
        sys.stdout.write(json.dumps(response, separators=(",", ":")))
        return 2
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        error = "request exceeded its structural bound"
    else:
        try:
            payload = json.loads(raw.decode("utf-8"))
            response = handle_request(payload)
            sys.stdout.write(
                json.dumps(response, ensure_ascii=True, separators=(",", ":"))
            )
            sys.stdout.flush()
            return 0 if response.get("ok") is True else 1
        except (UnicodeDecodeError, ValueError, RequestError) as exc:
            error = str(exc)[:500] or "malformed package-mutation request"
    response = {
        "response_version": 1,
        "ok": False,
        "context": {},
        "error": {"classification": "execution_failed", "message": error},
    }
    sys.stdout.write(json.dumps(response, ensure_ascii=True, separators=(",", ":")))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
