"""Diagnostics for the DSP Switcher Audio Console."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import CONF_API_TOKEN
from .coordinator import DspSwitcherConfigEntry

TO_REDACT = {CONF_API_TOKEN, "token", "api_token"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: DspSwitcherConfigEntry
) -> dict[str, Any]:
    """Return the entry config and the last snapshot, with the token redacted.

    ``/api/state`` carries no credentials, so it is included verbatim; only the
    entry's own data needs redaction.
    """
    coordinator = entry.runtime_data
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "last_update_success": coordinator.last_update_success,
        "state": coordinator.data,
    }
