"""Switch entities: one enable pill per source."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CMD_SOURCE_ENABLE
from .coordinator import DspSwitcherConfigEntry, DspSwitcherCoordinator
from .entity import DspSwitcherEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DspSwitcherConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add an enable switch for every configured source."""
    coordinator = entry.runtime_data
    async_add_entities(
        DspSwitcherSourceSwitch(
            coordinator, int(src["input"]), str(src.get("name") or src["input"])
        )
        for src in coordinator.sources
        if src.get("input") is not None
    )


class DspSwitcherSourceSwitch(DspSwitcherEntity, SwitchEntity):
    """A source's enable pill.

    This is more than a matrix mute: the gateway also starts or stops the
    source's streaming unit on its media host, so disabling a Spotify or
    AirPlay source withdraws it from mDNS and it stops being advertised as a
    target on the network. Re-enabling brings the unit -- and the advertisement
    -- back, which can take a few seconds to reappear for clients.
    """

    _attr_icon = "mdi:audio-input-rca"

    def __init__(
        self, coordinator: DspSwitcherCoordinator, input_id: int, name: str
    ) -> None:
        """Bind the switch to one matrix input."""
        super().__init__(coordinator, "source", input_id)
        self._input = input_id
        self._attr_name = f"{name} enabled"

    @property
    def _source(self) -> dict[str, Any]:
        return self.coordinator.source(self._input) or {}

    @property
    def is_on(self) -> bool | None:
        """Return whether the source is enabled."""
        value = self._source.get("enabled")
        return None if value is None else bool(value)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose the source's input number, type and media host."""
        source = self._source
        return {
            "input": self._input,
            "source_type": source.get("type"),
            "host": source.get("host"),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the source and start its streaming unit."""
        await self._async_enable(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the source and stop its streaming unit."""
        await self._async_enable(False)

    async def _async_enable(self, enabled: bool) -> None:
        # The frame's fields are "source" and "on" (internal/api/ws.go), not
        # "input"/"enabled" as the percentage-facing snapshot might suggest.
        await self.coordinator.async_send_command(
            {"type": CMD_SOURCE_ENABLE, "source": self._input, "on": enabled}
        )
