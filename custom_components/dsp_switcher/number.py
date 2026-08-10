"""Number entities: the master fader and one input trim per source."""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .api import pct_to_db
from .const import CMD_MASTER, CMD_SOURCE_LEVEL
from .coordinator import DspSwitcherConfigEntry, DspSwitcherCoordinator
from .entity import DspSwitcherEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DspSwitcherConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the master fader and a trim for every configured source."""
    coordinator = entry.runtime_data
    entities: list[NumberEntity] = [DspSwitcherMasterNumber(coordinator)]
    entities.extend(
        DspSwitcherSourceTrimNumber(
            coordinator, int(src["input"]), str(src.get("name") or src["input"])
        )
        for src in coordinator.sources
        if src.get("input") is not None
    )
    async_add_entities(entities)


class DspSwitcherMasterNumber(DspSwitcherEntity, NumberEntity):
    """The gateway's group master.

    The Tesira has no master bus: the gateway holds a per-zone ratio and moving
    this number rescales every zone proportionally, so 100 -> 0 -> 80 restores
    the original balance rather than flattening it.
    """

    _attr_name = "Master volume"
    _attr_icon = "mdi:volume-high"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: DspSwitcherCoordinator) -> None:
        """Bind the entity to the gateway master."""
        super().__init__(coordinator, "number", "master")

    @property
    def native_value(self) -> float | None:
        """Return the current master position."""
        value = (self.coordinator.data or {}).get("master")
        return None if value is None else float(value)

    async def async_set_native_value(self, value: float) -> None:
        """Move the master, rescaling every zone proportionally."""
        await self.coordinator.async_send_command(
            {"type": CMD_MASTER, "master": round(value)}
        )


class DspSwitcherSourceTrimNumber(DspSwitcherEntity, NumberEntity):
    """One source's input trim, as a 0..100 position on the console fader."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:tune-vertical"
    _attr_native_min_value = 0
    _attr_native_max_value = 100
    _attr_native_step = 1
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_mode = NumberMode.SLIDER

    def __init__(
        self, coordinator: DspSwitcherCoordinator, input_id: int, name: str
    ) -> None:
        """Bind the entity to one matrix input."""
        super().__init__(coordinator, "trim", input_id)
        self._input = input_id
        self._attr_name = f"{name} trim"

    @property
    def _source(self) -> dict[str, Any]:
        return self.coordinator.source(self._input) or {}

    @property
    def native_value(self) -> float | None:
        """Return the trim, or ``None`` before the gateway has read one."""
        value = self._source.get("trimPct")
        return None if value is None else float(value)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the raw dB the gateway holds for this trim."""
        return {"input": self._input, "trim_db": self._source.get("trimDb")}

    async def async_set_native_value(self, value: float) -> None:
        """Write the trim, converting the fader position to dB."""
        # The gateway calls this field "source", not "input" (internal/api/ws.go).
        await self.coordinator.async_send_command(
            {
                "type": CMD_SOURCE_LEVEL,
                "source": self._input,
                "level": pct_to_db(value),
            }
        )
