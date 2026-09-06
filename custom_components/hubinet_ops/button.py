"""Native per-resource operator controls for Hubinet Ops."""

from __future__ import annotations

from dataclasses import dataclass
import html
import re
from typing import Any, Mapping, override
import unicodedata

from homeassistant.components import persistent_notification
from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.translation import async_get_translations

from .api import ResourceSnapshot, ResourceType
from .const import DOMAIN
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


_MARKDOWN_PUNCTUATION = re.compile(r"([\\`*_{\[\]()#+\-.!|}>~])")


def _visible_text(value: Any) -> str:
    """Make control and format characters visible instead of structural."""

    return "".join(
        (
            f"\\u{ord(character):04X}"
            if unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"}
            else character
        )
        for character in str(value)
    )


def _cell(value: Any, *, unknown: str) -> str:
    """Render exact backend data without allowing Markdown structure."""

    if value is None:
        return unknown
    escaped_html = html.escape(_visible_text(value), quote=False)
    return _MARKDOWN_PUNCTUATION.sub(r"\\\1", escaped_html)


def _translation_key(key: str) -> str:
    return f"component.{DOMAIN}.notifications.{key}"


def _tr(strings: Mapping[str, str], key: str, **values: Any) -> str:
    """Format one integration-owned notification translation."""

    return strings[_translation_key(key)].format(**values)


async def _notification_translations(hass: HomeAssistant) -> Mapping[str, str]:
    return await async_get_translations(
        hass,
        hass.config.language,
        "notifications",
        integrations={DOMAIN},
    )


def _plan_message(plan: dict[str, Any], strings: Mapping[str, str]) -> str:
    packages = plan["packages"]
    unknown = _tr(strings, "common.unknown")
    lines = [
        f"**{_tr(strings, 'plan.package_count', count=plan['pending_count'])}**",
        "",
        "| "
        + " | ".join(
            _tr(strings, f"plan.columns.{column}")
            for column in (
                "package",
                "architecture",
                "installed",
                "candidate",
                "origin",
                "security",
                "description",
            )
        )
        + " |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        "| "
        + " | ".join(
            (
                _cell(package["name"], unknown=unknown),
                _cell(package["architecture"], unknown=unknown),
                _cell(package["installed_version"], unknown=unknown),
                _cell(package["candidate_version"], unknown=unknown),
                _cell(package["origin"], unknown=unknown),
                _tr(
                    strings,
                    "common.yes"
                    if package["security"] is True
                    else "common.no"
                    if package["security"] is False
                    else "common.unknown",
                ),
                _cell(package["description"], unknown=unknown),
            )
        )
        + " |"
        for package in packages
    )
    lines.extend(
        (
            "",
            _tr(strings, "plan.approval_instruction"),
        )
    )
    return "\n".join(lines)


def _job_message(job: dict[str, Any], strings: Mapping[str, str]) -> str:
    unknown = _tr(strings, "common.unknown")
    health = job["health_outcome"] or _tr(strings, "job.no_definitive_result")
    facts = (
        (_tr(strings, "job.labels.status"), job["status"]),
        (_tr(strings, "job.labels.checkpoint"), job["checkpoint"]),
        (_tr(strings, "job.labels.packages"), job["package_count"]),
        (
            _tr(strings, "job.labels.snapshot_confirmed"),
            job["snapshot_confirmed_at"] or _tr(strings, "common.no"),
        ),
        (
            _tr(strings, "job.labels.mutation_completed"),
            job["mutation_completed_at"] or _tr(strings, "common.no"),
        ),
        (_tr(strings, "job.labels.health_result"), health),
        (
            _tr(strings, "job.labels.rollback_available"),
            _tr(
                strings,
                "common.yes" if job["rollback_available"] else "common.no",
            ),
        ),
        (
            _tr(strings, "job.labels.terminal_reason"),
            job["terminal_reason"] or _tr(strings, "common.none"),
        ),
    )
    lines = [
        f"- **{label}:** {_cell(value, unknown=unknown)}" for label, value in facts
    ]
    if job["events"]:
        lines.extend(("", f"### {_tr(strings, 'job.recent_events')}", ""))
        lines.extend(
            f"- {_cell(event['created_at'], unknown=unknown)} — "
            f"{_cell(event['stage'], unknown=unknown)} — "
            f"{_cell(event['message'], unknown=unknown)}"
            for event in job["events"]
        )
    return "\n".join(lines)


def _health_contract_message(
    contract: dict[str, Any], strings: Mapping[str, str]
) -> str:
    if contract["status"] == "unconfigured":
        return _tr(strings, "health.unconfigured")
    unknown = _tr(strings, "common.unknown")
    lines = [
        f"**{_tr(strings, 'health.revision')}:** "
        f"{_cell(contract['revision'], unknown=unknown)}",
        "",
        _tr(strings, "health.all_required"),
        "",
    ]
    lines.extend(
        f"- {_cell(probe['kind'], unknown=unknown)} — "
        f"{_cell(probe['target'], unknown=unknown)}"
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
                self.coordinator.operator_capabilities(self.resource_id),
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
        name = _visible_text(resource_device_name(self.resource))
        strings = await _notification_translations(self.hass)
        if key == "review_update_plan":
            plan = await async_review_update_plan(self.coordinator, self.resource_id)
            persistent_notification.async_create(
                self.hass,
                _plan_message(plan, strings),
                title=_tr(strings, "plan.title", name=name),
                notification_id=_notification_id(entry_id, self.resource_id, "plan"),
            )
            return
        if key == "approve_reviewed_plan":
            await async_approve_reviewed_update_plan(
                self.coordinator, self.resource_id
            )
            persistent_notification.async_create(
                self.hass,
                _tr(strings, "approval.message"),
                title=_tr(strings, "approval.title", name=name),
                notification_id=_notification_id(entry_id, self.resource_id, "plan"),
            )
            return
        if key == "view_health_contract":
            contract = await async_view_health_contract(
                self.coordinator, self.resource_id
            )
            persistent_notification.async_create(
                self.hass,
                _health_contract_message(contract, strings),
                title=_tr(strings, "health.title", name=name),
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
            _job_message(job, strings),
            title=_tr(strings, "job.title", name=name),
            notification_id=_notification_id(entry_id, self.resource_id, "job"),
        )
