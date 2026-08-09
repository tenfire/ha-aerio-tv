"""AerioTV media player platform."""

from __future__ import annotations

from homeassistant.components.media_player import MediaPlayerEntity, MediaPlayerEntityFeature, MediaPlayerState
from homeassistant.components.media_player.const import MediaType
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import AerioTVConfigEntry
from .client import AerioTVClient, AerioTVState
from .const import CONF_DEVICE_ID, DOMAIN

BASE_FEATURES = MediaPlayerEntityFeature.PLAY | MediaPlayerEntityFeature.PAUSE


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

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._remove_callback = self._client.add_callback(self._state_updated)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_callback:
            self._remove_callback()

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
        return features

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
