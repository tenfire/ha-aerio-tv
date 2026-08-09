"""AerioTV integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import AerioTVAuthError, AerioTVClient, AerioTVConnectionError
from .const import CONF_PORT, CONF_TOKEN, PLATFORMS

type AerioTVConfigEntry = ConfigEntry[AerioTVClient]


async def async_setup_entry(hass: HomeAssistant, entry: AerioTVConfigEntry) -> bool:
    def auth_failed() -> None:
        hass.async_create_task(entry.async_start_reauth(hass))

    client = AerioTVClient(
        async_get_clientsession(hass),
        entry.data["host"],
        entry.data[CONF_PORT],
        entry.data[CONF_TOKEN],
        auth_failed,
    )
    try:
        await client.start()
    except AerioTVAuthError as err:
        raise ConfigEntryAuthFailed from err
    except AerioTVConnectionError as err:
        raise ConfigEntryNotReady from err
    entry.runtime_data = client
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await client.disconnect()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AerioTVConfigEntry) -> bool:
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.disconnect()
        return True
    return False
