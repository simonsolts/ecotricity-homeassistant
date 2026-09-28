"""The Ecotricity integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import EcotricityClient

type EcotricityConfigEntry = ConfigEntry[EcotricityClient]


async def async_setup_entry(hass: HomeAssistant, entry: EcotricityConfigEntry) -> bool:
    """Set up Ecotricity from a config entry."""
    # Each entry has its own session, so each has its own cookie jar.
    session = async_create_clientsession(hass, auto_cleanup=False)
    entry.async_on_unload(session.close)
    entry.runtime_data = EcotricityClient(session, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EcotricityConfigEntry) -> bool:
    """Unload a config entry."""
    return True
