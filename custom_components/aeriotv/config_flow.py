"""Config flow for AerioTV."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_CODE, CONF_HOST
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .client import AerioTVAuthError, AerioTVClient, AerioTVConnectionError
from .const import CONF_DEVICE_ID, CONF_PORT, CONF_TOKEN, DEFAULT_NAME, DOMAIN


class AerioTVConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Configure one AerioTV device."""

    VERSION = 1

    def __init__(self) -> None:
        self._client: AerioTVClient | None = None
        self._device_id: str | None = None
        self._name = DEFAULT_NAME
        self._host = ""
        self._port = 0
        self._reauth_entry: config_entries.ConfigEntry | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Require discovery because the advertised endpoint is ephemeral."""
        return self.async_abort(reason="discovery_required")

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> FlowResult:
        properties = discovery_info.properties
        if str(properties.get("v", "")) != "1" or not properties.get("id"):
            return self.async_abort(reason="unsupported_device")
        self._device_id = str(properties["id"])
        await self.async_set_unique_id(self._device_id)
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: discovery_info.host, CONF_PORT: discovery_info.port},
            reload_on_update=True,
        )
        self._host, self._port = discovery_info.host, discovery_info.port
        self._name = discovery_info.name.removesuffix("._aeriotv._tcp.local.").rstrip(".") or DEFAULT_NAME
        self.context["title_placeholders"] = {"name": self._name}
        return await self.async_step_confirm()

    async def async_step_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        if user_input is not None:
            result = await self._start_pairing()
            if result is not None:
                return result
            return self.async_abort(reason="cannot_connect")
        return self.async_show_form(step_id="confirm", description_placeholders={"name": self._name})

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Start reauthentication while preserving the existing entry."""
        entry = self._get_reauth_entry()
        self._reauth_entry = entry
        self._host = entry.data[CONF_HOST]
        self._port = entry.data[CONF_PORT]
        self._device_id = entry.data[CONF_DEVICE_ID]
        self._name = entry.title
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Ask the user to display a fresh pairing code."""
        if user_input is not None:
            result = await self._start_pairing()
            if result is not None:
                return result
            return self.async_abort(reason="cannot_connect")
        return self.async_show_form(step_id="reauth_confirm", description_placeholders={"name": self._name})

    async def _start_pairing(self) -> FlowResult | None:
        await self._close_client()
        self._client = AerioTVClient(async_get_clientsession(self.hass), self._host, self._port)
        try:
            await self._client.begin_pairing()
        except AerioTVConnectionError:
            await self._close_client()
            return None
        return await self.async_step_pair()

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            code = user_input[CONF_CODE]
            if len(code) != 6 or not code.isascii() or not code.isdigit():
                errors["base"] = "invalid_auth"
            else:
                assert self._client is not None
                try:
                    token = await self._client.submit_code(code)
                except AerioTVAuthError:
                    errors["base"] = "invalid_auth"
                except AerioTVConnectionError:
                    errors["base"] = "cannot_connect"
                    await self._close_client()
                else:
                    name = self._client.state.device_name or self._name
                    await self._close_client()
                    if self._reauth_entry is not None:
                        if self._reauth_entry.data[CONF_DEVICE_ID] != self._device_id:
                            return self.async_abort(reason="wrong_device")
                        return self.async_update_reload_and_abort(
                            self._reauth_entry,
                            data_updates={CONF_TOKEN: token},
                        )
                    if not self._device_id:
                        self._device_id = f"{self._host}:{self._port}"
                    await self.async_set_unique_id(self._device_id)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=name,
                        data={
                            CONF_HOST: self._host,
                            CONF_PORT: self._port,
                            CONF_DEVICE_ID: self._device_id,
                            CONF_TOKEN: token,
                        },
                    )
        return self.async_show_form(
            step_id="pair",
            data_schema=vol.Schema({vol.Required(CONF_CODE): vol.All(str, vol.Length(min=6, max=6))}),
            errors=errors,
        )

    async def _close_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.disconnect()
