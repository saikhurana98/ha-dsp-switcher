"""Thin async client for the dsp-switcher gateway HTTP API.

Deliberately free of Home Assistant imports: the client only needs an
``aiohttp.ClientSession`` (Home Assistant supplies a shared one), which keeps
this module unit-testable with nothing more than ``pytest`` and ``aiohttp``.

Surface used here, all confirmed against the Go source:

* ``GET  /api/state``   -- public aggregate snapshot (internal/api/get.go)
* ``GET  /api/session`` -- public identity probe   (internal/api/server.go)
* ``POST /api/command`` -- one command frame, bearer required (internal/api/ws.go)
"""

from __future__ import annotations

from typing import Any

import aiohttp
from yarl import URL

from .const import (
    DB_MAX,
    DB_MIN,
    PATH_COMMAND,
    PATH_SESSION,
    PATH_STATE,
    REQUEST_TIMEOUT,
)


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


def _error_text(body: Any) -> str:
    """Pull the gateway's ``{"error": "..."}`` text out of a decoded body."""
    if isinstance(body, dict) and body.get("error"):
        return str(body["error"])
    return ""
