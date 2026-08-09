"""AerioTV media player platform."""

from __future__ import annotations

import asyncio
import logging
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
_LOGGER = logging.getLogger(__name__)


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
        self._metadata_task: asyncio.Task[None] | None = None
        self._metadata_channel_id: str | None = self._client.state.channel_id
        self._media_title: str | None = None
        self._media_image_url: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._remove_callback = self._client.add_callback(self._state_updated)
        self._remove_config_entry_callback = async_dispatcher_connect(
            self.hass, SIGNAL_CONFIG_ENTRY_CHANGED, self._config_entry_updated
        )
        self._schedule_metadata_refresh(force=True)

    async def async_will_remove_from_hass(self) -> None:
        if self._remove_callback:
            self._remove_callback()
        if self._remove_config_entry_callback:
            self._remove_config_entry_callback()
            self._remove_config_entry_callback = None
        if self._metadata_task:
            task = self._metadata_task
            self._metadata_task = None
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await super().async_will_remove_from_hass()

    def _config_entry_updated(self, change_type: ConfigEntryChange, entry: ConfigEntry) -> None:
        """Publish optional feature changes when Dispatcharr changes state."""
        if entry.domain == DISPATCHARR_DOMAIN:
            self._schedule_metadata_refresh(force=True)
            self.async_write_ha_state()

    def _state_updated(self, state: AerioTVState) -> None:
        self._schedule_metadata_refresh()
        self.async_write_ha_state()

    def _schedule_metadata_refresh(self, *, force: bool = False) -> None:
        """Refresh metadata when the native channel or provider changes."""
        channel_id = self._client.state.channel_id
        if not force and channel_id == self._metadata_channel_id:
            return
        self._metadata_channel_id = channel_id
        self._media_title = None
        self._media_image_url = None
        if self._metadata_task:
            self._metadata_task.cancel()
            self._metadata_task = None
        if self.hass is not None and channel_id and channel_id.startswith("disp:"):
            self._metadata_task = self.hass.async_create_task(
                self._async_refresh_media_metadata(),
                f"Refresh AerioTV metadata for {self.entity_id or self._attr_unique_id}",
            )

    async def _async_refresh_media_metadata(self) -> None:
        """Look up current channel metadata through Dispatcharr's public media source."""
        native_id = self._client.state.channel_id
        if native_id is None or not native_id.startswith("disp:"):
            return
        channel_id = native_id.removeprefix("disp:")
        matches: list[tuple[str, str | None]] = []
        for entry_id in self._loaded_dispatcharr_entry_ids:
            identifier = f"entry/{entry_id}/channel/{channel_id}"
            media_id = media_source.generate_media_source_id(DISPATCHARR_DOMAIN, identifier)
            try:
                leaf = await media_source.async_browse_media(self.hass, media_id)
            except (HomeAssistantError, ValueError):
                continue
            except Exception:
                _LOGGER.debug("Unexpected Dispatcharr metadata lookup failure", exc_info=True)
                continue
            if self._client.state.channel_id != native_id:
                return
            if (
                leaf.domain != DISPATCHARR_DOMAIN
                or leaf.identifier != identifier
                or leaf.can_play is not True
                or leaf.can_expand is not False
                or not isinstance(leaf.title, str)
            ):
                _LOGGER.debug("Ignoring an invalid Dispatcharr metadata leaf")
                continue
            matches.append(
                (
                    leaf.title,
                    leaf.thumbnail if isinstance(leaf.thumbnail, str) else None,
                )
            )
        if self._client.state.channel_id != native_id:
            return
        if len(matches) == 1:
            self._media_title, self._media_image_url = matches[0]
            self.async_write_ha_state()
        elif len(matches) > 1:
            _LOGGER.debug("Dispatcharr channel metadata is ambiguous across entries")
        else:
            _LOGGER.debug("No Dispatcharr metadata found for the current AerioTV channel")

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
    def _loaded_dispatcharr_entry_ids(self) -> tuple[str, ...]:
        if self.hass is None:
            return ()
        return tuple(
            entry.entry_id
            for entry in self.hass.config_entries.async_entries(DISPATCHARR_DOMAIN)
            if entry.state is ConfigEntryState.LOADED
        )

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
    def media_title(self) -> str | None:
        return self._media_title

    @property
    def media_image_url(self) -> str | None:
        return self._media_image_url

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
