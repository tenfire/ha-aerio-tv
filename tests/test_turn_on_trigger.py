"""Tests for AerioTV's user-defined turn-on trigger."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant import config_entries
from homeassistant.components.media_player import MediaPlayerEntityFeature, MediaPlayerState
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr

from custom_components.aeriotv.client import AerioTVState
from custom_components.aeriotv.const import CONF_DEVICE_ID, CONF_PORT, CONF_TOKEN, DOMAIN
from custom_components.aeriotv.device_trigger import async_get_triggers
from custom_components.aeriotv.media_player import AerioTVMediaPlayer
from custom_components.aeriotv.triggers.turn_on import async_get_turn_on_trigger


def make_entry(client) -> config_entries.ConfigEntry:
    entry = config_entries.ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="Living Room",
        data={"host": "192.0.2.10", CONF_PORT: 43123, CONF_DEVICE_ID: "device-id", CONF_TOKEN: "token"},
        source=config_entries.SOURCE_USER,
        unique_id="device-id",
        discovery_keys={},
        options={},
        subentries_data=[],
    )
    entry.runtime_data = client
    return entry


def test_closed_app_is_off_and_hides_cached_transport_controls(hass: HomeAssistant) -> None:
    """A closed foreground app is off and exposes no stale transport controls."""
    client = AsyncMock()
    client.state = AerioTVState(connected=False, can_seek=True)
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass
    loaded_dispatcharr = Mock(state=ConfigEntryState.LOADED, entry_id="dispatcharr-entry")

    with patch.object(hass.config_entries, "async_entries", return_value=[loaded_dispatcharr]):
        assert entity.available is True
        assert entity.state is MediaPlayerState.OFF
        assert entity.supported_features == MediaPlayerEntityFeature(0)


async def test_turn_on_runs_registered_automation(hass: HomeAssistant) -> None:
    """Calling turn_on dispatches the action attached to this device."""
    client = AsyncMock()
    client.state = AerioTVState(connected=False)
    client.add_callback = Mock(return_value=Mock())
    entry = make_entry(client)
    entry._state = ConfigEntryState.LOADED
    hass.config_entries._entries[entry.entry_id] = entry
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "device-id")},
        name="Living Room",
    )
    entity = AerioTVMediaPlayer(entry)
    entity.hass = hass
    entity.registry_entry = Mock(device_id=device.id)
    entity._turn_on_action._update = Mock()
    action = AsyncMock()

    await entity.async_added_to_hass()
    remove = entity._turn_on_action.async_attach_trigger(
        hass,
        async_get_turn_on_trigger(device.id),
        action,
        {"trigger": {}},
    )
    try:
        assert entity.supported_features == MediaPlayerEntityFeature.TURN_ON
        await entity.async_turn_on()
        await hass.async_block_till_done()
    finally:
        remove()
        await entity.async_will_remove_from_hass()

    action.assert_awaited_once()


async def test_device_exposes_turn_on_trigger(hass: HomeAssistant) -> None:
    """The automation editor can discover the AerioTV turn-on trigger."""
    client = AsyncMock()
    client.state = AerioTVState(connected=False)
    entry = make_entry(client)
    entry._state = ConfigEntryState.LOADED
    hass.config_entries._entries[entry.entry_id] = entry
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, "device-id")},
        name="Living Room",
    )

    with patch.object(
        hass.config_entries, "async_get_entry", return_value=Mock(domain=DOMAIN, state=ConfigEntryState.LOADED)
    ):
        assert await async_get_triggers(hass, device.id) == [async_get_turn_on_trigger(device.id)]


async def test_unrelated_device_has_no_turn_on_trigger(hass: HomeAssistant) -> None:
    """Trigger discovery is scoped to loaded AerioTV devices."""
    other_entry = Mock(entry_id="other-entry", domain="other", state=ConfigEntryState.LOADED)
    with patch.object(hass.config_entries, "async_get_entry", return_value=other_entry):
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=other_entry.entry_id,
            identifiers={("other", "device-id")},
            name="Other",
        )

    with patch.object(hass.config_entries, "async_get_entry", return_value=other_entry):
        assert await async_get_triggers(hass, device.id) == []


async def test_turn_on_without_automation_fails_cleanly(hass: HomeAssistant) -> None:
    """A missing automation raises a user-facing error rather than an assertion."""
    client = AsyncMock()
    client.state = AerioTVState(connected=False)
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass

    with pytest.raises(HomeAssistantError, match="No AerioTV turn-on automation"):
        await entity.async_turn_on()
