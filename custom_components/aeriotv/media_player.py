"""AerioTV media player platform."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from time import monotonic
from typing import Any
from urllib.parse import unquote, urlsplit
from uuid import UUID

from homeassistant.components import media_source
from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)
from homeassistant.components.media_player.const import MediaType
from homeassistant.config_entries import SIGNAL_CONFIG_ENTRY_CHANGED, ConfigEntry, ConfigEntryChange, ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.trigger import PluggableAction

from . import AerioTVConfigEntry
from .client import AerioTVClient, AerioTVState
from .const import CONF_DEVICE_ID, DOMAIN
from .triggers.turn_on import async_get_turn_on_trigger

BASE_FEATURES = MediaPlayerEntityFeature.PLAY | MediaPlayerEntityFeature.PAUSE
DISPATCHARR_DOMAIN = "dispatcharr"
DISPATCHARR_MEDIA_ROOT = media_source.generate_media_source_id(DISPATCHARR_DOMAIN, "")
METADATA_REFRESH_INTERVAL = 60.0
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
        self._metadata_refreshed_at = 0.0
        self._media_channel: str | None = None
        self._media_title: str | None = None
        self._media_image_url: str | None = None
        self._turn_on_action = PluggableAction(self.async_write_ha_state)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (registry_entry := self.registry_entry) and registry_entry.device_id:
            self.async_on_remove(
                self._turn_on_action.async_register(self.hass, async_get_turn_on_trigger(registry_entry.device_id))
            )
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
        """Marshal provider lifecycle notifications onto Home Assistant's event loop."""
        if entry.domain == DISPATCHARR_DOMAIN:
            self.hass.add_job(self._async_config_entry_updated)

    @callback
    def _async_config_entry_updated(self) -> None:
        """Refresh optional metadata safely from the event loop."""
        self._schedule_metadata_refresh(force=True)
        self.async_write_ha_state()

    def _state_updated(self, state: AerioTVState) -> None:
        if not state.is_playing and not state.can_seek:
            self._clear_media_metadata()
            self.async_write_ha_state()
            return
        channel_id = state.channel_id
        if (
            channel_id
            and channel_id == self._metadata_channel_id
            and channel_id.startswith("disp:")
            and monotonic() - self._metadata_refreshed_at >= METADATA_REFRESH_INTERVAL
        ):
            self._schedule_metadata_refresh(force=True)
        else:
            self._schedule_metadata_refresh()
        self.async_write_ha_state()

    def _clear_media_metadata(self) -> None:
        """Clear now-playing metadata after an authoritative idle snapshot."""
        self._metadata_channel_id = None
        self._metadata_refreshed_at = 0.0
        self._media_channel = None
        self._media_title = None
        self._media_image_url = None
        if self._metadata_task:
            self._metadata_task.cancel()
            self._metadata_task = None

    def _metadata_playback_active(self) -> bool:
        """Return whether now-playing metadata is valid for the current state."""
        state = self._client.state
        return state.connected and (state.is_playing or state.can_seek)

    def _schedule_metadata_refresh(self, *, force: bool = False) -> None:
        """Refresh metadata when the native channel or provider changes."""
        if not self._metadata_playback_active():
            self._clear_media_metadata()
            return
        channel_id = self._client.state.channel_id
        if not force and channel_id == self._metadata_channel_id:
            return
        channel_changed = channel_id != self._metadata_channel_id
        self._metadata_channel_id = channel_id
        if channel_changed:
            self._media_channel = None
            self._media_title = None
            self._media_image_url = None
        if self._metadata_task:
            self._metadata_task.cancel()
            self._metadata_task = None
        if self.hass is not None and channel_id and channel_id.startswith("disp:"):
            self._metadata_refreshed_at = monotonic()
            self._metadata_task = self.hass.async_create_task(
                self._async_refresh_media_metadata(),
                f"Refresh AerioTV metadata for {self.entity_id or self._attr_unique_id}",
            )

    async def _async_refresh_media_metadata(self) -> None:
        """Look up current channel metadata through Dispatcharr's public media source."""
        native_id = self._client.state.channel_id
        if not self._metadata_playback_active() or native_id is None or not native_id.startswith("disp:"):
            return
        channel_id = native_id.removeprefix("disp:")
        matches: list[tuple[str, str | None, str | None]] = []
        for entry_id in self._loaded_dispatcharr_entry_ids:
            channel = await self._async_browse_metadata_leaf(entry_id, "channel", channel_id, can_play=True)
            if self._client.state.channel_id != native_id or not self._metadata_playback_active():
                return
            if channel is None:
                continue
            programme = await self._async_browse_metadata_leaf(entry_id, "programme", channel_id, can_play=False)
            if self._client.state.channel_id != native_id or not self._metadata_playback_active():
                return
            matches.append(
                (
                    channel.title,
                    programme.title if programme is not None else None,
                    self._validated_artwork_path(entry_id, channel.thumbnail),
                )
            )
        if self._client.state.channel_id != native_id or not self._metadata_playback_active():
            return
        if len(matches) == 1:
            channel, programme, image = matches[0]
            metadata = (channel, programme or channel, image)
        elif len(matches) > 1:
            _LOGGER.debug("Dispatcharr channel metadata is ambiguous across entries")
            metadata = (None, None, None)
        else:
            _LOGGER.debug("No Dispatcharr metadata found for the current AerioTV channel")
            metadata = (None, None, None)
        if self._client.state.channel_id != native_id or not self._metadata_playback_active():
            return
        if metadata != (self._media_channel, self._media_title, self._media_image_url):
            self._media_channel, self._media_title, self._media_image_url = metadata
            self.async_write_ha_state()

    async def _async_browse_metadata_leaf(self, entry_id: str, kind: str, channel_id: str, *, can_play: bool):
        """Fetch and validate one public Dispatcharr metadata leaf."""
        identifier = f"entry/{entry_id}/{kind}/{channel_id}"
        media_id = media_source.generate_media_source_id(DISPATCHARR_DOMAIN, identifier)
        try:
            leaf = await media_source.async_browse_media(self.hass, media_id)
        except (HomeAssistantError, ValueError):
            return None
        except Exception:
            _LOGGER.debug("Unexpected Dispatcharr metadata lookup failure", exc_info=True)
            return None
        if (
            leaf.domain != DISPATCHARR_DOMAIN
            or leaf.identifier != identifier
            or leaf.can_play is not can_play
            or leaf.can_expand is not False
            or not isinstance(leaf.title, str)
        ):
            _LOGGER.debug("Ignoring an invalid Dispatcharr metadata leaf")
            return None
        return leaf

    @staticmethod
    def _validated_artwork_path(entry_id: str, thumbnail: Any) -> str | None:
        """Accept only the authenticated same-origin Dispatcharr artwork route."""
        if not isinstance(thumbnail, str):
            return None
        parsed = urlsplit(thumbnail)
        expected_prefix = f"/api/dispatcharr/{entry_id}/artwork/"
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or unquote(parsed.path) != parsed.path
            or not parsed.path.startswith(expected_prefix)
            or not parsed.path.removeprefix(expected_prefix).isdigit()
        ):
            _LOGGER.debug("Ignoring an invalid Dispatcharr artwork path")
            return None
        return parsed.path

    @property
    def available(self) -> bool:
        return True

    @property
    def state(self) -> MediaPlayerState:
        if not self._client.state.connected:
            return MediaPlayerState.OFF
        return MediaPlayerState.PLAYING if self._client.state.is_playing else MediaPlayerState.PAUSED

    @property
    def supported_features(self) -> MediaPlayerEntityFeature:
        if not self._client.state.connected:
            return MediaPlayerEntityFeature.TURN_ON if self._turn_on_action else MediaPlayerEntityFeature(0)
        features = BASE_FEATURES
        if self._turn_on_action:
            features |= MediaPlayerEntityFeature.TURN_ON
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
    def media_channel(self) -> str | None:
        return self._media_channel

    @property
    def media_title(self) -> str | None:
        return self._media_title

    @property
    def media_image_url(self) -> str | None:
        return self._media_image_url

    async def async_get_media_image(self) -> tuple[bytes | None, str | None]:
        """Fetch authenticated Dispatcharr artwork through a fresh signed path."""
        if self._media_image_url is None:
            return None, None
        signed_path = async_sign_path(
            self.hass,
            self._media_image_url,
            timedelta(minutes=1),
            use_content_user=True,
        )
        return await self._async_fetch_image_from_cache(signed_path)

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

    async def async_turn_on(self) -> None:
        """Run the user-defined action for starting AerioTV."""
        if not self._turn_on_action:
            raise HomeAssistantError("No AerioTV turn-on automation is configured")
        await self._turn_on_action.async_run(self.hass, self._context)

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
