"""Tests for the AerioTV companion client."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientSession, web

from custom_components.aeriotv.client import (
    AerioTVAuthError,
    AerioTVClient,
    AerioTVConnectionError,
)


@pytest.fixture
async def server(unused_tcp_port, socket_enabled):
    """Run a small protocol-compatible localhost WebSocket server."""
    received = []
    channel_received = asyncio.Event()

    async def remote(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"t": "hello", "v": 1, "device": "Living room", "needsPairing": True})
        async for msg in ws:
            data = json.loads(msg.data)
            received.append(data)
            if data.get("cmd") == "setChannel":
                channel_received.set()
            if data.get("t") == "auth":
                if data.get("code") == "123456" or data.get("token") == "saved":
                    await ws.send_json({"t": "authOk", "token": "saved"})
                else:
                    await ws.send_json({"t": "authFail", "reason": "badCode"})
            elif data.get("cmd") == "getState":
                await ws.send_json(
                    {
                        "cmd": "position",
                        "channelId": "disp:abc",
                        "isPlaying": True,
                        "canSeek": True,
                        "positionWallMs": 5000,
                    }
                )
        return ws

    app = web.Application()
    app.router.add_get("/remote", remote)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", unused_tcp_port)
    await site.start()
    yield unused_tcp_port, received, channel_received
    await runner.cleanup()


@pytest.mark.asyncio
async def test_pair_and_commands(server, aiohttp_client, socket_enabled):
    """Pair, receive state, and send a channel command over localhost."""
    port, received, channel_received = server
    session = await aiohttp_client(web.Application())
    client = AerioTVClient(session.session, "127.0.0.1", port)

    await client.begin_pairing()
    assert client.state.device_name == "Living room"
    with pytest.raises(AerioTVAuthError):
        await client.submit_code("12")
    assert await client.submit_code("123456") == "saved"

    await client.request_state()
    await asyncio.sleep(0.05)
    assert client.state.channel_id == "disp:abc"
    assert client.state.is_playing

    await client.set_channel("disp:def")
    async with asyncio.timeout(1):
        await channel_received.wait()
    assert received[-1] == {"cmd": "setChannel", "channelId": "disp:def"}

    await client.seek_to_wall(1_700_000_090_000)
    await asyncio.sleep(0.05)
    assert received[-1] == {"cmd": "seekWall", "targetWallMs": 1_700_000_090_000}
    await client.disconnect()


def test_position_timestamp_changes_only_with_valid_position() -> None:
    """State-only and invalid position frames do not refresh stale position time."""
    session = AsyncMock()
    client = AerioTVClient(session, "127.0.0.1", 1234, "saved")

    client._handle({"cmd": "position", "positionWallMs": 5000})
    measured_at = client.state.position_updated_at
    assert measured_at is not None

    client._handle({"cmd": "state", "isPlaying": False, "canSeek": True})
    assert client.state.position_updated_at is measured_at

    client._handle({"cmd": "position", "positionWallMs": float("nan")})
    assert client.state.position_updated_at is measured_at


@pytest.mark.asyncio
async def test_runtime_client_stays_off_after_server_closes(unused_tcp_port, socket_enabled):
    """A closed foreground server does not cause background authentication retries."""
    connections = 0
    sockets = set()

    async def remote(request):
        nonlocal connections
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sockets.add(ws)
        connections += 1
        await ws.send_json({"t": "hello", "v": 1, "device": "Living room"})
        async for msg in ws:
            data = json.loads(msg.data)
            if data.get("t") == "auth":
                await ws.send_json({"t": "authOk", "token": "saved"})
        sockets.discard(ws)
        return ws

    app = web.Application()
    app.router.add_get("/remote", remote)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", unused_tcp_port)
    await site.start()
    session = ClientSession()
    client = AerioTVClient(session, "127.0.0.1", unused_tcp_port, "saved")

    try:
        await client.start()
        assert client.state.connected
        for ws in tuple(sockets):
            await ws.close()
        await asyncio.sleep(0.05)
        assert connections == 1
        assert not client.state.connected
    finally:
        await client.disconnect()
        await session.close()
        await runner.cleanup()

    await asyncio.sleep(0.03)
    assert connections == 1


@pytest.mark.asyncio
async def test_runtime_client_loads_offline_without_reconnecting():
    """Initial connection refusal leaves an off client without retrying."""
    session = AsyncMock()
    session.ws_connect.side_effect = OSError("app closed")
    client = AerioTVClient(session, "192.0.2.10", 43123, "saved")
    await client.start()
    assert not client.state.connected
    session.ws_connect.assert_awaited_once()
    await client.disconnect()


@pytest.mark.asyncio
async def test_auth_send_failure_is_normalized_and_cleaned_up() -> None:
    """A failed initial auth send cannot leak connection resources."""

    class FailingWebSocket:
        closed = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()
            raise StopAsyncIteration

        async def send_json(self, payload):
            raise ConnectionError("connection lost")

        async def close(self):
            self.closed = True

    websocket = FailingWebSocket()
    session = AsyncMock()
    session.ws_connect.return_value = websocket
    client = AerioTVClient(session, "example.invalid", 1, "saved")

    with pytest.raises(AerioTVConnectionError, match="connection lost"):
        await client.connect()

    await asyncio.sleep(0)
    assert websocket.closed
    assert client._ws is None
    assert client._listener is None
    assert not client._stopping
