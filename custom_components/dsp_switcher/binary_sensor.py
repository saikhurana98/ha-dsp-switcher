"""Binary sensor: the gateway's TTP link to the Tesira."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import DspSwitcherConfigEntry, DspSwitcherCoordinator
from .entity import DspSwitcherEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: DspSwitcherConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the DSP connectivity sensor."""
    async_add_entities([DspSwitcherConnectedBinarySensor(entry.runtime_data)])


class DspSwitcherConnectedBinarySensor(DspSwitcherEntity, BinarySensorEntity):
    """Whether the gateway currently holds a session with the DSP."""

    _attr_name = "DSP connected"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator: DspSwitcherCoordinator) -> None:
        """Bind the sensor to the snapshot's ``connected`` flag."""
        super().__init__(coordinator, "binary_sensor", "connected")

    @property
    def available(self) -> bool:
        """Stay available whenever the gateway itself answers.

        The shared base marks entities unavailable while the DSP link is down,
        which is exactly the condition this sensor exists to report -- so it
        deliberately opts out of that rule.
        """
        return self.coordinator.last_update_success

    @property
    def is_on(self) -> bool:
        """Return whether the DSP link is up."""
        return bool((self.coordinator.data or {}).get("connected"))
