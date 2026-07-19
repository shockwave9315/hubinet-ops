from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


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


AGENT_ENTITY_SPECS = (
    EntitySpec("availability", "availability", "Availability", "{{ value }}"),
    EntitySpec("version", "version", "Version", "{{ value_json.version }}"),
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
    EntitySpec("active_plan_id", "active_plan_id", "Active plan ID", "{{ value_json.active_plan_id | default('none', true) }}"),
    EntitySpec("active_job_id", "active_job_id", "Active job ID", "{{ value_json.active_job_id | default('none', true) }}"),
    EntitySpec("last_scan", "last_scan", "Last scan", "{{ value_json.last_scan | default('unknown', true) }}"),
    EntitySpec("last_refresh", "last_refresh", "Last refresh", "{{ value_json.last_refresh | default('unknown', true) }}"),
    EntitySpec("last_update", "last_update", "Last update", "{{ value_json.last_update | default('unknown', true) }}"),
    EntitySpec("last_error", "last_error", "Last error", "{{ value_json.last_error | default('none', true) }}"),
    EntitySpec("last_operation_result", "last_operation_result", "Last operation result", "{{ value_json.last_operation_result | default('none', true) }}"),
    EntitySpec("rollback_allowed", "rollback_allowed", "Rollback allowed", "{{ 'allowed' if value_json.rollback_allowed else 'blocked' }}"),
    EntitySpec("last_job_event", "last_job_event", "Last job event", "{{ value_json.last_job_event.message | default('none') }}"),
    EntitySpec("lifecycle_status", "lifecycle_status", "Lifecycle status", "{{ value_json.lifecycle_status | default('idle') }}"),
    EntitySpec("lifecycle_action", "lifecycle_action", "Lifecycle action", "{{ value_json.lifecycle_action | default('none', true) }}"),
    EntitySpec("verification_status", "verification_status", "Verification status", "{{ value_json.verification_status | default('unknown') }}"),
    EntitySpec("last_verification", "last_verification", "Last verification", "{{ value_json.last_verification | default('unknown', true) }}"),
    EntitySpec("apt_check_ok", "apt_check_ok", "APT check", "{{ 'unknown' if value_json.apt_check_ok is none else 'ok' if value_json.apt_check_ok else 'failed' }}"),
    EntitySpec("dpkg_audit_ok", "dpkg_audit_ok", "dpkg audit", "{{ 'unknown' if value_json.dpkg_audit_ok is none else 'ok' if value_json.dpkg_audit_ok else 'failed' }}"),
    EntitySpec("reboot_required", "reboot_required", "Reboot required", "{{ 'unknown' if value_json.reboot_required is none else 'yes' if value_json.reboot_required else 'no' }}"),
    EntitySpec("packages_remaining_count", "packages_remaining_count", "Packages remaining", "{{ value_json.packages_remaining_count | default(none) }}"),
    EntitySpec("recovery_scan_status", "recovery_scan_status", "Recovery scan status", "{{ value_json.recovery_scan_status | default('disabled') }}"),
    EntitySpec("last_recovery_scan", "last_recovery_scan", "Last recovery scan", "{{ value_json.last_recovery_scan | default('unknown', true) }}"),
    EntitySpec("last_recovery_scan_result", "last_recovery_scan_result", "Last recovery scan result", "{{ value_json.last_recovery_scan_result | default('none', true) }}"),
    EntitySpec("capability_start", "capability_start", "Capability start", "{{ 'allowed' if value_json.operator_capabilities.start else 'blocked' }}"),
    EntitySpec("capability_shutdown", "capability_shutdown", "Capability shutdown", "{{ 'allowed' if value_json.operator_capabilities.shutdown else 'blocked' }}"),
    EntitySpec("capability_reboot", "capability_reboot", "Capability reboot", "{{ 'allowed' if value_json.operator_capabilities.reboot else 'blocked' }}"),
    EntitySpec("capability_refresh", "capability_refresh", "Capability refresh", "{{ 'allowed' if value_json.operator_capabilities.refresh else 'blocked' }}"),
    EntitySpec("capability_scan", "capability_scan", "Capability scan", "{{ 'allowed' if value_json.operator_capabilities.scan else 'blocked' }}"),
    EntitySpec("capability_approve", "capability_approve", "Capability approve", "{{ 'allowed' if value_json.operator_capabilities.approve else 'blocked' }}"),
    EntitySpec("capability_reject", "capability_reject", "Capability reject", "{{ 'allowed' if value_json.operator_capabilities.reject else 'blocked' }}"),
    EntitySpec("capability_retry_healthcheck", "capability_retry_healthcheck", "Capability retry healthcheck", "{{ 'allowed' if value_json.operator_capabilities.retry_healthcheck else 'blocked' }}"),
    EntitySpec("capability_rollback", "capability_rollback", "Capability rollback", "{{ 'allowed' if value_json.operator_capabilities.rollback else 'blocked' }}"),
)


QEMU_ENTITY_SPECS = (
    EntitySpec("health_status", "health_status", "Health status", "{{ value_json.health_status }}"),
    _numeric("health_score", "health_score", "Health score", "value_json.health_score", "%"),
    EntitySpec("runtime_status", "runtime_status", "Runtime status", "{{ value_json.runtime_status | default('unknown') }}"),
    EntitySpec("qemu_status", "qemu_status", "QEMU status", "{{ value_json.qemu_status | default('unknown') }}"),
    _numeric("uptime_seconds", "uptime", "Uptime", "value_json.uptime_seconds", "s"),
    _numeric("cpu_usage", "cpu_usage", "CPU usage", "value_json.cpu.usage_percent", "%"),
    EntitySpec("cpu_cores", "cpu_cores", "CPU cores", "{{ value_json.cpu.cores | default(none) }}"),
    _numeric("memory_used_bytes", "memory_used", "Memory used", "value_json.memory.used_bytes", "B"),
    _numeric("memory_total_bytes", "memory_total", "Memory total", "value_json.memory.total_bytes", "B"),
    _numeric("disk_used_bytes", "disk_used", "Disk used", "value_json.disk.used_bytes", "B"),
    _numeric("disk_total_bytes", "disk_total", "Disk total", "value_json.disk.total_bytes", "B"),
    _numeric("network_in_bytes", "network_received", "Network received", "value_json.network.in_bytes", "B"),
    _numeric("network_out_bytes", "network_sent", "Network sent", "value_json.network.out_bytes", "B"),
    EntitySpec("guest_agent_status", "guest_agent", "Guest Agent", "{{ value_json.guest_agent_status | default('unknown') }}"),
    EntitySpec("ip_addresses", "ip_addresses", "Primary IP", "{{ value_json.primary_ip_address | default('unknown', true) }}"),
    EntitySpec("last_refresh", "last_refresh", "Last refresh", "{{ value_json.last_refresh | default('unknown', true) }}"),
    EntitySpec("last_error", "last_error", "Last error", "{{ value_json.last_error | default('none', true) }}"),
)


AGENT_SELF_ENTITY_SPECS = (
    EntitySpec("health_status", "health_status", "Health status", "{{ value_json.health_status }}"),
    _numeric("health_score", "health_score", "Health score", "value_json.health_score", "%"),
    EntitySpec("runtime_status", "runtime_status", "Runtime status", "{{ value_json.runtime_status | default('unknown') }}"),
    EntitySpec("lxc_status", "lxc_status", "LXC status", "{{ value_json.lxc_status | default('unknown') }}"),
    _numeric("uptime_seconds", "uptime", "Uptime", "value_json.uptime_seconds", "s"),
    EntitySpec("cpu_cores", "cpu_cores", "CPU cores", "{{ value_json.cpu.cores | default(none) }}"),
    EntitySpec("cpu_load_1m", "cpu_load_1m", "CPU load 1m", "{{ value_json.cpu.load_1m | default(none) }}"),
    _numeric("memory_used_bytes", "memory_used", "Memory used", "value_json.memory.used_bytes", "B"),
    _numeric("memory_total_bytes", "memory_total", "Memory total", "value_json.memory.total_bytes", "B"),
    _numeric("memory_available_bytes", "memory_available", "Memory available", "value_json.memory.available_bytes", "B"),
    _numeric("disk_used_bytes", "disk_used", "Disk used", "value_json.disk.used_bytes", "B"),
    _numeric("disk_total_bytes", "disk_total", "Disk total", "value_json.disk.total_bytes", "B"),
    _numeric("disk_free_bytes", "disk_free", "Disk free", "value_json.disk.free_bytes", "B"),
    EntitySpec("service_status", "service_status", "Service status", "{{ value_json.service_status | default('unknown') }}"),
    EntitySpec("api_health", "api_health", "API health", "{{ value_json.api_health | default('unknown') }}"),
    EntitySpec("agent_version", "agent_version", "Agent version", "{{ value_json.agent_version | default('unknown') }}"),
    EntitySpec("recent_warnings", "recent_warnings", "Recent warnings", "{{ value_json.recent_warnings | default([]) | count }}"),
    EntitySpec("last_refresh", "last_refresh", "Last refresh", "{{ value_json.last_refresh | default('unknown', true) }}"),
    EntitySpec("last_error", "last_error", "Last error", "{{ value_json.last_error | default('none', true) }}"),
)


SUPPORTED_RESOURCE_IDENTITIES = frozenset(
    {
        ("lxc", "apt"),
        ("lxc", "agent_self"),
        ("qemu", "haos"),
    }
)

# These exact 0.3.0 discovery keys are retained but have no 0.3.1 data source.
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
        return QEMU_ENTITY_SPECS
    if identity == ResourceIdentity("lxc", "agent_self"):
        return AGENT_SELF_ENTITY_SPECS
    if identity == ResourceIdentity("lxc", "apt"):
        return APT_ENTITY_SPECS
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
