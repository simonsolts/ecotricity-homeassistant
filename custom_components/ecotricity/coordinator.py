"""Data update coordinator and statistics import for Ecotricity."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

import aiohttp
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import async_add_external_statistics, get_last_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter

from .api import EcotricityAuthError, EcotricityClient, EcotricityError, MeterPoint, MeterReading, Tariff
from .const import (
    BACKFILL_DAYS,
    CONF_ACCOUNT_ID,
    CONF_PROPERTY_ID,
    DOMAIN,
    PORTAL_TIME_ZONE,
    REFRESH_DAYS,
    UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

type EcotricityConfigEntry = ConfigEntry[EcotricityCoordinator]


@dataclass
class MeterData:
    """Data for one meter point."""

    meter_point: MeterPoint
    readings: list[MeterReading]
    statistic_id: str

    @property
    def latest(self) -> MeterReading | None:
        return self.readings[-1] if self.readings else None


@dataclass
class EcotricityData:
    """All data for one config entry."""

    meters: dict[str, MeterData]
    tariffs: list[Tariff]

    def tariff_for(self, fuel: str) -> Tariff | None:
        return next((t for t in self.tariffs if t.fuel == fuel), self.tariffs[0] if self.tariffs else None)


def statistic_id_for(meter_point: MeterPoint) -> str:
    """Return the external statistic ID, for example `ecotricity:electricity_2700001234567`."""
    return f"{DOMAIN}:{meter_point.fuel.lower()}_{meter_point.mpan}"


class EcotricityCoordinator(DataUpdateCoordinator[EcotricityData]):
    """Fetch meter reads and tariffs, and import the reads as statistics."""

    config_entry: EcotricityConfigEntry

    def __init__(self, hass: HomeAssistant, entry: EcotricityConfigEntry, client: EcotricityClient) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.client = client
        self.account_id: str = entry.data[CONF_ACCOUNT_ID]
        self.property_id: int = entry.data[CONF_PROPERTY_ID]
        self._meter_points: list[MeterPoint] | None = None
        self._tz = dt_util.get_time_zone(PORTAL_TIME_ZONE)

    async def _async_update_data(self) -> EcotricityData:
        try:
            return await self._async_fetch()
        except EcotricityAuthError as err:
            raise ConfigEntryAuthFailed(translation_domain=DOMAIN, translation_key="auth_failed") from err
        except (EcotricityError, aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="update_failed",
                translation_placeholders={"error": str(err) or type(err).__name__},
            ) from err

    async def _async_fetch(self) -> EcotricityData:
        if self._meter_points is None:
            points = await self.client.get_meter_points(self.account_id, self.property_id)
            # Only single-register electricity meters are tested. Gas comes later.
            self._meter_points = [p for p in points if p.fuel == "Electricity"]

        today = dt_util.now(self._tz).date()
        meters: dict[str, MeterData] = {}
        for point in self._meter_points:
            statistic_id = statistic_id_for(point)
            last = await self._async_last_statistic(statistic_id)
            days = REFRESH_DAYS if last else BACKFILL_DAYS
            readings = await self.client.get_meter_readings(self.account_id, point, today - timedelta(days=days), today)
            self._import_statistics(point, statistic_id, readings, last)
            meters[point.id] = MeterData(point, readings, statistic_id)

        tariffs = await self.client.get_tariffs(self.account_id)
        return EcotricityData(meters=meters, tariffs=tariffs)

    async def _async_last_statistic(self, statistic_id: str) -> dict | None:
        result = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, False, {"state", "sum"}
        )
        rows = result.get(statistic_id)
        return dict(rows[0]) if rows else None

    def _import_statistics(
        self,
        point: MeterPoint,
        statistic_id: str,
        readings: list[MeterReading],
        last: dict | None,
    ) -> None:
        """Write one statistic per read.

        A read at 00:00 covers the day before, so the point goes in the hour that
        ends at the read time. `state` is the meter register. `sum` is the register
        minus an offset, so that the first point has sum 0. Every point stores both,
        so the offset is `state - sum` of any earlier point, and a new import writes
        the same values again (the import is idempotent).
        """
        if not readings:
            return
        if last and last.get("state") is not None and last.get("sum") is not None:
            offset = float(last["state"]) - float(last["sum"])
        else:
            offset = readings[0].cumulative

        statistics = []
        for reading in readings:
            end = reading.reading_at.replace(tzinfo=self._tz)
            statistics.append(
                StatisticData(
                    start=end - timedelta(hours=1),
                    state=reading.cumulative,
                    sum=round(reading.cumulative - offset, 3),
                )
            )
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Ecotricity {point.fuel.lower()} {point.mpan}",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        )
        async_add_external_statistics(self.hass, metadata, statistics)
