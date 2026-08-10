"""Polling coordinator for the DSP Switcher Audio Console."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import AuthError, CannotConnect, CommandError, DspSwitcherClient
from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)

type DspSwitcherConfigEntry = ConfigEntry["DspSwitcherCoordinator"]


class DspSwitcherCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch ``/api/state`` once per interval and share it with every entity."""

    config_entry: DspSwitcherConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: DspSwitcherConfigEntry,
        client: DspSwitcherClient,
    ) -> None:
        """Set up the coordinator with the entry's configured poll interval."""
        self.client = client
        interval = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=entry,
            update_interval=timedelta(seconds=interval),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Return the latest console snapshot."""
        try:
            return await self.client.async_get_state()
        except AuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (CannotConnect, CommandError) as err:
            raise UpdateFailed(str(err)) from err

    async def async_send_command(self, payload: dict[str, Any]) -> None:
        """Send a command frame, then refresh so the UI reflects it promptly.

        Errors are re-raised as :class:`HomeAssistantError` so the frontend
        shows the gateway's own message (for example ``output 9 is not a
        configured zone``) instead of a bare traceback.
        """
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
