"""Tests for AerioTV entity semantics and diagnostics privacy."""

from __future__ import annotations

from unittest.mock import AsyncMock

from homeassistant import config_entries
from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.redact import REDACTED

from custom_components.aeriotv.client import AerioTVState
from custom_components.aeriotv.const import CONF_DEVICE_ID, CONF_PORT, CONF_TOKEN, DOMAIN
from custom_components.aeriotv.diagnostics import async_get_config_entry_diagnostics
from custom_components.aeriotv.media_player import AerioTVMediaPlayer


def make_entry(client) -> config_entries.ConfigEntry:
    """Create an AerioTV config entry with runtime data."""
    entry = config_entries.ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="Private Living Room",
        data={
            CONF_HOST: "192.0.2.10",
            CONF_PORT: 43123,
            CONF_DEVICE_ID: "private-device-id",
            CONF_TOKEN: "private-token",
        },
        source=config_entries.SOURCE_ZEROCONF,
        unique_id="private-device-id",
        discovery_keys={},
        options={},
        subentries_data=[],
    )
    entry.runtime_data = client
    return entry


async def test_diagnostics_redact_identifiers(hass: HomeAssistant) -> None:
    """Diagnostics retain health facts but redact private identifiers."""
    client = AsyncMock()
    client.state = AerioTVState(
        connected=True,
        device_name="Private TV Name",
        channel_id="disp:private-channel",
        is_playing=True,
        can_seek=True,
    )
    diagnostics = await async_get_config_entry_diagnostics(hass, make_entry(client))

    assert diagnostics["entry"]["title"] == REDACTED
    assert diagnostics["entry"]["data"][CONF_HOST] == REDACTED
    assert diagnostics["entry"]["data"][CONF_DEVICE_ID] == REDACTED
    assert diagnostics["entry"]["data"][CONF_TOKEN] == REDACTED
    assert diagnostics["state"]["device_name"] == REDACTED
    assert diagnostics["state"]["channel_id"] == REDACTED
    assert diagnostics["state"]["connected"] is True
    assert diagnostics["state"]["is_playing"] is True


async def test_live_rewind_maps_to_relative_timeline() -> None:
    """Wall-clock rewind values map to HA-relative position and seek delta."""
    client = AsyncMock()
    client.state = AerioTVState(
        connected=True,
        is_playing=True,
        can_seek=True,
        position_ms=1_700_000_060_000,
        window_start_ms=1_700_000_000_000,
        window_end_ms=1_700_000_120_000,
    )
    entity = AerioTVMediaPlayer(make_entry(client))

    assert entity.media_position == 60
    assert entity.media_duration == 120
    assert entity.supported_features & MediaPlayerEntityFeature.SEEK

    await entity.async_media_seek(90)
    client.seek_by.assert_awaited_once_with(30_000)


def test_seek_feature_hidden_when_not_seekable() -> None:
    """The UI does not advertise seek when AerioTV says it is unavailable."""
    client = AsyncMock()
    client.state = AerioTVState(can_seek=False)
    entity = AerioTVMediaPlayer(make_entry(client))

    assert entity.media_position is None
    assert entity.media_duration is None
    assert not entity.supported_features & MediaPlayerEntityFeature.SEEK
