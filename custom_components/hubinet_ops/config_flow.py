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

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import create_api_client
from .api import (
    BackendInformation,
    HubinetOpsApiError,
    HubinetOpsCannotConnect,
    HubinetOpsInvalidAuth,
)
from .const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_VERIFY_TLS,
    DEFAULT_VERIFY_TLS,
    DOMAIN,
)

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

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Validate initial setup and bind the entry to backend instance_id."""

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
                await self.async_set_unique_id(info.instance_id)
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
                if info.instance_id != entry.unique_id:
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
                if info.instance_id != entry.unique_id:
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
