"""Diagnostics for Ecotricity. Personal data is removed."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import CONF_ACCOUNT_ID, CONF_ACCOUNT_NUMBER, CONF_PROPERTY_ID
from .coordinator import EcotricityConfigEntry

TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_ACCOUNT_ID,
    CONF_ACCOUNT_NUMBER,
    CONF_PROPERTY_ID,
    "id",
    "mpan",
    "statistic_id",
    "cost_statistic_id",
    "title",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: EcotricityConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data
    meters = []
    for meter in data.meters.values():
        meters.append(
            {
                "meter_point": asdict(meter.meter_point),
                "statistic_id": meter.statistic_id,
                "cost_statistic_id": meter.cost_statistic_id,
                "reading_count": len(meter.readings),
                "latest_readings": [asdict(r) for r in meter.readings[-3:]],
            }
        )
    return async_redact_data(
        {
            "entry": {"title": entry.title, "data": dict(entry.data)},
            "last_update_success": coordinator.last_update_success,
            "meters": meters,
            "tariffs": [asdict(t) for t in data.tariffs],
        },
        TO_REDACT,
    )
