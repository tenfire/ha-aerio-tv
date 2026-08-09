"""AerioTV media player platform."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import UUID

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.components.media_player.const import MediaType
from homeassistant.config_entries import SIGNAL_CONFIG_ENTRY_CHANGED, ConfigEntry, ConfigEntryChange, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AerioTVConfigEntry
from .client import AerioTVClient, AerioTVState
from .const import CONF_DEVICE_ID, DOMAIN

BASE_FEATURES = MediaPlayerEntityFeature.PLAY | MediaPlayerEntityFeature.PAUSE
DISPATCHARR_DOMAIN = "dispatcharr"
DISPATCHARR_MEDIA_ROOT = media_source.generate_media_source_id(DISPATCHARR_DOMAIN, "")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AerioTVConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([AerioTVMediaPlayer(entry)])


class AerioTVMediaPlayer(MediaPlayerEntity):
    """Represent one AerioTV app instance."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False

    def __init__(self, entry: AerioTVConfigEntry) -> None:
        self._client: AerioTVClient = entry.runtime_data
        self._attr_unique_id = entry.data[CONF_DEVICE_ID]
        self._attr_device_info = {
            "identifiers": {(DOMAIN, self._attr_unique_id)},
            "name": entry.title,
            "manufacturer": "AerioTV",
            "model": "Android TV app",
        }
        self._remove_callback = None
        self._remove_config_entry_callback = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._remove_callback = self._client.add_callback(self._state_updated)
        self._remove_config_entry_callback = async_dispatcher_connect(
            self.hass, SIGNAL_CONFIG_ENTRY_CHANGED, self._config_entry_updated
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_callback:
            self._remove_callback()
        if self._remove_config_entry_callback:
            self._remove_config_entry_callback()
            self._remove_config_entry_callback = None

    def _config_entry_updated(self, change_type: ConfigEntryChange, entry: ConfigEntry) -> None:
        """Publish optional feature changes when Dispatcharr changes state."""
        if entry.domain == DISPATCHARR_DOMAIN:
            self.async_write_ha_state()

    def _state_updated(self, state: AerioTVState) -> None:
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        return self._client.state.connected

    @property
    def state(self) -> MediaPlayerState:
        return MediaPlayerState.PLAYING if self._client.state.is_playing else MediaPlayerState.PAUSED

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        features = BASE_FEATURES
        if self._client.state.can_seek:
            features |= MediaPlayerEntityFeature.SEEK
        if self._dispatcharr_loaded:
            features |= MediaPlayerEntityFeature.PLAY_MEDIA | MediaPlayerEntityFeature.BROWSE_MEDIA
        return features

    @property
    def _loaded_dispatcharr_entry_ids(self) -> set[str]:
        if self.hass is None:
            return set()
        return {
            entry.entry_id
            for entry in self.hass.config_entries.async_entries(DISPATCHARR_DOMAIN)
            if entry.state is ConfigEntryState.LOADED
        }

    @property
    def _dispatcharr_loaded(self) -> bool:
        return bool(self._loaded_dispatcharr_entry_ids)

    @property
    def media_content_id(self) -> str | None:
        return self._client.state.channel_id

    @property
    def media_content_type(self) -> str:
        return MediaType.CHANNEL

    @property
    def media_position(self) -> float | None:
        state = self._client.state
        if not state.can_seek:
            return None
        return max(0, state.position_ms - state.window_start_ms) / 1000

    @property
    def media_duration(self) -> float | None:
        state = self._client.state
        if not state.can_seek or state.window_end_ms <= state.window_start_ms:
            return None
        return (state.window_end_ms - state.window_start_ms) / 1000

    @property
    def media_position_updated_at(self) -> datetime | None:
        if not self._client.state.can_seek:
            return None
        return self._client.state.position_updated_at

    async def async_media_play(self) -> None:
        await self._client.play()

    async def async_media_pause(self) -> None:
        await self._client.pause()

    async def async_media_play_pause(self) -> None:
        await self._client.toggle()

    async def async_media_seek(self, position: float) -> None:
        state = self._client.state
        if not state.can_seek:
            raise HomeAssistantError("The current AerioTV media is not seekable")
        duration = max(0, state.window_end_ms - state.window_start_ms) / 1000
        target_wall_ms = state.window_start_ms + int(min(max(position, 0), duration) * 1000)
        await self._client.seek_by(target_wall_ms - state.position_ms)

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ):
        """Browse the optional Dispatcharr catalogue through HA's public media source."""
        del media_content_type
        if not self._dispatcharr_loaded:
            raise HomeAssistantError("Dispatcharr media source is unavailable")
        media_id = media_content_id or DISPATCHARR_MEDIA_ROOT
        parsed = urlsplit(media_id)
        if (
            parsed.scheme != "media-source"
            or parsed.netloc != DISPATCHARR_DOMAIN
            or parsed.query
            or parsed.fragment
            or unquote(parsed.path) != parsed.path
        ):
            raise HomeAssistantError("Only Dispatcharr media can be browsed")
        return await media_source.async_browse_media(self.hass, media_id)

    async def async_play_media(
        self,
        media_type: MediaType | str,
        media_id: str,
        **kwargs: Any,
    ) -> None:
        """Select a channel from Dispatcharr's public media-source identifier."""
        del media_type, kwargs
        try:
            parsed = urlsplit(media_id)
            if parsed.scheme != "media-source" or parsed.netloc != DISPATCHARR_DOMAIN:
                raise ValueError
            if parsed.query or parsed.fragment or unquote(parsed.path) != parsed.path:
                raise ValueError
            parts = parsed.path.split("/")
            if (
                len(parts) != 5
                or parts[0]
                or parts[1] != "entry"
                or parts[2] not in self._loaded_dispatcharr_entry_ids
                or parts[3] != "channel"
            ):
                raise ValueError
            channel_id = str(UUID(parts[4]))
            if parts[4].lower() != channel_id:
                raise ValueError
        except (AttributeError, TypeError, ValueError) as err:
            raise HomeAssistantError("Unsupported Dispatcharr media identifier") from err
        await self._client.set_channel(f"disp:{channel_id}")
