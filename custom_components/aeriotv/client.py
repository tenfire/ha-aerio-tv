"""Async client for AerioTV's LAN companion protocol."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aiohttp import ClientConnectionError, ClientSession, ClientWebSocketResponse, WSMsgType


class AerioTVError(Exception):
    """Base AerioTV client error."""


class AerioTVConnectionError(AerioTVError):
    """Connection failed."""


class AerioTVAuthError(AerioTVError):
    """Authentication failed."""


@dataclass(slots=True)
class AerioTVState:
    """Latest pushed player state."""

    connected: bool = False
    device_name: str | None = None
    channel_id: str | None = None
    is_playing: bool = False
    can_seek: bool = False
    is_live: bool = True
    position_ms: int = 0
    window_start_ms: int = 0
    window_end_ms: int = 0
    position_updated_at: datetime | None = None


StateCallback = Callable[[AerioTVState], None]
AuthFailureCallback = Callable[[], None]
RECONNECT_DELAYS = (1, 2, 5, 10, 30)
_LOGGER = logging.getLogger(__name__)


class AerioTVClient:
    """Maintain an authenticated WebSocket connection to one AerioTV device."""

    def __init__(
        self,
        session: ClientSession,
        host: str,
        port: int,
        token: str | None = None,
        auth_failure_callback: AuthFailureCallback | None = None,
    ) -> None:
        self._session = session
        self.host = host
        self.port = port
        self.token = token
        self._auth_failure_callback = auth_failure_callback
        self.state = AerioTVState()
        self._ws: ClientWebSocketResponse | None = None
        self._listener: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._managed = False
        self._stopping = False
        self._callbacks: set[StateCallback] = set()
        self._auth_event = asyncio.Event()
        self._auth_error: str | None = None

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        return f"ws://{host}:{self.port}/remote"

    async def connect(self, *, keep_open_for_pairing: bool = False) -> None:
        self._auth_event.clear()
        self._auth_error = None
        try:
            self._ws = await self._session.ws_connect(self.url, heartbeat=30)
        except (TimeoutError, ClientConnectionError, OSError) as err:
            raise AerioTVConnectionError(str(err)) from err
        self._listener = asyncio.create_task(self._listen())
        try:
            await self._send({"t": "auth", "token": self.token or "", "code": ""})
            async with asyncio.timeout(10):
                await self._auth_event.wait()
        except TimeoutError as err:
            await self._close_transport()
            raise AerioTVConnectionError("Timed out waiting for authentication") from err
        except AerioTVConnectionError:
            await self._close_transport()
            raise
        if self._auth_error:
            error = AerioTVAuthError(self._auth_error)
            if not keep_open_for_pairing:
                await self.disconnect()
            raise error

    async def start(self) -> None:
        """Connect and supervise reconnection for a runtime config entry."""
        self._managed = True
        self._stopping = False
        try:
            await self.connect()
        except Exception:
            await self.disconnect()
            raise

    async def begin_pairing(self) -> None:
        """Connect without credentials and wait until the TV displays a code."""
        try:
            await self.connect(keep_open_for_pairing=True)
        except AerioTVAuthError:
            return
        raise AerioTVAuthError("Device unexpectedly accepted empty credentials")

    async def submit_code(self, code: str) -> str:
        if not (code.isdigit() and len(code) == 6):
            raise AerioTVAuthError("Pairing code must contain exactly 6 digits")
        self._auth_event.clear()
        self._auth_error = None
        await self._send({"t": "auth", "token": "", "code": code})
        try:
            async with asyncio.timeout(10):
                await self._auth_event.wait()
        except TimeoutError as err:
            raise AerioTVConnectionError("Timed out waiting for pairing result") from err
        if self._auth_error:
            raise AerioTVAuthError(self._auth_error)
        if not self.token:
            raise AerioTVAuthError("Pairing succeeded without a token")
        return self.token

    async def disconnect(self) -> None:
        self._stopping = True
        self._managed = False
        reconnect, self._reconnect_task = self._reconnect_task, None
        if reconnect and reconnect is not asyncio.current_task():
            reconnect.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reconnect
        await self._close_transport()
        self.state.connected = False
        self._notify()

    async def _close_transport(self) -> None:
        """Close one connection attempt without changing supervision state."""
        task, self._listener = self._listener, None
        if task and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._ws is not None:
            await self._ws.close()
        self._ws = None

    def add_callback(self, callback: StateCallback) -> Callable[[], None]:
        self._callbacks.add(callback)
        return lambda: self._callbacks.discard(callback)

    async def play(self) -> None:
        await self.command("play")

    async def pause(self) -> None:
        await self.command("pause")

    async def toggle(self) -> None:
        await self.command("toggle")

    async def seek_by(self, delta_ms: int) -> None:
        await self.command("seekBy", deltaMs=delta_ms)

    async def set_channel(self, channel_id: str) -> None:
        await self.command("setChannel", channelId=channel_id)

    async def request_state(self) -> None:
        await self.command("getState")

    async def command(self, command: str, **values: Any) -> None:
        if not self.state.connected:
            raise AerioTVConnectionError("Not connected")
        await self._send({"cmd": command, **values})

    async def _send(self, payload: dict[str, Any]) -> None:
        if self._ws is None or self._ws.closed:
            raise AerioTVConnectionError("WebSocket is closed")
        try:
            await self._ws.send_json(payload)
        except (ClientConnectionError, ConnectionError, OSError, RuntimeError) as err:
            raise AerioTVConnectionError(str(err)) from err

    async def _listen(self) -> None:
        assert self._ws is not None
        try:
            async for message in self._ws:
                if message.type == WSMsgType.TEXT:
                    try:
                        payload = json.loads(message.data)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    self._handle(payload)
                elif message.type in (WSMsgType.CLOSED, WSMsgType.ERROR):
                    break
        finally:
            self.state.connected = False
            self._notify()
            if self._listener is asyncio.current_task():
                self._listener = None
            if self._managed and not self._stopping:
                self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        if self._reconnect_task is None or self._reconnect_task.done():
            self._reconnect_task = asyncio.create_task(self._reconnect())

    async def _reconnect(self) -> None:
        """Reconnect with bounded backoff until stopped or connected."""
        attempt = 0
        while self._managed and not self._stopping:
            await asyncio.sleep(RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)])
            try:
                await self.connect()
            except AerioTVAuthError:
                self._managed = False
                if self._auth_failure_callback is not None:
                    self._auth_failure_callback()
                return
            except AerioTVConnectionError:
                attempt += 1
                continue
            return

    def _handle(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        kind = payload.get("t")
        if kind == "hello":
            self.state.device_name = payload.get("device") or self.state.device_name
        elif kind == "authOk":
            token = payload.get("token")
            if isinstance(token, str) and token:
                self.token = token
            self.state.connected = True
            self._auth_error = None
            self._auth_event.set()
            self._notify()
        elif kind == "authFail":
            self._auth_error = str(payload.get("reason", "authentication failed"))
            self._auth_event.set()
        elif payload.get("cmd") in ("state", "position"):
            if "channelId" in payload:
                self.state.channel_id = payload.get("channelId") or None
            if "isPlaying" in payload and isinstance(payload["isPlaying"], bool):
                self.state.is_playing = payload["isPlaying"]
            if "canSeek" in payload and isinstance(payload["canSeek"], bool):
                self.state.can_seek = payload["canSeek"]
            if "isLive" in payload and isinstance(payload["isLive"], bool):
                self.state.is_live = payload["isLive"]
            for field, attribute in (
                ("positionWallMs", "position_ms"),
                ("windowStartMs", "window_start_ms"),
                ("windowEndMs", "window_end_ms"),
            ):
                value = payload.get(field)
                if (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                    and value >= 0
                ):
                    setattr(self.state, attribute, int(value))
                    if field == "positionWallMs":
                        self.state.position_updated_at = datetime.now(UTC)
            self._notify()

    def _notify(self) -> None:
        for callback in tuple(self._callbacks):
            try:
                callback(self.state)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("AerioTV state callback failed")
