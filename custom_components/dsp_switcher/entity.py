"""Shared entity base for the DSP Switcher Audio Console.

Every entity of a config entry belongs to one Home Assistant device -- the
console itself -- so the device registry shows a single "Audio Console" with
its zones, sources and transport grouped underneath.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import DspSwitcherCoordinator


class DspSwitcherEntity(CoordinatorEntity[DspSwitcherCoordinator]):
    """Base class wiring device info and a stable unique id."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: DspSwitcherCoordinator, kind: str, key: str | int
    ) -> None:
        """Build the entity's unique id as ``{entry_id}_{kind}_{key}``."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{kind}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            manufacturer="Ashoka Makerspace",
            model="dsp-switcher gateway (Biamp Tesira)",
            name="Audio Console",
            configuration_url=coordinator.client.base_url,
        )

    @property
    def available(self) -> bool:
        """Report unavailable when polling fails or the DSP link is down."""
        connected = bool((self.coordinator.data or {}).get("connected"))
        return super().available and connected
