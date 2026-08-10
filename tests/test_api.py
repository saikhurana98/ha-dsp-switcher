"""End-to-end client tests against a real aiohttp server standing in for the gateway.

Everything runs through ``asyncio.run`` in plain sync test functions, so the
suite needs nothing beyond ``pytest`` and ``aiohttp``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
from typing import Any

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from conftest import api
import pytest

TOKEN = "dsp_testtoken"

SNAPSHOT: dict[str, Any] = {
    "time": "2026-08-10T12:00:00Z",
    "connected": True,
    "device": "tesira.local",
    "master": 80,
    "zones": [
        {
            "output": 1,
            "name": "Workshop",
            "volumePct": 75.0,
            "volumeDb": -15.0,
            "muted": False,
            "sends": [{"input": 5, "name": "Spotify", "levelPct": 100.0}],
        }
    ],
    "sources": [
        {
            "input": 5,
            "name": "Spotify",
            "type": "spotify",
            "host": "media01",
            "enabled": True,
            "trimPct": 100.0,
            "trimDb": 0.0,
            "live": {
                "status": "streaming",
                "name": "Makerspace",
                "title": "Track",
                "artist": "Artist",
                "album": "Album",
                "artUrl": "https://i.scdn.co/image/abc",
            },
        }
    ],
    "nowPlaying": {
        "available": True,
        "status": "Playing",
        "title": "Track",
        "artist": "Artist",
        "album": "Album",
        "artUrl": "https://i.scdn.co/image/abc",
        "device": "Makerspace",
        "controller": "Sai",
    },
}


def build_app(
    received: list[dict[str, Any]],
    handshakes: list[dict[str, Any]] | None = None,
) -> web.Application:
    """Build a stand-in gateway mirroring the real routing and auth behaviour."""

    def authed(request: web.Request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {TOKEN}"

    async def state(request: web.Request) -> web.Response:
        return web.json_response(SNAPSHOT)

    async def session(request: web.Request) -> web.Response:
        # Public route: a bad token yields authenticated=false, not a 401.
        ok = authed(request)
        return web.json_response(
            {
                "authEnabled": True,
                "authenticated": ok,
                "role": "member" if ok else "",
                "name": "Home Assistant" if ok else "",
                "loginPath": "/auth/login",
            }
        )

    async def command(request: web.Request) -> web.Response:
        if not authed(request):
            return web.json_response({"error": "authentication required"}, status=401)
        body = await request.json()
        received.append(body)
        if body.get("type") == "channel/level":
            return web.json_response({"error": "admin role required"}, status=403)
        if body.get("type") == "zone/level" and body.get("zone") == 99:
            return web.json_response(
                {"error": "output 99 is not a configured zone"}, status=502
            )
        if body.get("type") == "boom":
            return web.json_response(
                {"error": 'unknown command type "boom"'}, status=400
            )
        return web.json_response({"ok": True})

    async def websocket(request: web.Request) -> web.StreamResponse:
        """Stand in for GET /api/ws?stream=state.

        Mirrors the gateway's frame contract: a seeded ``state:aggregate``
        frame, an ``ack`` per command frame echoing its ``id``, and a fresh
        aggregate frame after a command changes something. A few synthetic
        command types drive the edge cases the client has to survive.
        """
        if handshakes is not None:
            handshakes.append(
                {
                    "path": request.path,
                    "query": dict(request.query),
                    "authorization": request.headers.get("Authorization"),
                }
            )
        if not authed(request):
            return web.json_response({"error": "authentication required"}, status=401)

        socket = web.WebSocketResponse()
        await socket.prepare(request)
        await socket.send_json({"type": "state:aggregate", "state": SNAPSHOT})

        async for message in socket:
            if message.type is not aiohttp.WSMsgType.TEXT:
                continue
            frame = json.loads(message.data)
            received.append(frame)
            command_id = frame.get("id")
            kind = frame.get("type")

            if kind == "silent":
                continue  # never acknowledged, to exercise the client timeout
            if kind == "bye":
                await socket.close()
                break
            if kind == "slow":
                await asyncio.sleep(0.15)
                await socket.send_json({"type": "ack", "id": command_id})
                continue
            if kind == "channel/level":
                await socket.send_json(
                    {
                        "type": "ack",
                        "id": command_id,
                        "code": 403,
                        "error": "admin role required",
                    }
                )
                continue

            # A frame type this client does not know must be ignored, not fatal.
            await socket.send_json({"type": "sources", "sources": []})
            await socket.send_json({"type": "ack", "id": command_id})
            await socket.send_json(
                {"type": "state:aggregate", "state": {**SNAPSHOT, "master": 42}}
            )

        return socket

    app = web.Application()
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/session", session)
    app.router.add_post("/api/command", command)
    app.router.add_get("/api/ws", websocket)
    return app


def with_client(
    body: Callable[[Any, list[dict[str, Any]]], Awaitable[None]],
    *,
    token: str = TOKEN,
) -> None:
    """Run ``body`` against a live stand-in gateway and a real client."""

    async def runner() -> None:
        received: list[dict[str, Any]] = []
        server = TestServer(build_app(received))
        await server.start_server()
        try:
            async with aiohttp.ClientSession() as session:
                client = api.DspSwitcherClient(session, str(server.make_url("")), token)
                await body(client, received)
        finally:
            await server.close()

    asyncio.run(runner())


def test_get_state_returns_the_snapshot() -> None:
    """The snapshot comes back verbatim."""

    async def body(client: Any, _received: list[dict[str, Any]]) -> None:
        data = await client.async_get_state()
        assert data["device"] == "tesira.local"
        assert data["zones"][0]["name"] == "Workshop"
        assert data["sources"][0]["live"]["status"] == "streaming"

    with_client(body)


def test_validate_accepts_a_good_token() -> None:
    """A valid bearer resolves to the token's name."""

    async def body(client: Any, _received: list[dict[str, Any]]) -> None:
        session = await client.async_validate()
        assert session["role"] == "member"
        assert session["name"] == "Home Assistant"

    with_client(body)


def test_validate_rejects_a_bad_token() -> None:
    """``authenticated: false`` on a 200 still raises AuthError."""

    async def body(client: Any, _received: list[dict[str, Any]]) -> None:
        with pytest.raises(api.AuthError):
            await client.async_validate()

    with_client(body, token="dsp_wrong")


def test_send_command_posts_the_frame() -> None:
    """The frame reaches the gateway with its fields intact."""

    async def body(client: Any, received: list[dict[str, Any]]) -> None:
        result = await client.async_send_command(
            {"type": "zone/level", "zone": 1, "level": api.pct_to_db(50)}
        )
        assert result == {"ok": True}
        assert received == [{"type": "zone/level", "zone": 1, "level": -30.0}]

    with_client(body)


def test_send_command_drops_none_fields() -> None:
    """Optional fields left as None are omitted rather than sent as null."""

    async def body(client: Any, received: list[dict[str, Any]]) -> None:
        await client.async_send_command(
            {"type": "crosspoint", "input": 5, "output": 1, "on": True, "side": None}
        )
        assert "side" not in received[0]

    with_client(body)


def test_command_401_raises_auth_error() -> None:
    """A revoked token surfaces as AuthError so reauth can be triggered."""

    async def body(client: Any, _received: list[dict[str, Any]]) -> None:
        with pytest.raises(api.AuthError):
            await client.async_send_command({"type": "master", "master": 50})

    with_client(body, token="dsp_wrong")


def test_command_403_raises_auth_error() -> None:
    """Admin-only frames are refused for tokens; the gateway's text is kept."""

    async def body(client: Any, _received: list[dict[str, Any]]) -> None:
        with pytest.raises(api.AuthError, match="admin role required"):
            await client.async_send_command(
                {"type": "channel/level", "channel": 1, "level": -6}
            )

    with_client(body)


def test_command_502_raises_command_error_with_server_text() -> None:
    """Device errors carry the gateway's own message and status."""

    async def body(client: Any, _received: list[dict[str, Any]]) -> None:
        with pytest.raises(api.CommandError) as excinfo:
            await client.async_send_command(
                {"type": "zone/level", "zone": 99, "level": -10}
            )
        assert "not a configured zone" in str(excinfo.value)
        assert excinfo.value.status == 502

    with_client(body)


def test_command_400_raises_command_error() -> None:
    """An unknown command type is a 400 with the gateway's wording."""

    async def body(client: Any, _received: list[dict[str, Any]]) -> None:
        with pytest.raises(api.CommandError, match="unknown command type"):
            await client.async_send_command({"type": "boom"})

    with_client(body)


def test_unreachable_host_raises_cannot_connect() -> None:
    """A dead endpoint becomes CannotConnect, not a bare aiohttp error."""

    async def runner() -> None:
        async with aiohttp.ClientSession() as session:
            client = api.DspSwitcherClient(
                session, "http://127.0.0.1:1", TOKEN, timeout=2
            )
            with pytest.raises(api.CannotConnect):
                await client.async_get_state()

    asyncio.run(runner())


def test_base_url_trailing_slash_is_stripped() -> None:
    """The client tolerates a trailing slash it was handed anyway."""

    async def runner() -> None:
        async with aiohttp.ClientSession() as session:
            client = api.DspSwitcherClient(session, "http://host:9000/", TOKEN)
            assert client.base_url == "http://host:9000"

    asyncio.run(runner())


# -- WebSocket push layer ---------------------------------------------------


class WsHarness:
    """Everything one websocket test needs, collected in a single object."""

    def __init__(self) -> None:
        """Start empty; the runner fills the fields in before the body runs."""
        self.client: Any = None
        self.ws: Any = None
        self.states: list[dict[str, Any]] = []
        self.drops: list[str] = []
        self.received: list[dict[str, Any]] = []
        self.handshakes: list[dict[str, Any]] = []

    async def wait_states(self, count: int, timeout: float = 2.0) -> None:
        """Block until ``count`` aggregate frames have been dispatched."""
        deadline = asyncio.get_running_loop().time() + timeout
        while len(self.states) < count:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(
                    f"only {len(self.states)} of {count} state frames arrived"
                )
            await asyncio.sleep(0.01)


def with_ws(
    body: Callable[[WsHarness], Awaitable[None]],
    *,
    token: str = TOKEN,
    command_timeout: float = 5.0,
    connect: bool = True,
) -> None:
    """Run ``body`` against a live stand-in gateway and a real DspWsClient."""

    async def runner() -> None:
        harness = WsHarness()
        server = TestServer(build_app(harness.received, harness.handshakes))
        await server.start_server()
        try:
            async with aiohttp.ClientSession() as session:
                harness.client = api.DspSwitcherClient(
                    session, str(server.make_url("")), token
                )
                harness.ws = api.DspWsClient(
                    harness.client,
                    on_state=harness.states.append,
                    on_disconnect=harness.drops.append,
                    command_timeout=command_timeout,
                )
                try:
                    if connect:
                        await harness.ws.async_connect()
                    await body(harness)
                finally:
                    await harness.ws.close()
        finally:
            await server.close()

    asyncio.run(runner())


def test_ws_url_derives_scheme_and_query() -> None:
    """Plain http becomes ws, https becomes wss, and a base path survives."""
    assert api.ws_url("http://host:9000") == "ws://host:9000/api/ws?stream=state"
    assert api.ws_url("https://host") == "wss://host/api/ws?stream=state"
    assert api.ws_url("http://host/") == "ws://host/api/ws?stream=state"
    assert api.ws_url("https://host/dsp") == "wss://host/dsp/api/ws?stream=state"


def test_ws_handshake_carries_path_query_and_bearer() -> None:
    """The client opens /api/ws?stream=state with the bearer attached."""

    async def body(h: WsHarness) -> None:
        assert h.ws.connected
        assert h.handshakes[0]["path"] == "/api/ws"
        assert h.handshakes[0]["query"] == {"stream": "state"}
        assert h.handshakes[0]["authorization"] == f"Bearer {TOKEN}"

    with_ws(body)


def test_ws_handshake_401_raises_auth_error() -> None:
    """A refused token fails the handshake as AuthError, so reauth can start."""

    async def body(h: WsHarness) -> None:
        with pytest.raises(api.AuthError):
            await h.ws.async_connect()
        assert not h.ws.connected

    with_ws(body, token="dsp_wrong", connect=False)


def test_ws_unreachable_host_raises_cannot_connect() -> None:
    """A dead endpoint is CannotConnect, never a bare aiohttp error."""

    async def runner() -> None:
        async with aiohttp.ClientSession() as session:
            client = api.DspSwitcherClient(session, "http://127.0.0.1:1", TOKEN)
            ws = api.DspWsClient(client, on_state=lambda _state: None)
            with pytest.raises(api.CannotConnect):
                await ws.async_connect()

    asyncio.run(runner())


def test_ws_seed_frame_reaches_on_state() -> None:
    """The aggregate frame's nested state is handed over verbatim."""

    async def body(h: WsHarness) -> None:
        await h.wait_states(1)
        assert h.states[0]["device"] == "tesira.local"
        assert h.states[0]["zones"][0]["name"] == "Workshop"

    with_ws(body)


def test_ws_command_acked_and_followed_by_a_push() -> None:
    """A command round-trips, and its resulting state arrives unprompted."""

    async def body(h: WsHarness) -> None:
        await h.wait_states(1)
        await h.ws.async_command({"type": "zone/level", "zone": 1, "level": -30.0})
        assert h.received[0] == {
            "type": "zone/level",
            "zone": 1,
            "level": -30.0,
            "id": 1,
        }
        # The unknown "sources" frame the stub interleaves must be ignored.
        await h.wait_states(2)
        assert h.states[1]["master"] == 42

    with_ws(body)


def test_ws_command_drops_none_fields() -> None:
    """Optional fields left as None never reach the wire."""

    async def body(h: WsHarness) -> None:
        await h.ws.async_command(
            {"type": "crosspoint", "input": 5, "output": 1, "on": True, "side": None}
        )
        assert "side" not in h.received[0]

    with_ws(body)


def test_ws_error_ack_raises_command_error_with_server_text() -> None:
    """An ack carrying an error becomes CommandError with the gateway's words."""

    async def body(h: WsHarness) -> None:
        with pytest.raises(api.CommandError) as excinfo:
            await h.ws.async_command({"type": "channel/level", "channel": 1})
        assert "admin role required" in str(excinfo.value)
        assert excinfo.value.status == 403

    with_ws(body)


def test_ws_acks_are_matched_by_id_not_arrival_order() -> None:
    """Two in-flight commands resolve independently, even acked out of order."""

    async def body(h: WsHarness) -> None:
        slow = asyncio.create_task(h.ws.async_command({"type": "slow"}))
        await asyncio.sleep(0.02)
        fast = asyncio.create_task(h.ws.async_command({"type": "master", "master": 5}))
        await asyncio.gather(slow, fast)
        assert [frame["id"] for frame in h.received] == [1, 2]
        assert [frame["type"] for frame in h.received] == ["slow", "master"]

    with_ws(body)


def test_ws_unacknowledged_command_times_out_as_cannot_connect() -> None:
    """A command the gateway never answers fails rather than hanging forever."""

    async def body(h: WsHarness) -> None:
        with pytest.raises(api.CannotConnect, match="not acknowledged"):
            await h.ws.async_command({"type": "silent"})

    with_ws(body, command_timeout=0.2)


def test_ws_close_fails_pending_commands() -> None:
    """close() settles anything still in flight instead of leaking a future."""

    async def body(h: WsHarness) -> None:
        pending = asyncio.create_task(h.ws.async_command({"type": "silent"}))
        await asyncio.sleep(0.05)
        await h.ws.close()
        with pytest.raises(api.CannotConnect):
            await pending
        assert not h.ws.connected

    with_ws(body, command_timeout=30)


def test_ws_command_without_a_connection_raises() -> None:
    """Commands are refused outright when no socket is open."""

    async def body(h: WsHarness) -> None:
        with pytest.raises(api.CannotConnect, match="not connected"):
            await h.ws.async_command({"type": "master", "master": 5})

    with_ws(body, connect=False)


def test_ws_server_close_reports_a_disconnect() -> None:
    """A socket the gateway drops fires on_disconnect so the caller can retry."""

    async def body(h: WsHarness) -> None:
        await h.wait_states(1)
        with pytest.raises(api.CannotConnect):
            await h.ws.async_command({"type": "bye"})
        deadline = asyncio.get_running_loop().time() + 2.0
        while not h.drops and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert h.drops, "on_disconnect was never called"
        assert not h.ws.connected

    with_ws(body, command_timeout=1.0)


def test_ws_deliberate_close_reports_no_disconnect() -> None:
    """Shutting the socket down on purpose is not a disconnect to recover from."""

    async def body(h: WsHarness) -> None:
        await h.wait_states(1)
        await h.ws.close()
        await asyncio.sleep(0.05)
        assert h.drops == []

    with_ws(body)
