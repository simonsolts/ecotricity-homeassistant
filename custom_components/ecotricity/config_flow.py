"""Config flow for the Ecotricity integration."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import EcotricityAuthError, EcotricityClient, EcotricityError, Premise
from .const import CONF_ACCOUNT_ID, CONF_ACCOUNT_NUMBER, CONF_PROPERTY_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)

CONF_PREMISE = "premise"

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): TextSelector(
            TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
        ),
        vol.Required(CONF_PASSWORD): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
        ),
    }
)


async def _async_get_premises(hass: HomeAssistant, username: str, password: str) -> list[Premise]:
    """Log in and return the active premises. Raise ValueError with an error key on failure."""
    session = async_create_clientsession(hass, auto_cleanup=False)
    try:
        premises = await EcotricityClient(session, username, password).get_premises()
    except EcotricityAuthError as err:
        raise ValueError("invalid_auth") from err
    except (aiohttp.ClientError, TimeoutError) as err:
        raise ValueError("cannot_connect") from err
    except EcotricityError as err:
        _LOGGER.warning("Unexpected response from the Ecotricity portal: %s", err)
        raise ValueError("unknown") from err
    finally:
        await session.close()
    return [p for p in premises if p.is_active]


class EcotricityConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Ecotricity."""

    VERSION = 1

    def __init__(self) -> None:
        self._username = ""
        self._password = ""
        self._premises: list[Premise] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the username and password."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._username = user_input[CONF_USERNAME].strip()
            self._password = user_input[CONF_PASSWORD]
            try:
                self._premises = await _async_get_premises(self.hass, self._username, self._password)
            except ValueError as err:
                errors["base"] = str(err)
            else:
                if not self._premises:
                    return self.async_abort(reason="no_premises")
                if len(self._premises) == 1:
                    return await self._async_create(self._premises[0])
                return await self.async_step_premise()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_premise(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Let the user choose a supply address."""
        if user_input is not None:
            premise = next(p for p in self._premises if p.account_id == user_input[CONF_PREMISE])
            return await self._async_create(premise)

        options = [SelectOptionDict(value=p.account_id, label=p.address or p.account_number) for p in self._premises]
        return self.async_show_form(
            step_id="premise",
            data_schema=vol.Schema({vol.Required(CONF_PREMISE): SelectSelector(SelectSelectorConfig(options=options))}),
        )

    async def _async_create(self, premise: Premise) -> ConfigFlowResult:
        await self.async_set_unique_id(premise.account_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=premise.address or f"Ecotricity {premise.account_number}",
            data={
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_ACCOUNT_ID: premise.account_id,
                CONF_PROPERTY_ID: premise.property_id,
                CONF_ACCOUNT_NUMBER: premise.account_number,
            },
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start reauth when the password stops working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for a new password."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            username = entry.data[CONF_USERNAME]
            try:
                premises = await _async_get_premises(self.hass, username, user_input[CONF_PASSWORD])
            except ValueError as err:
                errors["base"] = str(err)
            else:
                if not any(p.account_id == entry.data[CONF_ACCOUNT_ID] for p in premises):
                    return self.async_abort(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
                    )
                }
            ),
            description_placeholders={"username": entry.data[CONF_USERNAME]},
            errors=errors,
        )
