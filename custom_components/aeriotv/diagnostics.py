"""Diagnostics for AerioTV."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import async_redact_data

from . import AerioTVConfigEntry
from .const import CONF_DEVICE_ID, CONF_TOKEN

TO_REDACT = {"host", CONF_TOKEN, CONF_DEVICE_ID, "title", "device_name", "channel_id"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: AerioTVConfigEntry) -> dict:
    """Return privacy-preserving diagnostics for one AerioTV entry."""
    state = entry.runtime_data.state
    return async_redact_data(
        {
            "entry": {"title": entry.title, "data": dict(entry.data)},
            "state": {
                "connected": state.connected,
                "device_name": state.device_name,
                "channel_id": state.channel_id,
                "is_playing": state.is_playing,
                "can_seek": state.can_seek,
                "is_live": state.is_live,
            },
        },
        TO_REDACT,
    )
