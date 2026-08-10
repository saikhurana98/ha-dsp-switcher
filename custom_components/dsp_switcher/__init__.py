"""The DSP Switcher Audio Console integration.

One config entry == one gateway. A single :class:`DspSwitcherCoordinator`
polls ``/api/state`` and every platform reads from that shared snapshot.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import DspSwitcherClient, pct_to_db
from .const import (
    ATTR_ACTION,
    ATTR_ENTRY_ID,
    ATTR_INPUT,
    ATTR_LEVEL_DB,
    ATTR_LEVEL_PCT,
    ATTR_MASTER,
    ATTR_ON,
    ATTR_OUTPUT,
    ATTR_PAYLOAD,
    ATTR_SIDE,
    ATTR_TYPE,
    CMD_CROSSPOINT,
    CMD_CROSSPOINT_LEVEL,
    CMD_MASTER_OVERWRITE,
    CMD_SOURCE_CONTROL,
    CONF_API_TOKEN,
    CONF_BASE_URL,
    DOMAIN,
    SERVICE_MASTER_OVERWRITE,
    SERVICE_SEND_COMMAND,
    SERVICE_SET_CROSSPOINT,
    SERVICE_SET_SEND_LEVEL,
    SERVICE_SOURCE_CONTROL,
    SIDES,
    TRANSPORT_ACTIONS,
)
from .coordinator import DspSwitcherConfigEntry, DspSwitcherCoordinator

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.MEDIA_PLAYER,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]

_ENTRY_SCHEMA = {vol.Optional(ATTR_ENTRY_ID): cv.string}

SET_CROSSPOINT_SCHEMA = vol.Schema(
    {
        **_ENTRY_SCHEMA,
        vol.Required(ATTR_INPUT): vol.Coerce(int),
        vol.Required(ATTR_OUTPUT): vol.Coerce(int),
        vol.Required(ATTR_ON): cv.boolean,
        vol.Optional(ATTR_SIDE): vol.In(SIDES),
    }
)

SET_SEND_LEVEL_SCHEMA = vol.Schema(
    vol.All(
        {
            **_ENTRY_SCHEMA,
            vol.Required(ATTR_INPUT): vol.Coerce(int),
            vol.Required(ATTR_OUTPUT): vol.Coerce(int),
            vol.Optional(ATTR_LEVEL_PCT): vol.All(
                vol.Coerce(float), vol.Range(min=0, max=100)
            ),
            vol.Optional(ATTR_LEVEL_DB): vol.All(
                vol.Coerce(float), vol.Range(min=-100, max=12)
            ),
            vol.Optional(ATTR_SIDE): vol.In(SIDES),
        },
        cv.has_at_least_one_key(ATTR_LEVEL_PCT, ATTR_LEVEL_DB),
    )
)

MASTER_OVERWRITE_SCHEMA = vol.Schema(
    {
        **_ENTRY_SCHEMA,
        vol.Required(ATTR_MASTER): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
    }
)

SOURCE_CONTROL_SCHEMA = vol.Schema(
    {
        **_ENTRY_SCHEMA,
        vol.Required(ATTR_INPUT): vol.Coerce(int),
        vol.Required(ATTR_ACTION): vol.In(TRANSPORT_ACTIONS),
    }
)

SEND_COMMAND_SCHEMA = vol.Schema(
    {
        **_ENTRY_SCHEMA,
        vol.Required(ATTR_TYPE): cv.string,
        vol.Optional(ATTR_PAYLOAD, default=dict): dict,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: DspSwitcherConfigEntry) -> bool:
    """Set up one gateway from a config entry."""
    client = DspSwitcherClient(
        async_get_clientsession(hass),
        entry.data[CONF_BASE_URL],
        entry.data[CONF_API_TOKEN],
    )
    coordinator = DspSwitcherCoordinator(hass, entry, client)
    # The first snapshot comes over REST so setup fails fast and visibly on a
    # dead gateway; the push socket then takes over as the live transport.
    await coordinator.async_config_entry_first_refresh()
    await coordinator.async_start_ws()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    _async_register_services(hass)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: DspSwitcherConfigEntry
) -> bool:
    """Tear down a config entry, closing the push socket first."""
    await entry.runtime_data.async_stop_ws()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(
    hass: HomeAssistant, entry: DspSwitcherConfigEntry
) -> None:
    """Reload the entry so a changed poll interval takes effect."""
    await hass.config_entries.async_reload(entry.entry_id)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the domain-level services exactly once.

    These cover the parts of the gateway's command surface that do not map
    cleanly onto an entity: the routing matrix, the one-shot master overwrite,
    per-source transport, and a raw passthrough.
    """
    if hass.services.has_service(DOMAIN, SERVICE_SET_CROSSPOINT):
        return

    def _coordinator(call: ServiceCall) -> DspSwitcherCoordinator:
        """Resolve the target gateway, defaulting to the only loaded entry."""
        entry_id = call.data.get(ATTR_ENTRY_ID)
        entries = [
            entry
            for entry in hass.config_entries.async_loaded_entries(DOMAIN)
            if hasattr(entry, "runtime_data")
        ]
        if entry_id:
            for entry in entries:
                if entry.entry_id == entry_id:
                    return entry.runtime_data
            raise ServiceValidationError(
                f"No loaded DSP Switcher config entry with id {entry_id}"
            )
        if len(entries) == 1:
            return entries[0].runtime_data
        if not entries:
            raise ServiceValidationError("No DSP Switcher config entry is loaded")
        raise ServiceValidationError(
            "Several DSP Switcher gateways are configured; pass entry_id"
        )

    async def _set_crosspoint(call: ServiceCall) -> None:
        await _coordinator(call).async_send_command(
            {
                "type": CMD_CROSSPOINT,
                "input": call.data[ATTR_INPUT],
                "output": call.data[ATTR_OUTPUT],
                "on": call.data[ATTR_ON],
                "side": call.data.get(ATTR_SIDE),
            }
        )

    async def _set_send_level(call: ServiceCall) -> None:
        if ATTR_LEVEL_DB in call.data:
            level = float(call.data[ATTR_LEVEL_DB])
        else:
            level = pct_to_db(float(call.data[ATTR_LEVEL_PCT]))
        await _coordinator(call).async_send_command(
            {
                "type": CMD_CROSSPOINT_LEVEL,
                "input": call.data[ATTR_INPUT],
                "output": call.data[ATTR_OUTPUT],
                "level": level,
                "side": call.data.get(ATTR_SIDE),
            }
        )

    async def _master_overwrite(call: ServiceCall) -> None:
        await _coordinator(call).async_send_command(
            {"type": CMD_MASTER_OVERWRITE, "master": call.data[ATTR_MASTER]}
        )

    async def _source_control(call: ServiceCall) -> None:
        # The gateway names this field "source", not "input"
        # (internal/api/ws.go); the service keeps "input" for consistency with
        # the numbers shown in the console UI.
        await _coordinator(call).async_send_command(
            {
                "type": CMD_SOURCE_CONTROL,
                "source": call.data[ATTR_INPUT],
                "action": call.data[ATTR_ACTION],
            }
        )

    async def _send_command(call: ServiceCall) -> None:
        payload: dict[str, Any] = dict(call.data.get(ATTR_PAYLOAD) or {})
        payload["type"] = call.data[ATTR_TYPE]
        payload.pop(ATTR_ENTRY_ID, None)
        await _coordinator(call).async_send_command(payload)

    for name, handler, schema in (
        (SERVICE_SET_CROSSPOINT, _set_crosspoint, SET_CROSSPOINT_SCHEMA),
        (SERVICE_SET_SEND_LEVEL, _set_send_level, SET_SEND_LEVEL_SCHEMA),
        (SERVICE_MASTER_OVERWRITE, _master_overwrite, MASTER_OVERWRITE_SCHEMA),
        (SERVICE_SOURCE_CONTROL, _source_control, SOURCE_CONTROL_SCHEMA),
        (SERVICE_SEND_COMMAND, _send_command, SEND_COMMAND_SCHEMA),
    ):
        hass.services.async_register(DOMAIN, name, handler, schema=schema)
