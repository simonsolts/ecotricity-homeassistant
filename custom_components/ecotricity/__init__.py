"""The Ecotricity integration."""

from __future__ import annotations

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import EcotricityClient
from .coordinator import EcotricityConfigEntry, EcotricityCoordinator

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: EcotricityConfigEntry) -> bool:
    """Set up Ecotricity from a config entry."""
    # Each entry has its own session, so each has its own cookie jar.
    session = async_create_clientsession(hass, auto_cleanup=False)
    entry.async_on_unload(session.close)
    client = EcotricityClient(session, entry.data[CONF_USERNAME], entry.data[CONF_PASSWORD])

    coordinator = EcotricityCoordinator(hass, entry, client)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EcotricityConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
