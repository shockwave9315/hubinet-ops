from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

HA_STATE_MAX_LENGTH = 255


@dataclass(frozen=True)
class EntitySpec:
    """One stable Home Assistant MQTT entity contract."""

    key: str
    suffix: str
    name: str
    value_template: str
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResourceIdentity:
    """Canonical resource identity used by the Home Assistant contract."""

    resource_type: str
    adapter: str


def bounded_ha_state_text(value: Any, fallback: str) -> str:
    """Apply Home Assistant's state limit without changing the source payload."""

    selected = fallback if value is None or value == "" else value
    return str(selected)[:HA_STATE_MAX_LENGTH]


def _text_template(path: str, fallback: str, *, empty_is_missing: bool = True) -> str:
    default = f"default('{fallback}', true)" if empty_is_missing else f"default('{fallback}')"
    return (
        "{{ (("
        f"{path} | {default}"
        f") | string)[:{HA_STATE_MAX_LENGTH}] }}}}"
    )


def _text(
    key: str,
    suffix: str,
    name: str,
    path: str,
    fallback: str,
    extra: Mapping[str, Any] | None = None,
    *,
    empty_is_missing: bool = True,
) -> EntitySpec:
    return EntitySpec(
        key,
        suffix,
        name,
        _text_template(path, fallback, empty_is_missing=empty_is_missing),
        extra or {},
    )


def _numeric(
    key: str,
    suffix: str,
    name: str,
    path: str,
    unit: str,
) -> EntitySpec:
    return EntitySpec(
        key,
        suffix,
        name,
        f"{{{{ {path} | default(none) }}}}",
        {"unit_of_measurement": unit},
    )


def _byte_numeric(
    key: str,
    suffix: str,
    name: str,
    path: str,
    *,
    state_class: str = "measurement",
) -> EntitySpec:
    return EntitySpec(
        key,
        suffix,
        name,
        (
            "{{ none if "
            f"{path} is not defined or {path} is none "
            f"else ({path} | int) }}}}"
        ),
        {
            "device_class": "data_size",
            "unit_of_measurement": "B",
            "state_class": state_class,
        },
    )


def _timestamp(key: str, suffix: str, name: str, path: str) -> EntitySpec:
    return _text(
        key,
        suffix,
        name,
        path,
        "unknown",
        {"entity_category": "diagnostic"},
    )


def _capability(key: str) -> EntitySpec:
    return EntitySpec(
        f"capability_{key}",
        f"capability_{key}",
        f"Capability {key.replace('_', ' ')}",
        (
            "{{ 'allowed' if "
            f"(value_json.operator_capabilities | default({{}})).get('{key}', false) "
            "else 'blocked' }}"
        ),
    )


SNAPSHOT_ENTITY_SPECS = (
    EntitySpec("snapshot_count", "snapshot_count", "Snapshot count", "{{ value_json.snapshot_count | default(0) }}"),
    EntitySpec("snapshot_unproven_count", "snapshot_unproven_count", "Unproven host-owned snapshot count", "{{ value_json.snapshot_unproven_count | default(0) }}"),
    _text("latest_unproven_snapshot_name", "latest_unproven_snapshot_name", "Latest unproven host-owned snapshot", "value_json.latest_unproven_snapshot_name", "none"),
    _text("latest_snapshot_name", "latest_snapshot_name", "Latest snapshot name", "value_json.latest_snapshot_name", "none"),
    _timestamp("latest_snapshot_at", "latest_snapshot_at", "Latest snapshot at", "value_json.latest_snapshot_at"),
    _text("latest_snapshot_kind", "latest_snapshot_kind", "Latest snapshot kind", "value_json.latest_snapshot_kind", "none"),
    EntitySpec("snapshot_operation_status", "snapshot_operation_status", "Snapshot operation status", "{{ value_json.snapshot_operation_status | default('idle') }}"),
    EntitySpec("snapshot_restore_allowed", "snapshot_restore_allowed", "Snapshot restore allowed", "{{ 'allowed' if value_json.snapshot_restore_allowed else 'blocked' }}"),
)

COMMON_RESOURCE_ENTITY_SPECS = (
    _text(
        "dashboard_path",
        "dashboard_path",
        "Dashboard path",
        "value_json.dashboard_path",
        "none",
    ),
)


LIFECYCLE_CAPABILITY_SPECS = tuple(
    _capability(key)
    for key in (
        "start", "shutdown", "reboot", "force_stop", "refresh",
        "snapshot_create", "snapshot_list", "snapshot_rollback", "snapshot_delete",
        "self_update",
    )
)


AGENT_ENTITY_SPECS = (
    EntitySpec("availability", "availability", "Availability", "{{ value }}"),
    _text("version", "version", "Version", "value_json.version", "unknown"),
    EntitySpec(
        "configured_container_count",
        "configured_container_count",
        "Configured container count",
        "{{ value_json.configured_container_count | default(none) }}",
    ),
    EntitySpec(
        "configured_resource_count",
        "configured_resource_count",
        "Configured resource count",
        "{{ value_json.configured_resource_count | default(none) }}",
    ),
    EntitySpec(
        "configured_lxc_count",
        "configured_lxc_count",
        "Configured LXC count",
        "{{ value_json.configured_lxc_count | default(none) }}",
    ),
    EntitySpec(
        "configured_qemu_count",
        "configured_qemu_count",
        "Configured QEMU count",
        "{{ value_json.configured_qemu_count | default(none) }}",
    ),
    EntitySpec(
        "active_job_count",
        "active_job_count",
        "Active job count",
        "{{ value_json.active_job_count | default(none) }}",
    ),
    EntitySpec(
        "last_refresh",
        "last_refresh",
        "Last refresh",
        "{{ value_json.last_refresh | default('unknown', true) }}",
        {"entity_category": "diagnostic"},
    ),
)


APT_ENTITY_SPECS = (
    EntitySpec("health_status", "health_status", "Health status", "{{ value_json.health_status }}"),
    _numeric("health_score", "health_score", "Health score", "value_json.health_score", "%"),
    EntitySpec("lxc_status", "lxc_status", "LXC status", "{{ value_json.lxc_status | default('unknown') }}"),
    EntitySpec("runtime_status", "runtime_status", "Runtime status", "{{ value_json.runtime_status | default('unknown') }}"),
    _numeric("uptime_seconds", "uptime", "Uptime", "value_json.uptime_seconds", "s"),
    EntitySpec("update_status", "update_status", "Update status", "{{ value_json.update_status }}"),
    EntitySpec("operation_status", "operation_status", "Operation status", "{{ value_json.operation_status }}"),
    EntitySpec("job_stage", "job_stage", "Job stage", "{{ value_json.job_stage }}"),
    _numeric("job_progress", "job_progress", "Job progress", "value_json.job_progress", "%"),
    EntitySpec("pending_updates", "pending_update_count", "Pending update count", "{{ value_json.pending_updates | default(none) }}"),
    EntitySpec("risk", "risk", "Risk", "{{ value_json.risk }}"),
    _numeric("disk_used_percent", "disk_used", "Disk used", "value_json.disk.used_percent", "%"),
    _numeric("disk_free_mb", "disk_free", "Disk free", "value_json.disk.free_mb", "MiB"),
    _numeric("memory_used_percent", "memory_used", "Memory used", "value_json.memory.used_percent", "%"),
    EntitySpec("docker_required_healthy", "docker_required_healthy", "Docker required healthy", "{{ value_json.docker.required_healthy | default(none) }}"),
    EntitySpec("docker_required_total", "docker_required_total", "Docker required total", "{{ value_json.docker.required_total | default(none) }}"),
    _text("active_plan_id", "active_plan_id", "Active plan ID", "value_json.active_plan_id", "none"),
    _text("active_plan_status", "active_plan_status", "Active plan status", "value_json.active_plan_status", "none"),
    _text("active_job_id", "active_job_id", "Active job ID", "value_json.active_job_id", "none"),
    _text("last_job_id", "last_job_id", "Last job ID", "value_json.last_job_id", "none"),
    _text("operation_type", "operation_type", "Operation type", "value_json.operation_type", "none"),
    _timestamp("last_scan", "last_scan", "Last scan", "value_json.last_scan"),
    _timestamp("last_refresh", "last_refresh", "Last refresh", "value_json.last_refresh"),
    _timestamp("last_update", "last_update", "Last update", "value_json.last_update"),
    _timestamp("last_terminal_at", "last_terminal_at", "Last terminal operation", "value_json.last_terminal_at"),
    _text("last_error", "last_error", "Last error", "value_json.last_error", "none"),
    _text("last_operation_result", "last_operation_result", "Last operation result", "value_json.last_operation_result", "none"),
    EntitySpec("rollback_allowed", "rollback_allowed", "Rollback allowed", "{{ 'allowed' if value_json.rollback_allowed else 'blocked' }}"),
    _text("last_job_event", "last_job_event", "Last job event", "value_json.last_job_event.message", "none", empty_is_missing=False),
    EntitySpec("lifecycle_status", "lifecycle_status", "Lifecycle status", "{{ value_json.lifecycle_status | default('idle') }}"),
    _text("lifecycle_action", "lifecycle_action", "Lifecycle action", "value_json.lifecycle_action", "none"),
    _text("lifecycle_error", "lifecycle_error", "Lifecycle error", "value_json.lifecycle_error", "none"),
    _timestamp("lifecycle_started_at", "lifecycle_started_at", "Lifecycle started at", "value_json.lifecycle_started_at"),
    _timestamp("lifecycle_finished_at", "lifecycle_finished_at", "Lifecycle finished at", "value_json.lifecycle_finished_at"),
    EntitySpec("verification_status", "verification_status", "Verification status", "{{ value_json.verification_status | default('unknown') }}"),
    _text("verification_error", "verification_error", "Verification error", "value_json.verification_error", "none"),
    _timestamp("last_verification", "last_verification", "Last verification", "value_json.last_verification"),
    EntitySpec("apt_check_ok", "apt_check", "APT check", "{{ 'unknown' if value_json.apt_check_ok is none else 'ok' if value_json.apt_check_ok else 'failed' }}"),
    EntitySpec("dpkg_audit_ok", "dpkg_audit", "dpkg audit", "{{ 'unknown' if value_json.dpkg_audit_ok is none else 'ok' if value_json.dpkg_audit_ok else 'failed' }}"),
    EntitySpec("reboot_required", "reboot_required", "Reboot required", "{{ 'unknown' if value_json.reboot_required is none else 'yes' if value_json.reboot_required else 'no' }}"),
    EntitySpec("packages_remaining_count", "packages_remaining", "Packages remaining", "{{ value_json.packages_remaining_count | default(none) }}"),
    EntitySpec("recovery_scan_status", "recovery_scan_status", "Recovery scan status", "{{ value_json.recovery_scan_status | default('disabled') }}"),
    _timestamp("last_recovery_scan", "last_recovery_scan", "Last recovery scan", "value_json.last_recovery_scan"),
    _text("last_recovery_scan_result", "last_recovery_scan_result", "Last recovery scan result", "value_json.last_recovery_scan_result", "none"),
    _capability("scan"),
    _capability("approve"),
    _capability("reject"),
    _capability("retry_healthcheck"),
    _capability("rollback"),
    _text("executor_version", "executor_version", "Executor version", "value_json.executor_version", "unknown", {"entity_category": "diagnostic"}),
    EntitySpec("executor_protocol_version", "executor_protocol_version", "Executor protocol", "{{ value_json.executor_protocol_version | default(none) }}", {"entity_category": "diagnostic"}),
    EntitySpec("executor_compatible", "executor_compatible", "Executor compatible", "{{ 'compatible' if value_json.executor_compatible else 'incompatible' }}", {"entity_category": "diagnostic"}),
    _text("executor_sha256", "executor_sha256", "Executor SHA-256", "value_json.executor_sha256", "unknown", {"entity_category": "diagnostic"}),
    _text("executor_profile_sha256", "executor_profile_sha256", "Executor profile SHA-256", "value_json.executor_profile_sha256", "unknown", {"entity_category": "diagnostic"}),
    EntitySpec("executor_missing_actions", "executor_missing_actions", "Executor missing actions", f"{{{{ (((value_json.executor_missing_actions | default([]) | join(', ')) or 'none') | string)[:{HA_STATE_MAX_LENGTH}] }}}}", {"entity_category": "diagnostic"}),
    _text("profile_validation_status", "profile_validation_status", "Profile validation", "value_json.profile_validation_status", "unknown", {"entity_category": "diagnostic"}),
    _text("executor_contract_error", "executor_contract_error", "Executor contract error", "value_json.executor_contract_error", "none", {"entity_category": "diagnostic"}),
    _timestamp("executor_last_checked_at", "executor_last_checked_at", "Executor last checked", "value_json.executor_last_checked_at"),
) + LIFECYCLE_CAPABILITY_SPECS + SNAPSHOT_ENTITY_SPECS


QEMU_ENTITY_SPECS = (
    EntitySpec("health_status", "health_status", "Health status", "{{ value_json.health_status }}"),
    _numeric("health_score", "health_score", "Health score", "value_json.health_score", "%"),
    EntitySpec("runtime_status", "runtime_status", "Runtime status", "{{ value_json.runtime_status | default('unknown') }}"),
    EntitySpec("qemu_status", "qemu_status", "QEMU status", "{{ value_json.qemu_status | default('unknown') }}"),
    _numeric("uptime_seconds", "uptime", "Uptime", "value_json.uptime_seconds", "s"),
    _numeric("cpu_usage", "cpu_usage", "CPU usage", "value_json.cpu.usage_percent", "%"),
    EntitySpec("cpu_cores", "cpu_cores", "CPU cores", "{{ value_json.cpu.cores | default(none) }}"),
    _byte_numeric("memory_used_bytes", "memory_used", "Memory used", "value_json.memory.used_bytes"),
    _byte_numeric("memory_total_bytes", "memory_total", "Memory total", "value_json.memory.total_bytes"),
    _byte_numeric("disk_used_bytes", "disk_used", "Disk used", "value_json.disk.used_bytes"),
    _byte_numeric("disk_total_bytes", "disk_total", "Disk total", "value_json.disk.total_bytes"),
    _byte_numeric("network_in_bytes", "network_received", "Network received", "value_json.network.in_bytes", state_class="total_increasing"),
    _byte_numeric("network_out_bytes", "network_sent", "Network sent", "value_json.network.out_bytes", state_class="total_increasing"),
    _text("guest_agent_status", "guest_agent", "Guest Agent", "value_json.guest_agent_status", "unknown", empty_is_missing=False),
    _text("ip_addresses", "ip_addresses", "Primary IP", "value_json.primary_ip_address", "unknown"),
    _timestamp("last_refresh", "last_refresh", "Last refresh", "value_json.last_refresh"),
    _text("last_error", "last_error", "Last error", "value_json.last_error", "none"),
) + tuple(
    _capability(key)
    for key in ("snapshot_create", "snapshot_list", "snapshot_delete")
) + SNAPSHOT_ENTITY_SPECS


AGENT_SELF_ENTITY_SPECS = (
    EntitySpec("health_status", "health_status", "Health status", "{{ value_json.health_status }}"),
    _numeric("health_score", "health_score", "Health score", "value_json.health_score", "%"),
    EntitySpec("runtime_status", "runtime_status", "Runtime status", "{{ value_json.runtime_status | default('unknown') }}"),
    EntitySpec("lxc_status", "lxc_status", "LXC status", "{{ value_json.lxc_status | default('unknown') }}"),
    _numeric("uptime_seconds", "uptime", "Uptime", "value_json.uptime_seconds", "s"),
    EntitySpec("cpu_cores", "cpu_cores", "CPU cores", "{{ value_json.cpu.cores | default(none) }}"),
    EntitySpec("cpu_load_1m", "cpu_load_1m", "CPU load 1m", "{{ value_json.cpu.load_1m | default(none) }}"),
    _byte_numeric("memory_used_bytes", "memory_used", "Memory used", "value_json.memory.used_bytes"),
    _byte_numeric("memory_total_bytes", "memory_total", "Memory total", "value_json.memory.total_bytes"),
    _byte_numeric("memory_available_bytes", "memory_available", "Memory available", "value_json.memory.available_bytes"),
    _byte_numeric("disk_used_bytes", "disk_used", "Disk used", "value_json.disk.used_bytes"),
    _byte_numeric("disk_total_bytes", "disk_total", "Disk total", "value_json.disk.total_bytes"),
    _byte_numeric("disk_free_bytes", "disk_free", "Disk free", "value_json.disk.free_bytes"),
    _text("service_status", "service_status", "Service status", "value_json.service_status", "unknown", empty_is_missing=False),
    _text("api_health", "api_health", "API health", "value_json.api_health", "unknown", empty_is_missing=False),
    _text("agent_version", "agent_version", "Agent version", "value_json.agent_version", "unknown", empty_is_missing=False),
    EntitySpec("recent_warnings", "recent_warnings", "Recent warnings", "{{ value_json.recent_warnings | default([]) | count }}"),
    _timestamp("last_refresh", "last_refresh", "Last refresh", "value_json.last_refresh"),
    _text("last_error", "last_error", "Last error", "value_json.last_error", "none"),
    EntitySpec("operation_status", "operation_status", "Operation status", "{{ value_json.operation_status | default('idle') }}"),
    _text("operation_type", "operation_type", "Operation type", "value_json.operation_type", "none"),
    _text("last_operation_result", "last_operation_result", "Last operation result", "value_json.last_operation_result", "none"),
    _timestamp("last_terminal_at", "last_terminal_at", "Last terminal operation", "value_json.last_terminal_at"),
    _text("active_plan_id", "active_plan_id", "Active plan ID", "value_json.active_plan_id", "none"),
    _text("active_plan_status", "active_plan_status", "Active plan status", "value_json.active_plan_status", "none"),
    _text("self_update_release_id", "self_update_release_id", "Self-update release ID", "value_json.self_update_release_id", "none"),
    _text("self_update_release_version", "self_update_release_version", "Self-update release version", "value_json.self_update_release_version", "none"),
    _text("self_update_release_fingerprint", "self_update_release_fingerprint", "Self-update release fingerprint", "value_json.self_update_release_fingerprint", "none"),
    _text("system_update_status", "system_update_status", "CT110 system update status", "value_json.system_update_status", "unknown"),
    _numeric("system_pending_updates", "system_pending_updates", "CT110 pending system packages", "value_json.system_pending_updates", "packages"),
    _numeric("system_security_updates", "system_security_updates", "CT110 security updates", "value_json.system_security_updates", "packages"),
    _text("system_package_names", "system_package_names", "CT110 pending package names", "value_json.system_package_names", "none", {"entity_category": "diagnostic"}),
    _timestamp("system_last_scan", "system_last_scan", "CT110 system last scan", "value_json.system_last_scan"),
    _timestamp("system_last_update", "system_last_update", "CT110 system last update", "value_json.system_last_update"),
    _timestamp("system_last_verification", "system_last_verification", "CT110 system last verification", "value_json.system_last_verification"),
    _text("system_active_plan_status", "system_active_plan_status", "CT110 system plan status", "value_json.system_active_plan_status", "none"),
    _text("system_apt_check_ok", "system_apt_check_ok", "CT110 apt check", "value_json.system_apt_check_ok", "unknown"),
    _text("system_dpkg_audit_ok", "system_dpkg_audit_ok", "CT110 dpkg audit", "value_json.system_dpkg_audit_ok", "unknown"),
    _text("system_reboot_required", "system_reboot_required", "CT110 reboot required", "value_json.system_reboot_required", "unknown"),
    _text("system_last_error", "system_last_error", "CT110 system update error", "value_json.system_last_error", "none"),
    _text("application_release_check_status", "application_release_check_status", "Application release check", "value_json.application_release_check_status", "unknown"),
    _text("application_current_version", "application_current_version", "Installed Hubinet Ops version", "value_json.application_current_version", "unknown"),
    _text("application_latest_version", "application_latest_version", "Latest Hubinet Ops version", "value_json.application_latest_version", "none"),
    _text("application_release_tag", "application_release_tag", "Application release tag", "value_json.application_release_tag", "none"),
    _text("application_release_commit", "application_release_commit", "Application release commit", "value_json.application_release_commit", "none"),
    _timestamp("application_release_published_at", "application_release_published_at", "Application release published", "value_json.application_release_published_at"),
    _text("application_download_status", "application_download_status", "Application download status", "value_json.application_download_status", "not_started"),
    _text("application_validation_status", "application_validation_status", "Application validation status", "value_json.application_validation_status", "unknown"),
    _text("application_deployment_status", "application_deployment_status", "Application deployment status", "value_json.application_deployment_status", "idle"),
    _timestamp("application_last_check", "application_last_check", "Application last release check", "value_json.application_last_check"),
    _timestamp("application_last_deployment", "application_last_deployment", "Application last deployment", "value_json.application_last_deployment"),
    _text("application_last_result", "application_last_result", "Application last result", "value_json.application_last_result", "none"),
    _text("application_last_error", "application_last_error", "Application release error", "value_json.application_last_error", "none"),
    _capability("scan"),
    _capability("approve"),
    _capability("reject"),
    _capability("self_update"),
    EntitySpec("job_stage", "job_stage", "Job stage", "{{ value_json.job_stage | default('idle') }}"),
    _numeric("job_progress", "job_progress", "Job progress", "value_json.job_progress", "%"),
    _text("active_job_id", "active_job_id", "Active job ID", "value_json.active_job_id", "none"),
    _text("last_job_id", "last_job_id", "Last job ID", "value_json.last_job_id", "none"),
    EntitySpec("lifecycle_status", "lifecycle_status", "Lifecycle status", "{{ value_json.lifecycle_status | default('idle') }}"),
    _timestamp("lifecycle_started_at", "lifecycle_started_at", "Lifecycle started at", "value_json.lifecycle_started_at"),
    _timestamp("lifecycle_finished_at", "lifecycle_finished_at", "Lifecycle finished at", "value_json.lifecycle_finished_at"),
) + LIFECYCLE_CAPABILITY_SPECS + SNAPSHOT_ENTITY_SPECS


SUPPORTED_RESOURCE_IDENTITIES = frozenset(
    {
        ("lxc", "apt"),
        ("lxc", "agent_self"),
        ("qemu", "haos"),
    }
)

# These exact 0.3.0 discovery keys are retained but have no current data source.
OBSOLETE_DISCOVERY_KEYS = {
    ("qemu", "haos"): ("cpu_load_1m",),
    ("lxc", "agent_self"): ("cpu_usage", "network_in_bytes", "network_out_bytes"),
}


def normalize_resource_identity(cfg: Mapping[str, Any]) -> ResourceIdentity:
    """Return a canonical, supported identity without changing invalid input."""

    missing = [key for key in ("resource_type", "adapter") if cfg.get(key) is None]
    if missing:
        raise ValueError(f"Missing resource identity field(s): {', '.join(missing)}")

    resource_type = str(cfg["resource_type"]).strip().lower()
    adapter = str(cfg["adapter"]).strip().lower()
    identity = (resource_type, adapter)
    if identity not in SUPPORTED_RESOURCE_IDENTITIES:
        raise ValueError(
            "Unsupported resource entity contract: "
            f"resource_type={resource_type!r}, adapter={adapter!r}"
        )
    return ResourceIdentity(resource_type=resource_type, adapter=adapter)


def resource_entity_specs(cfg: Mapping[str, Any]) -> tuple[EntitySpec, ...]:
    identity = normalize_resource_identity(cfg)
    if identity == ResourceIdentity("qemu", "haos"):
        return QEMU_ENTITY_SPECS + COMMON_RESOURCE_ENTITY_SPECS
    if identity == ResourceIdentity("lxc", "agent_self"):
        return AGENT_SELF_ENTITY_SPECS + COMMON_RESOURCE_ENTITY_SPECS
    if identity == ResourceIdentity("lxc", "apt"):
        return APT_ENTITY_SPECS + COMMON_RESOURCE_ENTITY_SPECS
    raise AssertionError("Supported resource identity has no entity specification")


def resource_prefix(vmid: int, cfg: Mapping[str, Any]) -> str:
    identity = normalize_resource_identity(cfg)
    return f"vm{int(vmid)}" if identity.resource_type == "qemu" else f"ct{int(vmid)}"


def obsolete_discovery_keys(cfg: Mapping[str, Any]) -> tuple[str, ...]:
    identity = normalize_resource_identity(cfg)
    return OBSOLETE_DISCOVERY_KEYS.get(
        (identity.resource_type, identity.adapter),
        (),
    )


def resource_entity_id(vmid: int, cfg: Mapping[str, Any], key: str) -> str:
    spec = next((item for item in resource_entity_specs(cfg) if item.key == key), None)
    if spec is None:
        raise KeyError(f"Entity {key!r} is not published for VMID {int(vmid)}")
    return f"sensor.hubinet_ops_{resource_prefix(vmid, cfg)}_{spec.suffix}"


def agent_entity_id(key: str) -> str:
    spec = next((item for item in AGENT_ENTITY_SPECS if item.key == key), None)
    if spec is None:
        raise KeyError(f"Unknown agent entity {key!r}")
    return f"sensor.hubinet_ops_agent_{spec.suffix}"
