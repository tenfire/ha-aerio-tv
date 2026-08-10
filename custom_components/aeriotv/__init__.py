"""AerioTV integration."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import AerioTVAuthError, AerioTVClient
from .const import CONF_PORT, CONF_TOKEN, DOMAIN, PLATFORMS

type AerioTVConfigEntry = ConfigEntry[AerioTVClient]

APP_READY_DELAY = 5.0
PENDING_CHANNELS = "pending_channels"
_LOGGER = logging.getLogger(__name__)


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
    entry.runtime_data = client
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await client.disconnect()
        raise
    pending_channels = hass.data.setdefault(DOMAIN, {}).setdefault(PENDING_CHANNELS, {})
    if channel_id := pending_channels.get(entry.entry_id):

        async def send_pending_channel() -> None:
            try:
                await asyncio.sleep(APP_READY_DELAY)
                await client.set_channel(channel_id)
            except Exception:
                _LOGGER.exception("Unable to restore the pending AerioTV channel after reload")
            finally:
                if pending_channels.get(entry.entry_id) == channel_id:
                    pending_channels.pop(entry.entry_id, None)

        hass.async_create_task(send_pending_channel(), f"Restore pending AerioTV channel for {entry.title}")
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AerioTVConfigEntry) -> bool:
    if await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.disconnect()
        return True
    return False
