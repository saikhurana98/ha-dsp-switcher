"""Config and options flow for the DSP Switcher Audio Console."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    AuthError,
    CannotConnect,
    DspSwitcherClient,
    DspSwitcherError,
    normalize_base_url,
)
from .const import (
    CONF_API_TOKEN,
    CONF_BASE_URL,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import DspSwitcherConfigEntry

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_BASE_URL): TextSelector(
            TextSelectorConfig(type=TextSelectorType.URL)
        ),
        vol.Required(CONF_API_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)

STEP_REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_TOKEN): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


class DspSwitcherConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the UI setup of a gateway.

    The token is never logged: only the exception class steers the error
    message shown to the user.
    """

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect the base URL and API token, then validate them."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                base_url = normalize_base_url(user_input[CONF_BASE_URL])
            except ValueError:
                errors["base"] = "invalid_url"
            else:
                await self.async_set_unique_id(base_url.lower())
                self._abort_if_unique_id_configured()
                name = await self._async_validate(
                    base_url, user_input[CONF_API_TOKEN], errors
                )
                if not errors:
                    return self.async_create_entry(
                        title=name or "Audio Console",
                        data={
                            CONF_BASE_URL: base_url,
                            CONF_API_TOKEN: user_input[CONF_API_TOKEN],
                        },
                        options={CONF_SCAN_INTERVAL: DEFAULT_SCAN_INTERVAL},
                    )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start the token-rotation flow after a 401."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Accept a freshly minted token for an existing entry."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            await self._async_validate(
                entry.data[CONF_BASE_URL], user_input[CONF_API_TOKEN], errors
            )
            if not errors:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_TOKEN: user_input[CONF_API_TOKEN]}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"base_url": entry.data[CONF_BASE_URL]},
        )

    async def _async_validate(
        self, base_url: str, token: str, errors: dict[str, str]
    ) -> str | None:
        """Probe ``/api/session`` with the bearer; fill ``errors`` on failure."""
        client = DspSwitcherClient(async_get_clientsession(self.hass), base_url, token)
        try:
            session = await client.async_validate()
        except AuthError:
            errors["base"] = "invalid_auth"
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except DspSwitcherError:
            _LOGGER.exception("Unexpected error validating the DSP Switcher gateway")
            errors["base"] = "unknown"
        else:
            return str(session.get("name") or "") or None
        return None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: DspSwitcherConfigEntry,
    ) -> DspSwitcherOptionsFlow:
        """Return the options flow."""
        return DspSwitcherOptionsFlow()


class DspSwitcherOptionsFlow(OptionsFlow):
    """Let the user retune the poll interval."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and store the scan interval."""
        if user_input is not None:
            return self.async_create_entry(
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])}
            )

        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=current): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_SCAN_INTERVAL,
                        max=MAX_SCAN_INTERVAL,
                        step=1,
                        mode=NumberSelectorMode.BOX,
                        unit_of_measurement="s",
                    )
                )
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
