from __future__ import annotations

import hmac
import re
from dataclasses import dataclass
from typing import Any, Mapping

EXECUTOR_VERSION = "0.4.3"
EXECUTOR_PROTOCOL_VERSION = 1
REQUIRED_APT_ACTIONS = frozenset(
    {
        "capabilities",
        "inspect",
        "check-updates",
        "preflight",
        "update",
        "healthcheck",
        "repair",
        "verify",
    }
)
JOB_OPERATION_TYPES = frozenset(
    {
        "update",
        "lifecycle_start",
        "lifecycle_shutdown",
        "lifecycle_reboot",
        "lifecycle_force_stop",
        "snapshot_create",
        "snapshot_create_ram",
        "snapshot_rollback",
        "snapshot_delete",
        "snapshot_prune",
        "retry_healthcheck",
        "self_update",
        "ct110_system_update",
    }
)
SNAPSHOT_KINDS = frozenset({"pre-update", "manual"})
SNAPSHOT_NAME_RE = re.compile(
    r"^hubinet-ops-(?P<vmid>[1-9][0-9]{1,5})-"
    r"(?P<kind>pre-update|pre|manual|man)-(?P<timestamp>[0-9]{8}T[0-9]{6}Z)$"
)
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
SNAPSHOT_PRUNE_STATE_VERSION = 3
SNAPSHOT_PRUNE_DELETED_HISTORY_LIMIT = 50


@dataclass(frozen=True)
class ExecutorCompatibility:
    version: str
    protocol_version: int | None
    compatible: bool
    executor_sha256: str
    profile_sha256: str
    missing_actions: tuple[str, ...]
    profile_validation_status: str
    reasons: tuple[str, ...]

    def state_fields(self) -> dict[str, Any]:
        return {
            "executor_version": self.version or None,
            "executor_protocol_version": self.protocol_version,
            "executor_compatible": self.compatible,
            "executor_sha256": self.executor_sha256 or None,
            "executor_profile_sha256": self.profile_sha256 or None,
            "executor_missing_actions": list(self.missing_actions),
            "profile_validation_status": self.profile_validation_status,
        }


def evaluate_executor_contract(
    payload: Mapping[str, Any],
    *,
    expected_executor_sha256: str,
    expected_profile_sha256: str,
) -> ExecutorCompatibility:
    version = str(payload.get("version") or "")
    try:
        protocol = int(payload.get("protocol_version"))
    except (TypeError, ValueError):
        protocol = None
    raw_actions = payload.get("supported_actions")
    actions = {
        str(action)
        for action in raw_actions
        if isinstance(action, str)
    } if isinstance(raw_actions, list) else set()
    missing = tuple(sorted(REQUIRED_APT_ACTIONS - actions))
    executor_hash = str(payload.get("executor_sha256") or "").lower()
    profile_hash = str(payload.get("profile_sha256") or "").lower()
    profile_status = str(payload.get("profile_validation_status") or "invalid")
    reasons: list[str] = []
    if version != EXECUTOR_VERSION:
        reasons.append(f"version {version or 'unknown'} != {EXECUTOR_VERSION}")
    if protocol != EXECUTOR_PROTOCOL_VERSION:
        reasons.append(
            f"protocol {protocol if protocol is not None else 'unknown'} != "
            f"{EXECUTOR_PROTOCOL_VERSION}"
        )
    if missing:
        reasons.append(f"missing actions: {', '.join(missing)}")
    if not _matching_sha256(executor_hash, expected_executor_sha256):
        reasons.append("executor sha256 mismatch")
    if not _matching_sha256(profile_hash, expected_profile_sha256):
        reasons.append("profile sha256 mismatch")
    if profile_status not in {"valid", "insufficient_health_contract"}:
        reasons.append(f"profile validation is {profile_status}")
    return ExecutorCompatibility(
        version=version,
        protocol_version=protocol,
        compatible=not reasons,
        executor_sha256=executor_hash,
        profile_sha256=profile_hash,
        missing_actions=missing,
        profile_validation_status=profile_status,
        reasons=tuple(reasons),
    )


def parse_owned_snapshot_name(name: str, *, vmid: int | None = None) -> dict[str, str] | None:
    match = SNAPSHOT_NAME_RE.fullmatch(str(name))
    if not match:
        return None
    values = match.groupdict()
    if vmid is not None and int(values["vmid"]) != int(vmid):
        return None
    values["kind"] = {
        "pre": "pre-update",
        "man": "manual",
    }.get(values["kind"], values["kind"])
    return values


def _matching_sha256(actual: str, expected: str) -> bool:
    normalized = str(expected or "").lower()
    return bool(
        SHA256_RE.fullmatch(actual)
        and SHA256_RE.fullmatch(normalized)
        and hmac.compare_digest(actual, normalized)
    )
