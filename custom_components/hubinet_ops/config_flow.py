"""Config flow for Hubinet Ops.

The flow lifecycle follows Home Assistant Core's Apache-2.0 ``proxmoxve``
integration, but all fields and validation target the Hubinet Ops backend.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any, override
from urllib.parse import urlsplit, urlunsplit

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import create_api_client
from .api import (
    BackendInformation,
    HealthDiscoveryCandidate,
    HealthProbe,
    HealthProbeKind,
    HubinetOpsApiError,
    HubinetOpsCannotConnect,
    HubinetOpsConflict,
    HubinetOpsHealthContractUnconfigured,
    HubinetOpsInvalidAuth,
    ResourceType,
)
from .const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_VERIFY_TLS,
    DATA_COORDINATORS,
    DEFAULT_VERIFY_TLS,
    DOMAIN,
)
from .coordinator import resource_device_name

_LOGGER = logging.getLogger(__name__)


def _connection_schema(
    *, defaults: Mapping[str, Any] | None = None
) -> vol.Schema:
    values = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_BASE_URL, default=values.get(CONF_BASE_URL, "https://")
            ): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
            vol.Required(CONF_API_TOKEN): TextSelector(
                TextSelectorConfig(
                    type=TextSelectorType.PASSWORD,
                    autocomplete="current-password",
                )
            ),
            vol.Required(
                CONF_VERIFY_TLS,
                default=values.get(CONF_VERIFY_TLS, DEFAULT_VERIFY_TLS),
            ): bool,
        }
    )


def _reauth_schema() -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_API_TOKEN): TextSelector(
                TextSelectorConfig(
                    type=TextSelectorType.PASSWORD,
                    autocomplete="current-password",
                )
            )
        }
    )


def _normalize_input(user_input: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(user_input)
    raw_url = str(data[CONF_BASE_URL]).strip()
    parsed = urlsplit(raw_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("invalid_url")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("invalid_url")
    normalized_path = parsed.path.rstrip("/")
    data[CONF_BASE_URL] = urlunsplit(
        (parsed.scheme.lower(), parsed.netloc, normalized_path, "", "")
    )
    data[CONF_API_TOKEN] = str(data[CONF_API_TOKEN]).strip()
    if not data[CONF_API_TOKEN]:
        raise ValueError("invalid_auth")
    data[CONF_VERIFY_TLS] = bool(data.get(CONF_VERIFY_TLS, DEFAULT_VERIFY_TLS))
    return data


async def _async_validate(
    hass: HomeAssistant, data: Mapping[str, Any]
) -> BackendInformation:
    client = create_api_client(hass, data)
    return await client.async_validate_connection()


class HubinetOpsConfigFlow(ConfigFlow, domain=DOMAIN):
    """Configure a clean-install Hubinet Ops 0.5 backend connection."""

    VERSION = 1
    MINOR_VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> HubinetOpsOptionsFlow:
        """Native per-resource health-contract maintenance (Settings ->
        Devices & Services -> Hubinet Ops -> Configure). See
        `HubinetOpsOptionsFlow`."""

        return HubinetOpsOptionsFlow()

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate setup and bind the entry to exact backend_instance_id."""

        errors: dict[str, str] = {}
        data: dict[str, Any] | None = None
        if user_input is not None:
            try:
                data = _normalize_input(user_input)
                info = await _async_validate(self.hass, data)
            except ValueError as err:
                errors["base"] = str(err)
            except HubinetOpsInvalidAuth:
                errors["base"] = "invalid_auth"
            except HubinetOpsCannotConnect:
                errors["base"] = "cannot_connect"
            except HubinetOpsApiError:
                _LOGGER.debug("Hubinet Ops setup validation failed", exc_info=True)
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(info.backend_instance_id)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=info.name, data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=_connection_schema(defaults=data or user_input),
            errors=errors,
        )

    @override
    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication for an existing backend instance."""

        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate and store a replacement Hubinet Ops bearer token."""

        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = _normalize_input({**entry.data, **user_input})
                info = await _async_validate(self.hass, data)
                if info.backend_instance_id != entry.unique_id:
                    errors["base"] = "wrong_instance"
                else:
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={CONF_API_TOKEN: data[CONF_API_TOKEN]},
                    )
            except ValueError as err:
                errors["base"] = str(err)
            except HubinetOpsInvalidAuth:
                errors["base"] = "invalid_auth"
            except HubinetOpsCannotConnect:
                errors["base"] = "cannot_connect"
            except HubinetOpsApiError:
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_reauth_schema(),
            errors=errors,
        )

    @override
    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconfigure URL, token and TLS while preserving backend identity."""

        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        data: dict[str, Any] | None = None
        if user_input is not None:
            try:
                data = _normalize_input(user_input)
                info = await _async_validate(self.hass, data)
                if info.backend_instance_id != entry.unique_id:
                    errors["base"] = "wrong_instance"
                else:
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates=data,
                    )
            except ValueError as err:
                errors["base"] = str(err)
            except HubinetOpsInvalidAuth:
                errors["base"] = "invalid_auth"
            except HubinetOpsCannotConnect:
                errors["base"] = "cannot_connect"
            except HubinetOpsApiError:
                errors["base"] = "unknown"

        suggested = data or {
            CONF_BASE_URL: entry.data[CONF_BASE_URL],
            CONF_VERIFY_TLS: entry.data[CONF_VERIFY_TLS],
        }
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_connection_schema(defaults=suggested),
            errors=errors,
        )


def _probe_field(kind: HealthProbeKind, target: str | None) -> str:
    """One stable, readable form-field name per probe identity -- mirrors
    the backend's own duplicate-identity rule, `(kind, target)`."""

    if kind is HealthProbeKind.GUEST_OPERATIONAL:
        return "probe__guest_operational"
    return f"probe__{kind.value}__{target}"


def _probe_line(
    kind: HealthProbeKind, target: str | None, note: str, *, recommended: bool
) -> str:
    target_text = target if target is not None else "(no target -- guest-level fallback)"
    flag = " (recommended)" if recommended else ""
    return f"- **{kind.value}** `{target_text}`{flag} -- {note}"


class HubinetOpsOptionsFlow(OptionsFlow):
    """Native per-resource health-contract maintenance: view the current
    contract, re-discover backend candidates, edit the selection, replace
    the contract, or explicitly clear it.

    Reached from Settings -> Devices & Services -> Hubinet Ops -> Configure,
    never Developer Tools. Discovery classification -- Docker/systemd
    inspection, origin/role_hint, the recommendation priority order, and the
    `guest_operational` fallback decision -- has no counterpart here at all:
    this flow only ever renders an already-classified, already-bounded
    `HealthDiscoveryResult` fetched over the existing typed route, and
    nothing becomes a declared contract until the operator explicitly
    submits the confirm/clear step. The contract's own `revision`, read the
    moment this flow looked at it, is sent back as `expected_revision` on
    every write -- a concurrent change is refused (`HubinetOpsConflict`)
    rather than silently overwritten, and this flow re-reads the current
    state and lets the operator try again instead of retrying blindly.
    """

    _resource_id: str = ""
    _resource_name: str = ""
    _current_probes: tuple[HealthProbe, ...] = ()
    _current_revision: int | None = None
    _candidates: tuple[HealthDiscoveryCandidate, ...] = ()

    def _coordinator(self):
        coordinators = self.hass.data.get(DOMAIN, {}).get(DATA_COORDINATORS, {})
        return coordinators.get(self.config_entry.entry_id)

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        coordinator = self._coordinator()
        if coordinator is None:
            return self.async_abort(reason="entry_not_loaded")
        resources = {
            resource.resource_id: resource_device_name(resource)
            for resource in coordinator.data.resources
            if resource.resource_type is ResourceType.LXC
        }
        if not resources:
            return self.async_abort(reason="no_lxc_resources")
        if user_input is not None:
            self._resource_id = user_input["resource_id"]
            self._resource_name = resources[self._resource_id]
            return await self.async_step_menu()
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required("resource_id"): vol.In(resources)}),
        )

    async def _read_current_contract(self) -> str | None:
        """Refresh `_current_probes`/`_current_revision`. Returns an abort
        reason on failure, else ``None``."""

        coordinator = self._coordinator()
        if coordinator is None:
            return "entry_not_loaded"
        try:
            contract = await coordinator.api.async_fetch_health_contract(
                self._resource_id
            )
            self._current_probes = contract.probes or ()
            self._current_revision = contract.revision
        except HubinetOpsHealthContractUnconfigured:
            self._current_probes = ()
            self._current_revision = None
        except HubinetOpsApiError:
            return "health_contract_read_failed"
        return None

    async def async_step_menu(
        self, user_input: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        abort_reason = await self._read_current_contract()
        if abort_reason is not None:
            return self.async_abort(reason=abort_reason)
        if not self._current_probes:
            # Nothing declared yet -- there is nothing to view or clear, so
            # go straight to discovery, exactly like the onboarding Repair.
            return await self.async_step_discover()
        return self.async_show_menu(
            step_id="menu", menu_options=["discover", "clear_confirm"]
        )

    def _line_items(self) -> list[dict[str, Any]]:
        """Every current probe UNIONED with every discovered candidate,
        deduplicated by `(kind, target)` identity. A current probe always
        defaults to checked (removing one is an explicit uncheck, never
        implicit); a newly-discovered candidate defaults to the backend's
        own `recommended` flag."""

        merged: dict[tuple[HealthProbeKind, str | None], dict[str, Any]] = {}
        for probe in self._current_probes:
            merged[(probe.kind, probe.target)] = {
                "kind": probe.kind,
                "target": probe.target,
                "default": True,
                "line": _probe_line(
                    probe.kind, probe.target, "currently declared", recommended=False
                ),
            }
        for candidate in self._candidates:
            key = (candidate.kind, candidate.target)
            if key in merged:
                continue
            merged[key] = {
                "kind": candidate.kind,
                "target": candidate.target,
                "default": candidate.recommended,
                "line": _probe_line(
                    candidate.kind,
                    candidate.target,
                    candidate.rationale,
                    recommended=candidate.recommended,
                ),
            }
        return [
            {**value, "field": _probe_field(value["kind"], value["target"])}
            for value in merged.values()
        ]

    async def async_step_discover(
        self, user_input: dict[str, bool] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        coordinator = self._coordinator()
        if coordinator is None:
            return self.async_abort(reason="entry_not_loaded")

        if user_input is None:
            try:
                result = await coordinator.api.async_fetch_health_candidates(
                    self._resource_id
                )
            except HubinetOpsApiError:
                return self.async_abort(reason="discovery_failed")
            self._candidates = result.candidates
        else:
            line_items = self._line_items()
            selected = tuple(
                item for item in line_items if user_input.get(item["field"])
            )
            if not selected:
                errors["base"] = "no_probes_selected"
            else:
                probes = tuple(
                    HealthProbe(kind=item["kind"], target=item["target"])
                    for item in selected
                )
                try:
                    await coordinator.api.async_replace_health_contract(
                        self._resource_id, probes, self._current_revision
                    )
                except HubinetOpsConflict:
                    errors["base"] = "revision_changed"
                    await self._read_current_contract()
                except HubinetOpsApiError:
                    errors["base"] = "declare_failed"
                else:
                    await coordinator.async_request_refresh()
                    return self.async_create_entry(title="", data={})

        line_items = self._line_items()
        schema = vol.Schema(
            {
                vol.Optional(item["field"], default=item["default"]): bool
                for item in line_items
            }
        )
        return self.async_show_form(
            step_id="discover",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "name": self._resource_name,
                "candidates": "\n".join(item["line"] for item in line_items),
            },
        )

    async def async_step_clear_confirm(
        self, user_input: dict[str, bool] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("confirm"):
                errors["base"] = "clear_not_confirmed"
            else:
                coordinator = self._coordinator()
                if coordinator is None:
                    return self.async_abort(reason="entry_not_loaded")
                try:
                    await coordinator.api.async_clear_health_contract(
                        self._resource_id, self._current_revision
                    )
                except HubinetOpsConflict:
                    errors["base"] = "revision_changed"
                    await self._read_current_contract()
                except HubinetOpsApiError:
                    errors["base"] = "clear_failed"
                else:
                    await coordinator.async_request_refresh()
                    return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="clear_confirm",
            data_schema=vol.Schema({vol.Required("confirm", default=False): bool}),
            errors=errors,
            description_placeholders={
                "name": self._resource_name,
                "candidates": "\n".join(
                    _probe_line(probe.kind, probe.target, "currently declared", recommended=False)
                    for probe in self._current_probes
                ),
            },
        )
