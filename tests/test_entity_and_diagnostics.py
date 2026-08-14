"""Tests for AerioTV entity semantics and diagnostics privacy."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant import config_entries
from homeassistant.components.media_player import BrowseMedia, MediaPlayerEntityFeature
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
    client.seek_to_wall.assert_awaited_once_with(1_700_000_090_000)


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
    """The picker preserves Dispatcharr as a branded top-level source."""
    client = AsyncMock()
    client.state = AerioTVState(connected=True)
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass
    expected = BrowseMedia(
        title="Dispatcharr",
        media_class="directory",
        media_content_id="media-source://dispatcharr",
        media_content_type="video",
        can_play=False,
        can_expand=True,
    )
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
        root = await entity.async_browse_media()

    browse.assert_awaited_once_with(hass, "media-source://dispatcharr")
    assert root.media_content_id == ""
    assert root.title == "Media"
    assert root.children == [expected]


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


async def test_current_dispatcharr_channel_enriches_title_and_image(
    hass: HomeAssistant,
) -> None:
    """The current native UUID is enriched through Dispatcharr's public leaf."""
    channel_id = "ceaf43af-32ad-432f-9a41-465ced16e655"
    client = AsyncMock()
    client.state = AerioTVState(
        connected=True,
        channel_id=f"disp:{channel_id}",
        is_playing=True,
        can_seek=True,
    )
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass
    entity.async_write_ha_state = Mock()
    entry = AsyncMock()
    entry.state = ConfigEntryState.LOADED
    entry.entry_id = "entry-one"
    channel_leaf = SimpleNamespace(
        domain="dispatcharr",
        identifier=f"entry/entry-one/channel/{channel_id}",
        can_play=True,
        can_expand=False,
        title="SVT 1",
        thumbnail="/api/dispatcharr/entry-one/artwork/17",
    )
    programme_leaf = SimpleNamespace(
        domain="dispatcharr",
        identifier=f"entry/entry-one/programme/{channel_id}",
        can_play=False,
        can_expand=False,
        title="Synthetic bulletin",
        thumbnail=None,
    )

    with (
        patch.object(hass.config_entries, "async_entries", return_value=[entry]),
        patch(
            "custom_components.aeriotv.media_player.media_source.async_browse_media",
            AsyncMock(side_effect=[channel_leaf, programme_leaf]),
        ) as browse,
    ):
        await entity._async_refresh_media_metadata()

    assert entity.media_channel == "SVT 1"
    assert entity.media_title == "Synthetic bulletin"
    assert entity.media_image_url == "/api/dispatcharr/entry-one/artwork/17"
    assert browse.await_args_list == [
        ((hass, f"media-source://dispatcharr/entry/entry-one/channel/{channel_id}"),),
        ((hass, f"media-source://dispatcharr/entry/entry-one/programme/{channel_id}"),),
    ]


async def test_channel_change_clears_stale_metadata(hass: HomeAssistant) -> None:
    """A channel transition never leaves the prior channel's title or image."""
    client = AsyncMock()
    client.state = AerioTVState(connected=True, channel_id="disp:old")
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass
    entity.async_write_ha_state = Mock()
    entity._media_title = "Old channel"
    entity._media_image_url = "/old.png"

    client.state.channel_id = None
    entity._state_updated(client.state)

    assert entity.media_title is None
    assert entity.media_image_url is None


async def test_same_channel_refreshes_metadata_after_one_minute(
    hass: HomeAssistant,
) -> None:
    """Push updates refresh a programme after the bounded freshness interval."""
    client = AsyncMock()
    client.state = AerioTVState(connected=True, channel_id="disp:same", is_playing=True, can_seek=True)
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass
    entity.async_write_ha_state = Mock()
    entity._metadata_channel_id = "disp:same"
    entity._metadata_refreshed_at = 100.0

    with (
        patch("custom_components.aeriotv.media_player.monotonic", return_value=159.9),
        patch.object(entity, "_schedule_metadata_refresh") as refresh,
    ):
        entity._state_updated(client.state)
        refresh.assert_called_once_with()

    with (
        patch("custom_components.aeriotv.media_player.monotonic", return_value=160.0),
        patch.object(entity, "_schedule_metadata_refresh") as refresh,
    ):
        entity._state_updated(client.state)
        refresh.assert_called_once_with(force=True)


async def test_paused_channel_does_not_clear_metadata(hass: HomeAssistant) -> None:
    """A genuine pause retains its active channel and programme metadata."""
    client = AsyncMock()
    client.state = AerioTVState(connected=True, channel_id="disp:same", is_playing=False, can_seek=True)
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass
    entity.async_write_ha_state = Mock()
    entity._metadata_channel_id = "disp:same"
    entity._metadata_refreshed_at = 100.0
    entity._media_channel = "TV 8"
    entity._media_title = "Current programme"

    with patch("custom_components.aeriotv.media_player.monotonic", return_value=100.0):
        entity._state_updated(client.state)

    assert entity.media_channel == "TV 8"
    assert entity.media_title == "Current programme"


async def test_idle_snapshot_clears_metadata_and_cancels_refresh(
    hass: HomeAssistant,
) -> None:
    """The same signal that removes the seekbar clears stale now-playing data."""
    client = AsyncMock()
    client.state = AerioTVState(connected=True, channel_id="disp:same", is_playing=False, can_seek=False)
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass
    entity.async_write_ha_state = Mock()
    entity._metadata_channel_id = "disp:same"
    entity._metadata_refreshed_at = 100.0
    entity._media_channel = "TV 8"
    entity._media_title = "Previous programme"
    entity._media_image_url = "/api/dispatcharr/entry-one/artwork/17"
    refresh_task = Mock()
    entity._metadata_task = refresh_task

    entity._state_updated(client.state)

    refresh_task.cancel.assert_called_once_with()
    assert entity._metadata_task is None
    assert entity._metadata_channel_id is None
    assert entity.media_channel is None
    assert entity.media_title is None
    assert entity.media_image_url is None
    entity.async_write_ha_state.assert_called_once_with()


async def test_resume_same_channel_refreshes_after_idle(hass: HomeAssistant) -> None:
    """Resetting idle identity forces metadata lookup when playback resumes."""
    client = AsyncMock()
    client.state = AerioTVState(connected=True, channel_id="disp:same", is_playing=False, can_seek=False)
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass
    entity.async_write_ha_state = Mock()
    entity._metadata_channel_id = "disp:same"

    entity._state_updated(client.state)
    client.state.is_playing = True
    client.state.can_seek = True

    with patch.object(entity, "_schedule_metadata_refresh") as refresh:
        entity._state_updated(client.state)

    refresh.assert_called_once_with()


async def test_provider_event_cannot_repopulate_idle_metadata(
    hass: HomeAssistant,
) -> None:
    """A retained channel ID cannot restart enrichment after playback stops."""
    client = AsyncMock()
    client.state = AerioTVState(connected=True, channel_id="disp:same", is_playing=False, can_seek=False)
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass
    entity.async_write_ha_state = Mock()

    with patch.object(hass, "async_create_task") as create_task:
        entity._async_config_entry_updated()

    create_task.assert_not_called()
    assert entity.media_title is None


async def test_inflight_refresh_cannot_publish_after_idle(hass: HomeAssistant) -> None:
    """A lookup crossing an idle transition cannot restore stopped metadata."""
    channel_id = "ceaf43af-32ad-432f-9a41-465ced16e655"
    client = AsyncMock()
    client.state = AerioTVState(
        connected=True,
        channel_id=f"disp:{channel_id}",
        is_playing=True,
        can_seek=True,
    )
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass
    entity.async_write_ha_state = Mock()
    started = asyncio.Event()
    release = asyncio.Event()

    async def browse(*_args, **_kwargs):
        started.set()
        await release.wait()
        return SimpleNamespace(
            domain="dispatcharr",
            identifier=f"entry/entry-one/channel/{channel_id}",
            can_play=True,
            can_expand=False,
            title="TV 8",
            thumbnail=None,
        )

    entry = AsyncMock()
    entry.state = ConfigEntryState.LOADED
    entry.entry_id = "entry-one"
    with (
        patch.object(hass.config_entries, "async_entries", return_value=[entry]),
        patch(
            "custom_components.aeriotv.media_player.media_source.async_browse_media",
            side_effect=browse,
        ),
    ):
        task = asyncio.create_task(entity._async_refresh_media_metadata())
        await started.wait()
        client.state.is_playing = False
        client.state.can_seek = False
        release.set()
        await task

    assert entity.media_title is None
    entity.async_write_ha_state.assert_not_called()


async def test_stale_metadata_lookup_cannot_overwrite_new_channel(
    hass: HomeAssistant,
) -> None:
    """A slow old-channel lookup is discarded after a newer state arrives."""
    old_id = "ceaf43af-32ad-432f-9a41-465ced16e655"
    new_id = "7e343078-9dc6-43d8-bbad-ecb75d2d9c71"
    client = AsyncMock()
    client.state = AerioTVState(
        connected=True,
        channel_id=f"disp:{old_id}",
        is_playing=True,
        can_seek=True,
    )
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass
    entity.async_write_ha_state = Mock()
    entry = AsyncMock()
    entry.state = ConfigEntryState.LOADED
    entry.entry_id = "entry-one"
    old_started = asyncio.Event()
    release_old = asyncio.Event()

    async def browse(_hass, media_id):
        channel_id = media_id.rsplit("/", 1)[-1]
        if channel_id == old_id:
            old_started.set()
            await release_old.wait()
        return SimpleNamespace(
            domain="dispatcharr",
            identifier=f"entry/entry-one/channel/{channel_id}",
            can_play=True,
            can_expand=False,
            title="Old" if channel_id == old_id else "New",
            thumbnail=(
                "/api/dispatcharr/entry-one/artwork/17"
                if channel_id == old_id
                else "/api/dispatcharr/entry-one/artwork/18"
            ),
        )

    with (
        patch.object(hass.config_entries, "async_entries", return_value=[entry]),
        patch(
            "custom_components.aeriotv.media_player.media_source.async_browse_media",
            side_effect=browse,
        ),
    ):
        old_task = asyncio.create_task(entity._async_refresh_media_metadata())
        await old_started.wait()
        client.state.channel_id = f"disp:{new_id}"
        await entity._async_refresh_media_metadata()
        release_old.set()
        await old_task

    assert entity.media_title == "New"
    assert entity.media_image_url == "/api/dispatcharr/entry-one/artwork/18"


@pytest.mark.parametrize(
    "thumbnail",
    [
        "https://example.invalid/logo.png",
        "/api/dispatcharr/other-entry/artwork/17",
        "/api/dispatcharr/entry-one/artwork/not-an-id",
        "/api/dispatcharr/entry-one/artwork/17?token=secret",
        "/api/dispatcharr/entry-one/artwork/%31%37",
    ],
)
def test_artwork_path_validation_rejects_untrusted_urls(thumbnail: str) -> None:
    """Only the exact same-entry protected artwork route may be signed."""
    assert AerioTVMediaPlayer._validated_artwork_path("entry-one", thumbnail) is None


async def test_missing_dispatcharr_channel_leaves_metadata_empty(
    hass: HomeAssistant,
) -> None:
    """Older providers and unknown UUIDs remain a harmless soft dependency."""
    channel_id = "ceaf43af-32ad-432f-9a41-465ced16e655"
    client = AsyncMock()
    client.state = AerioTVState(connected=True, channel_id=f"disp:{channel_id}")
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass
    entity.async_write_ha_state = Mock()
    entry = AsyncMock()
    entry.state = ConfigEntryState.LOADED
    entry.entry_id = "entry-one"

    with (
        patch.object(hass.config_entries, "async_entries", return_value=[entry]),
        patch(
            "custom_components.aeriotv.media_player.media_source.async_browse_media",
            AsyncMock(side_effect=HomeAssistantError("not found")),
        ),
    ):
        await entity._async_refresh_media_metadata()

    assert entity.media_title is None
    assert entity.media_image_url is None
    entity.async_write_ha_state.assert_not_called()


async def test_duplicate_dispatcharr_matches_are_ambiguous(hass: HomeAssistant) -> None:
    """Duplicate UUIDs across providers never select arbitrary metadata."""
    channel_id = "ceaf43af-32ad-432f-9a41-465ced16e655"
    client = AsyncMock()
    client.state = AerioTVState(connected=True, channel_id=f"disp:{channel_id}")
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass
    entity.async_write_ha_state = Mock()
    entries = []
    for entry_id in ("entry-one", "entry-two"):
        entry = AsyncMock()
        entry.state = ConfigEntryState.LOADED
        entry.entry_id = entry_id
        entries.append(entry)

    async def browse(_hass, media_id):
        identifier = media_id.removeprefix("media-source://dispatcharr/")
        return SimpleNamespace(
            domain="dispatcharr",
            identifier=identifier,
            can_play=True,
            can_expand=False,
            title=identifier.split("/")[1],
            thumbnail="/logo.png",
        )

    with (
        patch.object(hass.config_entries, "async_entries", return_value=entries),
        patch(
            "custom_components.aeriotv.media_player.media_source.async_browse_media",
            side_effect=browse,
        ),
    ):
        await entity._async_refresh_media_metadata()

    assert entity.media_title is None
    assert entity.media_image_url is None
    entity.async_write_ha_state.assert_not_called()


async def test_invalid_leaf_and_unexpected_failure_are_harmless(
    hass: HomeAssistant,
) -> None:
    """Malformed and failed optional-provider responses never leak into state."""
    channel_id = "ceaf43af-32ad-432f-9a41-465ced16e655"
    client = AsyncMock()
    client.state = AerioTVState(connected=True, channel_id=f"disp:{channel_id}")
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass
    entity.async_write_ha_state = Mock()
    entries = []
    for entry_id in ("bad-leaf", "failure"):
        entry = AsyncMock()
        entry.state = ConfigEntryState.LOADED
        entry.entry_id = entry_id
        entries.append(entry)
    invalid = SimpleNamespace(
        domain="other",
        identifier="wrong",
        can_play=False,
        can_expand=True,
        title="Wrong",
        thumbnail="/wrong.png",
    )

    with (
        patch.object(hass.config_entries, "async_entries", return_value=entries),
        patch(
            "custom_components.aeriotv.media_player.media_source.async_browse_media",
            AsyncMock(side_effect=[invalid, RuntimeError("provider race")]),
        ),
    ):
        await entity._async_refresh_media_metadata()

    assert entity.media_title is None
    assert entity.media_image_url is None


async def test_media_image_fetch_uses_fresh_signed_path(hass: HomeAssistant) -> None:
    """The media-player proxy can fetch protected Dispatcharr artwork."""
    client = AsyncMock()
    client.state = AerioTVState(connected=True)
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass
    entity._media_image_url = "/api/dispatcharr/entry-one/artwork/17"
    entity._async_fetch_image_from_cache = AsyncMock(return_value=(b"image", "image/png"))

    with patch(
        "custom_components.aeriotv.media_player.async_sign_path",
        return_value="/api/dispatcharr/entry-one/artwork/17?authSig=signed",
    ) as sign:
        result = await entity.async_get_media_image()

    assert result == (b"image", "image/png")
    sign.assert_called_once()
    assert sign.call_args.args[:2] == (hass, "/api/dispatcharr/entry-one/artwork/17")
    assert sign.call_args.kwargs == {"use_content_user": True}
    assert sign.call_args.args[2].total_seconds() == 60
    entity._async_fetch_image_from_cache.assert_awaited_once_with(
        "/api/dispatcharr/entry-one/artwork/17?authSig=signed"
    )


async def test_config_entry_callback_marshals_to_event_loop(hass: HomeAssistant) -> None:
    """Worker-thread provider notifications never call async HA APIs directly."""
    client = AsyncMock()
    client.state = AerioTVState(connected=True)
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass
    entry = AsyncMock()
    entry.domain = "dispatcharr"

    with patch.object(hass, "add_job") as add_job:
        entity._config_entry_updated(Mock(), entry)

    add_job.assert_called_once_with(entity._async_config_entry_updated)


async def test_unload_awaits_blocked_metadata_task(hass: HomeAssistant) -> None:
    """Entity removal cancels and collects an in-flight metadata lookup."""
    client = AsyncMock()
    client.state = AerioTVState(connected=True, channel_id="disp:channel")
    entity = AerioTVMediaPlayer(make_entry(client))
    entity.hass = hass
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked_lookup():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = asyncio.create_task(blocked_lookup())
    entity._metadata_task = task
    await started.wait()
    await entity.async_will_remove_from_hass()

    assert task.cancelled()
    assert cancelled.is_set()
    assert entity._metadata_task is None
