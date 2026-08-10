"""Push-first coordinator for the DSP Switcher Audio Console.

The entity-facing surface is unchanged: this is still a
:class:`DataUpdateCoordinator` whose ``data`` is the ``/api/state`` document.
What changed is where that document comes from.

* **Primary path -- WebSocket push.** A supervisor task holds ``/api/ws?
  stream=state`` open. Every ``state:aggregate`` frame is fed straight into
  ``async_set_updated_data``, so a change made in the console UI reaches Home
  Assistant as fast as the gateway can send it instead of waiting out a poll.
  The frame's payload is byte-for-byte what ``GET /api/state`` returns, which is
  why no entity needed touching.
* **Fallback path -- REST poll.** ``_async_update_data`` still fetches
  ``/api/state``. While the socket is up the poll is stretched to a slow safety
  net (``WS_FALLBACK_SCAN_INTERVAL``) so it costs almost nothing but still
  repairs the state if a push were ever missed; the moment the socket drops, the
  interval snaps back to the user's configured ``scan_interval`` and stays there
  until the supervisor reconnects. That is why the option is now described as
  governing the *fallback* poll.

Commands prefer the socket too, and do not request a refresh afterwards -- the
gateway pushes the resulting state on its own. When the socket is down they go
out over ``POST /api/command`` followed by a refresh, exactly as before.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
import logging
import random
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AuthError, CannotConnect, CommandError, DspSwitcherClient, DspWsClient
from .const import (
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    WS_FALLBACK_SCAN_INTERVAL,
    WS_RECONNECT_MAX,
    WS_RECONNECT_MIN,
)

_LOGGER = logging.getLogger(__name__)

type DspSwitcherConfigEntry = ConfigEntry["DspSwitcherCoordinator"]


class DspSwitcherCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Share one console snapshot, pushed over WebSocket, polled as a fallback."""

    config_entry: DspSwitcherConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: DspSwitcherConfigEntry,
        client: DspSwitcherClient,
    ) -> None:
        """Set up the coordinator with the entry's configured fallback interval."""
        self.client = client
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        self._poll_interval = timedelta(seconds=interval)
        self._ws = DspWsClient(
            client,
            on_state=self._handle_ws_state,
            on_disconnect=self._handle_ws_disconnect,
        )
        self._dropped = asyncio.Event()
        self._supervisor: asyncio.Task[None] | None = None
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=self._poll_interval,
        )

    # -- websocket lifecycle ------------------------------------------------

    @property
    def ws_connected(self) -> bool:
        """Return whether state is currently arriving by push."""
        return self._ws.connected

    async def async_start_ws(self) -> None:
        """Start the supervisor that keeps the push socket alive."""
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = self.config_entry.async_create_background_task(
                self.hass, self._ws_supervisor(), f"{DOMAIN}_ws"
            )

    async def async_stop_ws(self) -> None:
        """Cancel the supervisor and close the socket. Idempotent."""
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None and not supervisor.done():
            supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await supervisor
        await self._ws.close()
        self._apply_ws_interval(connected=False)

    async def _ws_supervisor(self) -> None:
        """Connect, then reconnect with exponential backoff for as long as we live.

        A refused token is terminal for the socket: it will keep being refused,
        so the loop stops and hands off to the reauth flow. The REST fallback
        keeps entities alive in the meantime because ``/api/state`` is public.
        """
        delay: float = WS_RECONNECT_MIN
        while True:
            try:
                await self._ws.async_connect()
            except AuthError as err:
                _LOGGER.warning(
                    "DSP Switcher refused the token on the push socket (%s); "
                    "falling back to polling and asking for a new token",
                    err,
                )
                self._apply_ws_interval(connected=False)
                self.config_entry.async_start_reauth(self.hass)
                return
            except CannotConnect as err:
                _LOGGER.debug("DSP Switcher push socket unavailable: %s", err)
            else:
                _LOGGER.debug("DSP Switcher push socket connected")
                delay = WS_RECONNECT_MIN
                self._dropped.clear()
                self._apply_ws_interval(connected=True)
                await self._dropped.wait()
                self._apply_ws_interval(connected=False)
                # Repair anything the drop may have hidden, immediately rather
                # than on the next scheduled poll.
                await self.async_request_refresh()

            # Jitter keeps a gateway restart from being hit by every Home
            # Assistant instance on the same tick.
            await asyncio.sleep(delay + random.uniform(0, delay / 2))
            delay = min(delay * 2, WS_RECONNECT_MAX)

    @callback
    def _handle_ws_state(self, state: dict[str, Any]) -> None:
        """Publish a pushed snapshot to every entity."""
        self.async_set_updated_data(state)

    @callback
    def _handle_ws_disconnect(self, reason: str) -> None:
        """Wake the supervisor so it reconnects, and resume the fallback poll."""
        _LOGGER.debug("DSP Switcher push socket dropped: %s", reason)
        self._dropped.set()

    def _apply_ws_interval(self, *, connected: bool) -> None:
        """Stretch the poll while pushes flow; restore it when they stop."""
        wanted = (
            timedelta(seconds=WS_FALLBACK_SCAN_INTERVAL)
            if connected
            else self._poll_interval
        )
        if self.update_interval != wanted:
            self.update_interval = wanted

    # -- coordinator surface ------------------------------------------------

    async def _async_update_data(self) -> dict[str, Any]:
        """Return the latest console snapshot over REST (the fallback path)."""
        try:
            return await self.client.async_get_state()
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (CannotConnect, CommandError) as err:
            raise UpdateFailed(str(err)) from err

    async def async_send_command(self, payload: dict[str, Any]) -> None:
        """Send a command frame over the socket when it is up, REST otherwise.

        Errors are re-raised as :class:`HomeAssistantError` so the frontend
        shows the gateway's own message (for example ``output 9 is not a
        configured zone``) instead of a bare traceback.
        """
        if self._ws.connected:
            try:
                await self._ws.async_command(payload)
            except CommandError as err:
                raise HomeAssistantError(
                    f"DSP Switcher rejected the command: {err}"
                ) from err
            except CannotConnect as err:
                # The socket died between the check and the send; the REST path
                # below is the retry.
                _LOGGER.debug("Push command failed (%s); retrying over REST", err)
            else:
                # No refresh: the gateway pushes the resulting state itself.
                return

        try:
            await self.client.async_send_command(payload)
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except CannotConnect as err:
            raise HomeAssistantError(f"DSP Switcher unreachable: {err}") from err
        except CommandError as err:
            raise HomeAssistantError(
                f"DSP Switcher rejected the command: {err}"
            ) from err
        await self.async_request_refresh()

    # -- snapshot accessors -------------------------------------------------

    @property
    def device_host(self) -> str:
        """Return the DSP host reported by the gateway."""
        return str((self.data or {}).get("device") or "")

    @property
    def zones(self) -> list[dict[str, Any]]:
        """Return the zone list from the latest snapshot."""
        return list((self.data or {}).get("zones") or [])

    @property
    def sources(self) -> list[dict[str, Any]]:
        """Return the source list from the latest snapshot."""
        return list((self.data or {}).get("sources") or [])

    @property
    def now_playing(self) -> dict[str, Any] | None:
        """Return the now-playing block, or ``None`` when Spotify is disabled."""
        value = (self.data or {}).get("nowPlaying")
        return value if isinstance(value, dict) else None

    def zone(self, output: int) -> dict[str, Any] | None:
        """Return one zone by its matrix output number."""
        for zone in self.zones:
            if zone.get("output") == output:
                return zone
        return None

    def source(self, input_id: int) -> dict[str, Any] | None:
        """Return one source by its matrix input number."""
        for source in self.sources:
            if source.get("input") == input_id:
                return source
        return None

    def source_names(self) -> list[str]:
        """Return every configured source name, in gateway order."""
        return [str(src.get("name", "")) for src in self.sources if src.get("name")]

    def input_for_name(self, name: str) -> int | None:
        """Resolve a source display name back to its matrix input number."""
        for source in self.sources:
            if source.get("name") == name:
                value = source.get("input")
                return int(value) if value is not None else None
        return None
