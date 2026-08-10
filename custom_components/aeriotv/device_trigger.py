"""Device automations for AerioTV."""

import voluptuous as vol
from homeassistant.components.device_automation import DEVICE_TRIGGER_BASE_SCHEMA
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_DEVICE_ID, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.trigger import TriggerActionType, TriggerInfo
from homeassistant.helpers.typing import ConfigType

from . import trigger
from .const import DOMAIN
from .triggers.turn_on import PLATFORM_TYPE, async_get_turn_on_trigger

TRIGGER_SCHEMA = DEVICE_TRIGGER_BASE_SCHEMA.extend({vol.Required(CONF_TYPE): vol.In({PLATFORM_TYPE})})


async def async_validate_trigger_config(hass: HomeAssistant, config: ConfigType) -> ConfigType:
    """Validate a device trigger."""
    config = TRIGGER_SCHEMA(config)
    if not _is_aeriotv_device(hass, config[CONF_DEVICE_ID]):
        raise vol.Invalid("Device is not a loaded AerioTV device")
    return config


def _is_aeriotv_device(hass: HomeAssistant, device_id: str) -> bool:
    """Return whether a device belongs to a loaded AerioTV entry."""
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return False
    return any(
        (entry := hass.config_entries.async_get_entry(entry_id)) is not None
        and entry.domain == DOMAIN
        and entry.state is ConfigEntryState.LOADED
        for entry_id in device.config_entries
    )


async def async_get_triggers(hass: HomeAssistant, device_id: str) -> list[dict[str, str]]:
    """List device triggers."""
    if not _is_aeriotv_device(hass, device_id):
        return []
    return [async_get_turn_on_trigger(device_id)]


async def async_attach_trigger(
    hass: HomeAssistant,
    config: ConfigType,
    action: TriggerActionType,
    trigger_info: TriggerInfo,
) -> CALLBACK_TYPE:
    """Attach a device trigger."""
    trigger_config = {CONF_PLATFORM: PLATFORM_TYPE, CONF_DEVICE_ID: config[CONF_DEVICE_ID]}
    trigger_config = await trigger.async_validate_trigger_config(hass, trigger_config)
    return await trigger.async_attach_trigger(hass, trigger_config, action, trigger_info)
