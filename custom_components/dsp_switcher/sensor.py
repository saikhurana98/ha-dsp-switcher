"""Sensor entities: live status for every source that reports one."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DspSwitcherConfigEntry, DspSwitcherCoordinator
from .entity import DspSwitcherEntity

STATUS_OPTIONS = ["streaming", "idle", "offline"]

_ICONS = {
    "streaming": "mdi:play-circle",
    "idle": "mdi:pause-circle",
    "offline": "mdi:close-circle-outline",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DspSwitcherConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a status sensor for each source the gateway reports live data for.

    Sources without a ``live`` block (unmapped line inputs, or every source
    when the gateway's source-status service is disabled) get no sensor.
    """
    coordinator = entry.runtime_data
    async_add_entities(
        DspSwitcherSourceStatusSensor(
            coordinator, int(src["input"]), str(src.get("name") or src["input"])
        )
        for src in coordinator.sources
        if src.get("input") is not None and isinstance(src.get("live"), dict)
    )


class DspSwitcherSourceStatusSensor(DspSwitcherEntity, SensorEntity):
    """streaming / idle / offline for one source, with its metadata attached."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS

    def __init__(
        self, coordinator: DspSwitcherCoordinator, input_id: int, name: str
    ) -> None:
        """Bind the sensor to one matrix input."""
        super().__init__(coordinator, "status", input_id)
        self._input = input_id
        self._attr_name = f"{name} status"

    @property
    def _live(self) -> dict[str, Any]:
        source = self.coordinator.source(self._input) or {}
        live = source.get("live")
        return live if isinstance(live, dict) else {}

    @property
    def native_value(self) -> str | None:
        """Return the source's live status."""
        status = self._live.get("status")
        return str(status) if status in STATUS_OPTIONS else None

    @property
    def icon(self) -> str | None:
        """Pick an icon matching the current status."""
        status = str(self._live.get("status") or "")
        return _ICONS.get(status, "mdi:help-circle-outline")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the now-playing metadata this source advertises."""
        live = self._live
        return {
            "input": self._input,
            "advertised_name": live.get("name"),
            "title": live.get("title"),
            "artist": live.get("artist"),
            "album": live.get("album"),
            "art_url": live.get("artUrl"),
        }
