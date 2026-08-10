"""Switch entities: one enable pill per source."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import CMD_SOURCE_ENABLE, CMD_ZONE_MUTE
from .coordinator import DspSwitcherConfigEntry, DspSwitcherCoordinator
from .entity import DspSwitcherEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DspSwitcherConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add an enable switch for every configured source, plus master mute."""
    coordinator = entry.runtime_data
    entities: list[SwitchEntity] = [
        DspSwitcherSourceSwitch(
            coordinator, int(src["input"]), str(src.get("name") or src["input"])
        )
        for src in coordinator.sources
        if src.get("input") is not None
    ]
    entities.append(DspSwitcherMasterMute(coordinator))
    async_add_entities(entities)


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


class DspSwitcherMasterMute(DspSwitcherEntity, SwitchEntity):
    """Mute every zone at once, restoring the previous mix on release.

    The gateway has no master-mute command; like the console's ALL MUTE button
    this loops zone/mute over every zone. Turning on remembers which zones were
    already muted and turning off puts exactly that pattern back. The memory
    lives in this entity instance, so after a Home Assistant restart turning
    off simply unmutes everything.
    """

    _attr_name = "Master mute"
    _attr_icon = "mdi:volume-variant-off"

    def __init__(self, coordinator: DspSwitcherCoordinator) -> None:
        """Bind the switch to the whole zone set."""
        super().__init__(coordinator, "switch", "master_mute")
        self._pre_mute: dict[int, bool] | None = None

    @property
    def is_on(self) -> bool | None:
        """Return True when every zone is muted."""
        zones = self.coordinator.zones
        if not zones:
            return None
        return all(bool(z.get("muted")) for z in zones)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Mute all zones, remembering which were already muted."""
        zones = self.coordinator.zones
        self._pre_mute = {
            int(z["output"]): bool(z.get("muted")) for z in zones if "output" in z
        }
        for output in self._pre_mute:
            await self.coordinator.async_send_command(
                {"type": CMD_ZONE_MUTE, "zone": output, "mute": True}
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Restore the pre-mute pattern, or unmute everything."""
        pre = self._pre_mute or {}
        self._pre_mute = None
        for z in self.coordinator.zones:
            if "output" not in z:
                continue
            output = int(z["output"])
            await self.coordinator.async_send_command(
                {"type": CMD_ZONE_MUTE, "zone": output, "mute": pre.get(output, False)}
            )
