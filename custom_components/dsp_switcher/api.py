"""Thin async client for the dsp-switcher gateway HTTP API.

Deliberately free of Home Assistant imports: the client only needs an
``aiohttp.ClientSession`` (Home Assistant supplies a shared one), which keeps
this module unit-testable with nothing more than ``pytest`` and ``aiohttp``.

Surface used here, all confirmed against the Go source:

* ``GET  /api/state``   -- public aggregate snapshot (internal/api/get.go)
* ``GET  /api/session`` -- public identity probe   (internal/api/server.go)
* ``POST /api/command`` -- one command frame, bearer required (internal/api/ws.go)
* ``GET  /api/ws?stream=state`` -- push socket, bearer required (internal/api/ws.go)

The socket is the preferred transport: it pushes the very same document
``/api/state`` returns, so nothing downstream has to care which one produced the
snapshot it is looking at. REST stays as the fallback path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import json
import logging
from typing import Any

import aiohttp
from yarl import URL

from .const import (
    DB_MAX,
    DB_MIN,
    FRAME_ACK,
    FRAME_STATE_AGGREGATE,
    PATH_COMMAND,
    PATH_SESSION,
    PATH_STATE,
    PATH_WS,
    REQUEST_TIMEOUT,
    WS_COMMAND_TIMEOUT,
    WS_CONNECT_TIMEOUT,
    WS_HEARTBEAT,
    WS_STREAM_QUERY,
)

_LOGGER = logging.getLogger(__name__)


class DspSwitcherError(Exception):
    """Base error for every failure raised by this client."""


class CannotConnect(DspSwitcherError):
    """The gateway could not be reached, or answered something unparseable."""


class AuthError(DspSwitcherError):
    """The bearer token was missing, revoked, expired or lacks the role."""


class CommandError(DspSwitcherError):
    """The gateway rejected a command frame; carries the server's own text."""

    def __init__(self, message: str, status: int | None = None) -> None:
        """Keep the HTTP status alongside the gateway's error string."""
        super().__init__(message)
        self.status = status


def db_to_pct(db: float) -> float:
    """Convert a level in dB to its 0..100 fader position, clamped.

    Mirrors ``matrix.PctOfDb``: ``(db - (-60)) / (0 - (-60)) * 100``.
    """
    pct = (db - DB_MIN) / (DB_MAX - DB_MIN) * 100
    return min(100.0, max(0.0, pct))


def pct_to_db(pct: float) -> float:
    """Convert a 0..100 fader position back to dB, clamped.

    Mirrors ``matrix.zoneDb``: ``-60 + pct/100 * 60``, i.e. ``pct * 0.6 - 60``.
    """
    pct = min(100.0, max(0.0, pct))
    return DB_MIN + pct / 100 * (DB_MAX - DB_MIN)


def normalize_base_url(raw: str) -> str:
    """Validate and canonicalise a user-supplied base URL.

    Trailing slashes are stripped and an http/https scheme with a host is
    required, so ``{base}{path}`` concatenation is always well formed.
    """
    candidate = (raw or "").strip().rstrip("/")
    if not candidate:
        raise ValueError("base URL is empty")
    url = URL(candidate)
    if url.scheme not in ("http", "https"):
        raise ValueError("base URL must start with http:// or https://")
    if not url.host:
        raise ValueError("base URL has no host")
    return candidate


def ws_url(base_url: str) -> str:
    """Derive the aggregate-stream WebSocket URL from an HTTP base URL.

    Only the scheme changes (``https`` -> ``wss``, anything else -> ``ws``); any
    path prefix on the base URL is preserved, matching how the REST paths are
    concatenated elsewhere in this module.
    """
    base = str(
        URL(base_url.rstrip("/")).with_scheme(
            "wss" if URL(base_url).scheme == "https" else "ws"
        )
    ).rstrip("/")
    return f"{base}{PATH_WS}?{WS_STREAM_QUERY}"


class DspSwitcherClient:
    """Minimal client over the gateway's three HTTP endpoints."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        base_url: str,
        token: str,
        *,
        timeout: float = REQUEST_TIMEOUT,
    ) -> None:
        """Store the session and the (already normalised) connection details."""
        self._session = session
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    @property
    def base_url(self) -> str:
        """Return the gateway base URL, without a trailing slash."""
        return self._base_url

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        auth: bool = False,
    ) -> Any:
        """Perform one request and decode its JSON body.

        Network and decode failures become :class:`CannotConnect`; 401/403
        become :class:`AuthError`; any other non-2xx becomes
        :class:`CommandError` carrying the gateway's ``error`` string.
        """
        headers = self._auth_headers() if auth else {}
        try:
            async with self._session.request(
                method,
                f"{self._base_url}{path}",
                json=json,
                headers=headers,
                timeout=self._timeout,
            ) as resp:
                status = resp.status
                try:
                    body = await resp.json(content_type=None)
                except (ValueError, aiohttp.ClientError) as err:
                    if status >= 400:
                        raise CommandError(f"HTTP {status}", status) from err
                    raise CannotConnect(
                        f"{method} {path}: response was not JSON"
                    ) from err
        except TimeoutError as err:
            raise CannotConnect(f"{method} {path}: timed out") from err
        except aiohttp.ClientError as err:
            raise CannotConnect(f"{method} {path}: {err}") from err

        if status in (401, 403):
            raise AuthError(_error_text(body) or f"HTTP {status}")
        if status >= 400:
            raise CommandError(_error_text(body) or f"HTTP {status}", status)
        return body

    async def async_get_state(self) -> dict[str, Any]:
        """Fetch the aggregate console snapshot.

        ``/api/state`` is deliberately public on the gateway, so no bearer is
        attached: polling keeps working even while a token is being rotated,
        and a revoked token surfaces on the first command instead.
        """
        data = await self._request("GET", PATH_STATE)
        if not isinstance(data, dict):
            raise CannotConnect("/api/state did not return an object")
        return data

    async def async_get_session(self) -> dict[str, Any]:
        """Probe identity with the bearer attached.

        The route is public, so a bad token yields ``authenticated: false``
        rather than a 401 -- callers must check the flag, not just the status.
        """
        data = await self._request("GET", PATH_SESSION, auth=True)
        if not isinstance(data, dict):
            raise CannotConnect("/api/session did not return an object")
        return data

    async def async_validate(self) -> dict[str, Any]:
        """Verify the token, raising :class:`AuthError` when it is not accepted.

        When the gateway runs with auth disabled every request is already an
        admin, so ``authenticated`` is true and any token is accepted.
        """
        data = await self.async_get_session()
        if not data.get("authenticated"):
            raise AuthError("token was not accepted by the gateway")
        return data

    async def async_send_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Send one command frame to ``POST /api/command``."""
        frame = {k: v for k, v in payload.items() if v is not None}
        data = await self._request("POST", PATH_COMMAND, json=frame, auth=True)
        if isinstance(data, dict) and data.get("error"):
            raise CommandError(str(data["error"]))
        return data if isinstance(data, dict) else {}

    async def ws_connect(self) -> aiohttp.ClientWebSocketResponse:
        """Open the aggregate push socket, bearer attached.

        Unlike ``/api/state`` the socket is *not* public, so a refused token
        fails the HTTP handshake with 401/403 rather than yielding a body --
        that becomes :class:`AuthError`, which is what drives reauth. Every
        other failure (DNS, refused, TLS, timeout, a non-WebSocket response) is
        :class:`CannotConnect` so the caller simply retries.
        """
        url = ws_url(self._base_url)
        try:
            async with asyncio.timeout(WS_CONNECT_TIMEOUT):
                return await self._session.ws_connect(
                    url,
                    headers=self._auth_headers(),
                    heartbeat=WS_HEARTBEAT,
                )
        except aiohttp.WSServerHandshakeError as err:
            if err.status in (401, 403):
                raise AuthError(
                    f"websocket handshake refused the token (HTTP {err.status})"
                ) from err
            raise CannotConnect(f"websocket handshake failed: {err}") from err
        except TimeoutError as err:
            raise CannotConnect("websocket handshake timed out") from err
        except aiohttp.ClientError as err:
            raise CannotConnect(f"websocket connect failed: {err}") from err


class DspWsClient:
    """The gateway's ``?stream=state`` socket: pushed snapshots plus commands.

    One instance owns at most one live connection. Frames the gateway sends are
    decoded on a background listen task:

    * ``{"type": "state:aggregate", "state": {...}}`` -- the whole /api/state
      document, handed to ``on_state``.
    * ``{"type": "ack", "id": n}`` (or with ``code``/``error``) -- the reply to
      one command frame, resolving that command's pending future.
    * anything else -- ignored, so the gateway can add frame types freely.

    The class deliberately does not reconnect itself: reconnection policy (and
    the decision to fall back to polling) belongs to the coordinator, which is
    told about a dropped socket through ``on_disconnect``.
    """

    def __init__(
        self,
        client: DspSwitcherClient,
        *,
        on_state: Callable[[dict[str, Any]], None],
        on_disconnect: Callable[[str], None] | None = None,
        command_timeout: float = WS_COMMAND_TIMEOUT,
    ) -> None:
        """Bind the socket to a client and the two callbacks that drain it."""
        self._client = client
        self._on_state = on_state
        self._on_disconnect = on_disconnect
        self._command_timeout = command_timeout
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[None]] = {}
        self._next_id = 0
        self._closing = False

    @property
    def connected(self) -> bool:
        """Return whether a live socket is currently open."""
        return self._ws is not None and not self._ws.closed

    async def async_connect(self) -> None:
        """Open the socket and start the listen task.

        Raises :class:`AuthError` or :class:`CannotConnect` exactly as
        :meth:`DspSwitcherClient.ws_connect` does.
        """
        await self.close()
        self._closing = False
        ws = await self._client.ws_connect()
        self._ws = ws
        self._task = asyncio.create_task(self._listen(ws))

    async def close(self) -> None:
        """Stop listening, close the socket and fail anything still pending.

        Safe to call repeatedly and safe to call when never connected.
        """
        self._closing = True
        task, self._task = self._task, None
        ws, self._ws = self._ws, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if ws is not None and not ws.closed:
            with contextlib.suppress(Exception):
                await ws.close()
        self._fail_pending(CannotConnect("websocket was closed"))

    async def async_command(self, payload: dict[str, Any]) -> None:
        """Send one command frame and wait for its ack.

        The frame is the same JSON ``POST /api/command`` takes, plus the ``id``
        this method assigns. ``None`` values are stripped so an optional field
        (``side``) is simply absent rather than explicitly null.
        """
        ws = self._ws
        if ws is None or ws.closed:
            raise CannotConnect("websocket is not connected")

        self._next_id += 1
        command_id = self._next_id
        frame = {k: v for k, v in payload.items() if v is not None}
        frame["id"] = command_id

        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._pending[command_id] = future
        try:
            await ws.send_json(frame)
            async with asyncio.timeout(self._command_timeout):
                await future
        except TimeoutError as err:
            raise CannotConnect(
                f"command {payload.get('type')!r} was not acknowledged in time"
            ) from err
        except aiohttp.ClientError as err:
            raise CannotConnect(
                f"command {payload.get('type')!r} could not be sent: {err}"
            ) from err
        finally:
            self._pending.pop(command_id, None)

    # -- internals ----------------------------------------------------------

    async def _listen(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        """Drain frames until the socket ends, then report the disconnect.

        Cancellation (only :meth:`close` cancels this) propagates untouched and
        reports nothing: a deliberate shutdown is not a disconnect to recover
        from.
        """
        reason = "connection closed by the gateway"
        try:
            async for message in ws:
                if message.type is aiohttp.WSMsgType.TEXT:
                    self._dispatch(message.data)
                elif message.type is aiohttp.WSMsgType.ERROR:
                    reason = str(ws.exception() or "websocket error")
                    break
                elif message.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                ):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as err:  # the listen loop must never escape
            reason = str(err) or type(err).__name__

        self._fail_pending(CannotConnect(f"websocket closed: {reason}"))
        if self._on_disconnect is not None and not self._closing:
            self._on_disconnect(reason)

    def _dispatch(self, raw: str) -> None:
        """Route one decoded text frame; unknown shapes are dropped silently."""
        try:
            frame = json.loads(raw)
        except ValueError:
            _LOGGER.debug("Ignoring unparseable websocket frame")
            return
        if not isinstance(frame, dict):
            return

        kind = frame.get("type")
        if kind == FRAME_STATE_AGGREGATE:
            state = frame.get("state")
            if isinstance(state, dict):
                self._on_state(state)
            return
        if kind == FRAME_ACK:
            self._resolve_ack(frame)

    def _resolve_ack(self, frame: dict[str, Any]) -> None:
        """Settle the future for one ack frame, matched on its ``id``."""
        try:
            command_id = int(frame["id"])
        except (KeyError, TypeError, ValueError):
            return
        future = self._pending.pop(command_id, None)
        if future is None or future.done():
            return
        if frame.get("error"):
            code = frame.get("code")
            future.set_exception(
                CommandError(
                    str(frame["error"]), code if isinstance(code, int) else None
                )
            )
        else:
            future.set_result(None)

    def _fail_pending(self, error: DspSwitcherError) -> None:
        """Resolve every outstanding command with ``error``."""
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(error)


def _error_text(body: Any) -> str:
    """Pull the gateway's ``{"error": "..."}`` text out of a decoded body."""
    if isinstance(body, dict) and body.get("error"):
        return str(body["error"])
    return ""
