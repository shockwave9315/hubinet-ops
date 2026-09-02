"""Deterministic job-owned package mutation operation identity.

One package update job causes AT MOST ONE real workload package mutation.
That operation's identity is derived here, purely, from immutable job
identity, so the exact same job derives the exact same operation id after any
restart or retry -- which is what lets a recovering backend reattach to the
host's durable at-most-once journal instead of guessing whether `apt-get`
already ran.

This is the mutation analogue of `snapshot_identity.py`, and deliberately a
separate, narrower module: a mutation operation has no PVE-visible name, no
description, and no ownership metadata carried by a PVE object. Its whole
identity is the operation id, and the host binds it to the exact request
material (`plan_fingerprint`) rather than to anything the guest could be
made to claim about itself.

The bound facts are exactly the ones `AGENTS.md` and `ARCHITECTURE.md`
require an operation to belong to:

- ``backend_instance_id`` -- this Hubinet installation;
- ``job_id`` -- this one update job;
- ``resource_id`` -- this durable resource identity, not a VMID;
- ``resource_continuity_revision`` -- this exact incarnation of it.

A reused VMID, a replaced guest, or a second job for the same resource
therefore derives a different operation id and can never inherit an existing
mutation operation. The VMID, node, binding, and locator generation are
deliberately NOT inputs: they are execution locators that may legitimately
change while the same job's identity does not, and they are fenced
separately, per request, by the host boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from typing import Any
import uuid

from .models import PackageUpdateMutationIdentity


class MutationIdentityError(ValueError):
    """A package mutation identity input is not usable."""


#: Domain-separated from every other digest in this repository, so a
#: mutation operation id can never collide with, or be mistaken for, a
#: snapshot operation id derived from the exact same job facts.
_OPERATION_DOMAIN = b"hubinet-ops/package-update/package-mutation-operation-id/v1"


def _canonical_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise MutationIdentityError(f"{field} must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise MutationIdentityError(f"{field} must be a canonical UUID") from exc
    if parsed.int == 0 or str(parsed) != value:
        raise MutationIdentityError(
            f"{field} must be a canonical lowercase hyphenated non-NIL UUID"
        )
    return value


def _positive_integer(value: Any, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise MutationIdentityError(f"{field} must be a positive integer")
    return value


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        dict(payload), ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")


def derive_package_mutation_identity(
    *,
    backend_instance_id: str,
    job_id: str,
    resource_id: str,
    resource_continuity_revision: int,
) -> PackageUpdateMutationIdentity:
    """Derive one job's deterministic package mutation operation identity.

    Every input is an immutable authority fact, so this is stable across
    restarts and retries and never depends on the current time, on a VMID,
    or on anything a caller may choose. There is deliberately no way to
    supply an operation id: the host boundary refuses any request whose id
    it cannot bind to the exact material fingerprint it already journaled.
    """

    payload = _canonical_json(
        {
            "backend_instance_id": _canonical_uuid(
                backend_instance_id, "backend_instance_id"
            ),
            "job_id": _canonical_uuid(job_id, "job_id"),
            "resource_id": _canonical_uuid(resource_id, "resource_id"),
            "resource_continuity_revision": _positive_integer(
                resource_continuity_revision, "resource_continuity_revision"
            ),
        }
    )
    digest = hashlib.sha256(_OPERATION_DOMAIN + b"\n" + payload).digest()
    operation_id = str(uuid.UUID(bytes=digest[:16], version=5))
    return PackageUpdateMutationIdentity(
        mutation_operation_id=_canonical_uuid(
            operation_id, "mutation_operation_id"
        )
    )
