"""Native per-resource operator controls for Hubinet Ops."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, override

from homeassistant.components import persistent_notification
from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import ResourceSnapshot, ResourceType
from .coordinator import HubinetOpsConfigEntry, resource_device_name
from .entity import HubinetOpsResourceEntity
from .services import (
    async_approve_reviewed_update_plan,
    async_resume_update,
    async_review_update_plan,
    async_rollback_update,
    async_start_update,
    async_view_health_contract,
    async_view_update_job,
)

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class HubinetOpsButtonDescription(ButtonEntityDescription):
    """Describe one explicit operator control."""

    capability: str


RESOURCE_BUTTONS = (
    HubinetOpsButtonDescription(
        key="review_update_plan",
        translation_key="review_update_plan",
        icon="mdi:file-document-search",
        capability="can_review_update_plan",
    ),
    HubinetOpsButtonDescription(
        key="approve_reviewed_plan",
        translation_key="approve_reviewed_plan",
        icon="mdi:check-decagram",
        capability="can_approve_update_plan",
    ),
    HubinetOpsButtonDescription(
        key="start_update",
        translation_key="start_update",
        icon="mdi:package-up",
        capability="can_start_update",
    ),
    HubinetOpsButtonDescription(
        key="view_update_job",
        translation_key="view_update_job",
        icon="mdi:timeline-text",
        capability="can_view_update_job",
    ),
    HubinetOpsButtonDescription(
        key="resume_update",
        translation_key="resume_update",
        icon="mdi:play-circle-outline",
        capability="can_resume_update",
    ),
    HubinetOpsButtonDescription(
        key="rollback_update",
        translation_key="rollback_update",
        icon="mdi:backup-restore",
        capability="can_rollback_update",
    ),
    HubinetOpsButtonDescription(
        key="view_health_contract",
        translation_key="view_health_contract",
        icon="mdi:heart-pulse",
        capability="can_view_health_contract",
    ),
)


def _notification_id(entry_id: str, resource_id: str, kind: str) -> str:
    return f"hubinet_ops_{entry_id}_{resource_id}_{kind}"


def _cell(value: Any) -> str:
    """Render bounded backend data without letting it alter the table."""

    if value is None:
        return "Unknown"
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _plan_message(plan: dict[str, Any]) -> str:
    packages = plan["packages"]
    lines = [
        f"**{plan['pending_count']} package update(s)**",
        "",
        "| Package | Architecture | Installed | Candidate | Origin | Security | Description |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        "| "
        + " | ".join(
            (
                _cell(package["name"]),
                _cell(package["architecture"]),
                _cell(package["installed_version"]),
                _cell(package["candidate_version"]),
                _cell(package["origin"]),
                "Yes" if package["security"] is True else "Unknown",
                _cell(package["description"]),
            )
        )
        + " |"
        for package in packages
    )
    lines.extend(
        (
            "",
            "Review every row, then press **Approve reviewed plan** on this "
            "resource. Approval is refused if the backend, resource, scan, or "
            "material fingerprint changes. This review is forgotten when Home "
            "Assistant reloads.",
        )
    )
    return "\n".join(lines)


def _job_message(job: dict[str, Any]) -> str:
    health = job["health_outcome"] or "No definitive result"
    facts = (
        ("Status", job["status"]),
        ("Checkpoint", job["checkpoint"]),
        ("Packages", job["package_count"]),
        ("Snapshot confirmed", job["snapshot_confirmed_at"] or "No"),
        ("Package mutation completed", job["mutation_completed_at"] or "No"),
        ("Health result", health),
        ("Rollback available", "Yes" if job["rollback_available"] else "No"),
        ("Terminal reason", job["terminal_reason"] or "None"),
    )
    lines = [f"- **{label}:** {_cell(value)}" for label, value in facts]
    if job["events"]:
        lines.extend(("", "### Recent durable events", ""))
        lines.extend(
            f"- `{_cell(event['created_at'])}` **{_cell(event['stage'])}** — "
            f"{_cell(event['message'])}"
            for event in job["events"]
        )
    return "\n".join(lines)


def _health_contract_message(contract: dict[str, Any]) -> str:
    if contract["status"] == "unconfigured":
        return (
            "No health contract is configured. This is **unconfigured**, not "
            "healthy, and a package update cannot start until a contract is declared."
        )
    lines = [
        f"**Revision:** {contract['revision']}",
        "",
        "Every probe below is required:",
        "",
    ]
    lines.extend(
        f"- `{_cell(probe['kind'])}` — `{_cell(probe['target'])}`"
        for probe in contract["probes"]
    )
    return "\n".join(lines)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: HubinetOpsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up dynamic resource buttons on existing Hubinet devices."""

    coordinator = entry.runtime_data

    def add_resources(resources: list[ResourceSnapshot]) -> None:
        async_add_entities(
            HubinetOpsResourceButton(coordinator, description, resource)
            for resource in resources
            if resource.resource_type is ResourceType.LXC
            for description in RESOURCE_BUTTONS
        )

    coordinator.new_resources_callbacks.append(add_resources)
    entry.async_on_unload(
        lambda: coordinator.new_resources_callbacks.remove(add_resources)
    )
    add_resources(list(coordinator.data.resources))


class HubinetOpsResourceButton(HubinetOpsResourceEntity, ButtonEntity):
    """One backend-gated, explicitly pressed resource operation."""

    entity_description: HubinetOpsButtonDescription

    @property
    @override
    def available(self) -> bool:
        if not super().available:
            return False
        if self.entity_description.key == "approve_reviewed_plan":
            return self.coordinator.reviewed_update_plan(self.resource_id) is not None
        return bool(
            getattr(
                self.resource.operator_capabilities,
                self.entity_description.capability,
            )
        )

    @override
    async def async_press(self) -> None:
        if not self.available:
            raise HomeAssistantError(
                "the Hubinet Ops backend does not currently allow this operation"
            )

        key = self.entity_description.key
        entry_id = self.coordinator.config_entry.entry_id
        name = resource_device_name(self.resource)
        if key == "review_update_plan":
            plan = await async_review_update_plan(self.coordinator, self.resource_id)
            persistent_notification.async_create(
                self.hass,
                _plan_message(plan),
                title=f"Hubinet Ops update plan — {name}",
                notification_id=_notification_id(entry_id, self.resource_id, "plan"),
            )
            return
        if key == "approve_reviewed_plan":
            await async_approve_reviewed_update_plan(
                self.coordinator, self.resource_id
            )
            persistent_notification.async_create(
                self.hass,
                "The exact reviewed package plan was approved. Starting the "
                "update still requires a separate explicit press.",
                title=f"Hubinet Ops plan approved — {name}",
                notification_id=_notification_id(entry_id, self.resource_id, "plan"),
            )
            return
        if key == "view_health_contract":
            contract = await async_view_health_contract(
                self.coordinator, self.resource_id
            )
            persistent_notification.async_create(
                self.hass,
                _health_contract_message(contract),
                title=f"Hubinet Ops health contract — {name}",
                notification_id=_notification_id(entry_id, self.resource_id, "health"),
            )
            return
        if key == "view_update_job":
            job = await async_view_update_job(self.coordinator, self.resource_id)
        elif key == "start_update":
            job = await async_start_update(self.coordinator, self.resource_id)
        elif key == "resume_update":
            job = await async_resume_update(self.coordinator, self.resource_id)
        elif key == "rollback_update":
            job = await async_rollback_update(self.coordinator, self.resource_id)
        else:  # pragma: no cover - descriptions are a closed local tuple
            raise HomeAssistantError("unknown Hubinet Ops operator control")
        persistent_notification.async_create(
            self.hass,
            _job_message(job),
            title=f"Hubinet Ops package update — {name}",
            notification_id=_notification_id(entry_id, self.resource_id, "job"),
        )
