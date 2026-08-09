"""Tests for AerioTV config-entry lifecycle."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed

from custom_components.aeriotv import async_setup_entry, async_unload_entry
from custom_components.aeriotv.client import AerioTVAuthError
from custom_components.aeriotv.const import CONF_DEVICE_ID, CONF_PORT, CONF_TOKEN, DOMAIN, PLATFORMS


def make_entry() -> config_entries.ConfigEntry:
    """Create one configured AerioTV entry."""
    return config_entries.ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="Living Room",
        data={
            CONF_HOST: "192.0.2.10",
            CONF_PORT: 43123,
            CONF_DEVICE_ID: "tv-stable-id",
            CONF_TOKEN: "saved-token",
        },
        source=config_entries.SOURCE_ZEROCONF,
        unique_id="tv-stable-id",
        discovery_keys={},
        options={},
        subentries_data=[],
    )


async def test_setup_and_unload_supervised_client(hass: HomeAssistant) -> None:
    """Setup starts and stores one client; unload closes it after platforms."""
    entry = make_entry()
    client = AsyncMock()
    with (
        patch("custom_components.aeriotv.AerioTVClient", return_value=client),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(),
        ) as forward,
        patch.object(
            hass.config_entries,
            "async_unload_platforms",
            new=AsyncMock(return_value=True),
        ) as unload,
    ):
        assert await async_setup_entry(hass, entry)
        assert entry.runtime_data is client
        client.start.assert_awaited_once()
        forward.assert_awaited_once_with(entry, PLATFORMS)

        assert await async_unload_entry(hass, entry)
        unload.assert_awaited_once_with(entry, PLATFORMS)
        client.disconnect.assert_awaited_once()


async def test_setup_rejects_invalid_authentication(hass: HomeAssistant) -> None:
    """Revoked credentials start Home Assistant's repair flow."""
    entry = make_entry()
    client = AsyncMock()
    client.start.side_effect = AerioTVAuthError("revoked")
    with patch("custom_components.aeriotv.AerioTVClient", return_value=client):
        with pytest.raises(ConfigEntryAuthFailed):
            await async_setup_entry(hass, entry)


async def test_setup_loads_entity_while_device_is_offline(hass: HomeAssistant) -> None:
    """A closed TV app is represented by an unavailable loaded entity."""
    entry = make_entry()
    client = AsyncMock()
    client.state.connected = False
    client._reconnect_task = AsyncMock()
    with (
        patch("custom_components.aeriotv.AerioTVClient", return_value=client),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(),
        ) as forward,
    ):
        assert await async_setup_entry(hass, entry)

    client.start.assert_awaited_once()
    forward.assert_awaited_once_with(entry, PLATFORMS)
    assert entry.runtime_data is client


async def test_platform_forward_failure_disconnects_client(hass: HomeAssistant) -> None:
    """A partially configured entry cannot leak its supervised connection."""
    entry = make_entry()
    client = AsyncMock()
    with (
        patch("custom_components.aeriotv.AerioTVClient", return_value=client),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(side_effect=RuntimeError("platform failed")),
        ),
        pytest.raises(RuntimeError, match="platform failed"),
    ):
        await async_setup_entry(hass, entry)

    client.disconnect.assert_awaited_once()


async def test_runtime_auth_callback_starts_reauth(hass: HomeAssistant) -> None:
    """The client callback starts repair on the same config entry."""
    entry = make_entry()
    client = AsyncMock()
    with (
        patch("custom_components.aeriotv.AerioTVClient", return_value=client) as client_class,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new=AsyncMock(),
        ),
        patch.object(entry, "async_start_reauth", new=AsyncMock()) as start_reauth,
    ):
        await async_setup_entry(hass, entry)
        client_class.call_args.args[4]()
        await hass.async_block_till_done()

    start_reauth.assert_awaited_once_with(hass)
