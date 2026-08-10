"""AerioTV device turn-on trigger."""

from __future__ import annotations

import voluptuous as vol
from homeassistant.const import ATTR_DEVICE_ID, CONF_DEVICE_ID, CONF_DOMAIN, CONF_PLATFORM, CONF_TYPE
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.trigger import PluggableAction, TriggerActionType

from ..const import DOMAIN

PLATFORM_TYPE = f"{DOMAIN}.turn_on"

TRIGGER_SCHEMA = vol.All(
    cv.TRIGGER_BASE_SCHEMA.extend(
        {
            vol.Required(CONF_PLATFORM): PLATFORM_TYPE,
            vol.Optional(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
        }
    ),
    cv.has_at_least_one_key(ATTR_DEVICE_ID),
)


def async_get_turn_on_trigger(device_id: str) -> dict[str, str]:
    """Return the device automation representation of a turn-on trigger."""
    return {
        CONF_PLATFORM: "device",
        CONF_DEVICE_ID: device_id,
        CONF_DOMAIN: DOMAIN,
        CONF_TYPE: PLATFORM_TYPE,
    }


async def async_attach_trigger(
    hass: HomeAssistant,
    config: dict,
    action: TriggerActionType,
    trigger_info: dict,
) -> CALLBACK_TYPE:
    """Attach an AerioTV turn-on trigger."""
    device_ids = set(config.get(ATTR_DEVICE_ID, []))
    unsubs = []
    for device_id in device_ids:
        variables = {
            **trigger_info["trigger_data"],
            CONF_PLATFORM: PLATFORM_TYPE,
            ATTR_DEVICE_ID: device_id,
            "description": "AerioTV turn-on trigger",
        }
        unsubs.append(
            PluggableAction.async_attach_trigger(
                hass,
                async_get_turn_on_trigger(device_id),
                action,
                {"trigger": variables},
            )
        )

    @callback
    def remove() -> None:
        for unsub in unsubs:
            unsub()
        unsubs.clear()

    return remove
