"""End-to-end client tests against a real aiohttp server standing in for the gateway.

Everything runs through ``asyncio.run`` in plain sync test functions, so the
suite needs nothing beyond ``pytest`` and ``aiohttp``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
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


def build_app(received: list[dict[str, Any]]) -> web.Application:
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

    app = web.Application()
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/session", session)
    app.router.add_post("/api/command", command)
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
