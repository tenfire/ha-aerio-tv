"""Tests for the AerioTV companion client."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientSession, web

from custom_components.aeriotv import client as client_module
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
async def test_runtime_client_reconnects_after_server_closes(unused_tcp_port, socket_enabled, monkeypatch):
    """The managed runtime client reconnects after AerioTV returns."""
    connections = 0
    connected_twice = asyncio.Event()
    state_reconnected = asyncio.Event()
    sockets = set()

    async def remote(request):
        nonlocal connections
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sockets.add(ws)
        connections += 1
        if connections == 2:
            connected_twice.set()
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
    monkeypatch.setattr(client_module, "RECONNECT_DELAYS", (0.01,))
    session = ClientSession()
    client = AerioTVClient(session, "127.0.0.1", unused_tcp_port, "saved")

    def state_changed(state):
        if connections >= 2 and state.connected:
            state_reconnected.set()

    client.add_callback(state_changed)
    try:
        await client.start()
        assert client.state.connected
        for ws in tuple(sockets):
            await ws.close()
        async with asyncio.timeout(1):
            await connected_twice.wait()
        async with asyncio.timeout(1):
            await state_reconnected.wait()
        assert connections == 2
    finally:
        await client.disconnect()
        await session.close()
        await runner.cleanup()

    await asyncio.sleep(0.03)
    assert connections == 2


@pytest.mark.asyncio
async def test_reconnect_survives_immediate_post_auth_close(unused_tcp_port, socket_enabled, monkeypatch):
    """A reconnect that closes during authentication cannot lose supervision."""
    connections = 0
    authenticated_third_time = asyncio.Event()
    connected_third_time = asyncio.Event()
    first_socket = None

    async def remote(request):
        nonlocal connections, first_socket
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        connections += 1
        if connections == 1:
            first_socket = ws
        await ws.send_json({"t": "hello", "v": 1, "device": "Living room"})
        async for msg in ws:
            if json.loads(msg.data).get("t") != "auth":
                continue
            await ws.send_json({"t": "authOk", "token": "saved"})
            if connections == 2:
                await ws.close()
            elif connections == 3:
                authenticated_third_time.set()
        return ws

    app = web.Application()
    app.router.add_get("/remote", remote)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    monkeypatch.setattr(client_module, "RECONNECT_DELAYS", (0.01,))
    session = ClientSession()
    client = AerioTVClient(session, "127.0.0.1", unused_tcp_port, "saved")

    def state_changed(state):
        if authenticated_third_time.is_set() and state.connected:
            connected_third_time.set()

    client.add_callback(state_changed)
    try:
        await client.start()
        assert first_socket is not None
        await first_socket.close()
        async with asyncio.timeout(1):
            await connected_third_time.wait()
        assert connections == 3
        assert client.state.connected
    finally:
        await client.disconnect()
        await session.close()
        await runner.cleanup()


@pytest.mark.asyncio
async def test_runtime_client_loads_offline_and_reconnects(monkeypatch):
    """Initial connection refusal leaves a supervised unavailable client."""
    session = AsyncMock()
    session.ws_connect.side_effect = OSError("app closed")
    client = AerioTVClient(session, "192.0.2.10", 43123, "saved")
    reconnect_started = asyncio.Event()

    async def reconnect():
        reconnect_started.set()

    monkeypatch.setattr(client, "_reconnect", reconnect)

    await client.start()
    await reconnect_started.wait()

    assert not client.state.connected
    assert client._managed
    assert client._reconnect_task is not None
    await client.disconnect()


@pytest.mark.asyncio
async def test_runtime_auth_rejection_invokes_callback(unused_tcp_port, socket_enabled, monkeypatch):
    """A token revoked after setup requests reauthentication on reconnect."""
    connections = 0
    sockets = set()
    reauth_requested = asyncio.Event()

    async def remote(request):
        nonlocal connections
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sockets.add(ws)
        connections += 1
        await ws.send_json({"t": "hello", "v": 1, "device": "Living room"})
        async for msg in ws:
            if json.loads(msg.data).get("t") == "auth":
                if connections == 1:
                    await ws.send_json({"t": "authOk", "token": "saved"})
                else:
                    await ws.send_json({"t": "authFail", "reason": "revoked"})
        sockets.discard(ws)
        return ws

    app = web.Application()
    app.router.add_get("/remote", remote)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", unused_tcp_port).start()
    monkeypatch.setattr(client_module, "RECONNECT_DELAYS", (0.01,))
    session = ClientSession()
    client = AerioTVClient(
        session,
        "127.0.0.1",
        unused_tcp_port,
        "saved",
        reauth_requested.set,
    )
    try:
        await client.start()
        for ws in tuple(sockets):
            await ws.close()
        async with asyncio.timeout(1):
            await reauth_requested.wait()
        assert connections == 2
        assert not client.state.connected
    finally:
        await client.disconnect()
        await session.close()
        await runner.cleanup()


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
    assert client._reconnect_task is None
    assert not client._managed
    assert not client._stopping
