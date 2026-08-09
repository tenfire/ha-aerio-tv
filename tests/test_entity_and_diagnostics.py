"""Tests for AerioTV entity semantics and diagnostics privacy."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
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
    updated_at = datetime(2026, 8, 9, 19, 51, 54, tzinfo=UTC)
    client.state = AerioTVState(
        connected=True,
        is_playing=True,
        can_seek=True,
        position_ms=1_700_000_060_000,
        window_start_ms=1_700_000_000_000,
        window_end_ms=1_700_000_120_000,
        position_updated_at=updated_at,
    )
    entity = AerioTVMediaPlayer(make_entry(client))

    assert entity.media_position == 60
    assert entity.media_duration == 120
    assert entity.media_position_updated_at == updated_at
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
    assert entity.media_position_updated_at is None
    assert not entity.supported_features & MediaPlayerEntityFeature.SEEK


def test_dispatcharr_features_are_soft_dependency(hass: HomeAssistant) -> None:
    """Browse/play are exposed only while Dispatcharr has a loaded entry."""
    client = AsyncMock()
    client.state = AerioTVState(connected=True)
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass

    assert not entity.supported_features & MediaPlayerEntityFeature.PLAY_MEDIA
    assert not entity.supported_features & MediaPlayerEntityFeature.BROWSE_MEDIA

    entry = AsyncMock()
    entry.state = ConfigEntryState.LOADED
    entry.entry_id = "entry-one"
    with patch.object(hass.config_entries, "async_entries", return_value=[entry]):
        assert entity.supported_features & MediaPlayerEntityFeature.PLAY_MEDIA
        assert entity.supported_features & MediaPlayerEntityFeature.BROWSE_MEDIA


async def test_browse_delegates_to_dispatcharr_media_source(hass: HomeAssistant) -> None:
    """Dispatcharr owns the catalogue exposed through the AerioTV picker."""
    client = AsyncMock()
    client.state = AerioTVState(connected=True)
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass
    expected = AsyncMock()
    entry = AsyncMock()
    entry.state = ConfigEntryState.LOADED
    entry.entry_id = "entry-one"

    with (
        patch.object(hass.config_entries, "async_entries", return_value=[entry]),
        patch(
            "custom_components.aeriotv.media_player.media_source.async_browse_media",
            AsyncMock(return_value=expected),
        ) as browse,
    ):
        assert await entity.async_browse_media() is expected

    browse.assert_awaited_once_with(hass, "media-source://dispatcharr")


async def test_browse_rejects_other_or_encoded_media_sources(hass: HomeAssistant) -> None:
    """Browse remains inside the exact Dispatcharr media-source namespace."""
    client = AsyncMock()
    client.state = AerioTVState(connected=True)
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass
    entry = AsyncMock()
    entry.state = ConfigEntryState.LOADED
    entry.entry_id = "entry-one"

    with patch.object(hass.config_entries, "async_entries", return_value=[entry]):
        for media_id in (
            "media-source://dispatcharr-evil/entry/one",
            "media-source://dispatcharr/entry%2Fone",
            "media-source://dispatcharr/entry/one?x=1",
        ):
            with pytest.raises(HomeAssistantError):
                await entity.async_browse_media(media_content_id=media_id)


async def test_dispatcharr_leaf_selects_native_channel(hass: HomeAssistant) -> None:
    """A public Dispatcharr leaf becomes AerioTV's native stable channel ID."""
    client = AsyncMock()
    client.state = AerioTVState(connected=True)
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass

    entry = AsyncMock()
    entry.state = ConfigEntryState.LOADED
    entry.entry_id = "entry-one"
    with patch.object(hass.config_entries, "async_entries", return_value=[entry]):
        await entity.async_play_media(
            "video/mp2t",
            "media-source://dispatcharr/entry/entry-one/channel/CEAF43AF-32AD-432F-9A41-465CED16E655",
        )

    client.set_channel.assert_awaited_once_with("disp:ceaf43af-32ad-432f-9a41-465ced16e655")


async def test_play_media_rejects_non_channel_identifiers(hass: HomeAssistant) -> None:
    """Folders, other sources, and malformed identifiers are never sent to the TV."""
    client = AsyncMock()
    client.state = AerioTVState(connected=True)
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass
    invalid = (
        "media-source://dispatcharr",
        "media-source://dispatcharr/entry/entry-one/all",
        "media-source://dispatcharr/entry/entry-one/channel/not-a-uuid",
        "media-source://other/entry/entry-one/channel/ceaf43af-32ad-432f-9a41-465ced16e655",
        "media-source://dispatcharr/entry/entry-one/channel/ceaf43af-32ad-432f-9a41-465ced16e655?x=1",
        "media-source://dispatcharr/entry/entry-one/channel/ceaf43af32ad432f9a41465ced16e655",
        "media-source://dispatcharr/entry/entry-one/channel/{ceaf43af-32ad-432f-9a41-465ced16e655}",
        "media-source://dispatcharr/entry/entry-one/channel/ceaf43af-32ad-432f-9a41-465ced16e655/",
    )
    entry = AsyncMock()
    entry.state = ConfigEntryState.LOADED
    entry.entry_id = "entry-one"

    with patch.object(hass.config_entries, "async_entries", return_value=[entry]):
        for media_id in invalid:
            with pytest.raises(HomeAssistantError):
                await entity.async_play_media("video/mp2t", media_id)

    client.set_channel.assert_not_awaited()


async def test_play_media_requires_referenced_loaded_entry(hass: HomeAssistant) -> None:
    """Direct service calls cannot bypass the optional provider lifecycle."""
    client = AsyncMock()
    client.state = AerioTVState(connected=True)
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass
    unloaded = AsyncMock()
    unloaded.state = ConfigEntryState.NOT_LOADED
    unloaded.entry_id = "entry-one"

    with patch.object(hass.config_entries, "async_entries", return_value=[unloaded]):
        with pytest.raises(HomeAssistantError):
            await entity.async_play_media(
                "video/mp2t",
                "media-source://dispatcharr/entry/entry-one/channel/ceaf43af-32ad-432f-9a41-465ced16e655",
            )

    client.set_channel.assert_not_awaited()
