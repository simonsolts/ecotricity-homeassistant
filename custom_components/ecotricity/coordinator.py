"""Data update coordinator and statistics import for Ecotricity."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

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

from .api import (
    Agreement,
    EcotricityAuthError,
    EcotricityClient,
    EcotricityError,
    MeterPoint,
    MeterReading,
    Tariff,
)
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
    cost_statistic_id: str

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
    """Return the energy statistic ID, for example `ecotricity:electricity_2700001234567`."""
    return f"{DOMAIN}:{meter_point.fuel.lower()}_{meter_point.mpan}"


def cost_statistic_id_for(meter_point: MeterPoint) -> str:
    """Return the cost statistic ID, for example `ecotricity:electricity_cost_2700001234567`."""
    return f"{DOMAIN}:{meter_point.fuel.lower()}_cost_{meter_point.mpan}"


class EcotricityCoordinator(DataUpdateCoordinator[EcotricityData]):
    """Fetch meter reads and tariffs, and import the reads as statistics."""

    config_entry: EcotricityConfigEntry

    def __init__(self, hass: HomeAssistant, entry: EcotricityConfigEntry, client: EcotricityClient) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN, update_interval=UPDATE_INTERVAL)
        self.client = client
        self.account_id: str = entry.data[CONF_ACCOUNT_ID]
        self.property_id: int = entry.data[CONF_PROPERTY_ID]
        self._meter_points: list[MeterPoint] | None = None
        self._agreements: dict[str, list[Agreement]] = {}
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

        tariffs = await self.client.get_tariffs(self.account_id)
        data = EcotricityData(meters={}, tariffs=tariffs)

        today = dt_util.now(self._tz).date()
        for point in self._meter_points:
            statistic_id = statistic_id_for(point)
            cost_statistic_id = cost_statistic_id_for(point)
            last = await self._async_last_statistic(statistic_id)
            last_cost = await self._async_last_statistic(cost_statistic_id)
            tariff = data.tariff_for(point.fuel)
            priced_from = await self._async_tariff_start(tariff, point) if tariff else None

            earliest = today - timedelta(days=BACKFILL_DAYS)
            if last is None:
                from_date = earliest
            else:
                # Normally a short window. Reach back further to the last saved
                # point (for example after Home Assistant was off), or to the
                # tariff start when the cost statistic is new.
                starts = [today - timedelta(days=REFRESH_DAYS), self._stat_date(last) - timedelta(days=1)]
                if last_cost is not None:
                    starts.append(self._stat_date(last_cost) - timedelta(days=1))
                elif priced_from is not None and priced_from != date.max:
                    starts.append(priced_from)
                from_date = max(earliest, min(starts))
            readings = await self.client.get_meter_readings(self.account_id, point, from_date, today)

            self._import_statistics(point, statistic_id, readings, last)
            if tariff is not None:
                self._import_costs(point, cost_statistic_id, readings, last_cost, tariff, priced_from)
            data.meters[point.id] = MeterData(point, readings, statistic_id, cost_statistic_id)
        return data

    def _stat_date(self, row: dict) -> date:
        return datetime.fromtimestamp(row["start"], self._tz).date()

    async def _async_last_statistic(self, statistic_id: str) -> dict | None:
        result = await get_instance(self.hass).async_add_executor_job(
            get_last_statistics, self.hass, 1, statistic_id, False, {"state", "sum"}
        )
        rows = result.get(statistic_id)
        return dict(rows[0]) if rows else None

    async def _async_tariff_start(self, tariff: Tariff, point: MeterPoint) -> date | None:
        """Return the start date of the agreement for this tariff and meter point."""
        if tariff.code not in self._agreements:
            try:
                self._agreements[tariff.code] = await self.client.get_agreements(self.account_id, [tariff.code])
            except EcotricityError as err:
                # Without the start date we cannot tell which old reads the rate applies to.
                _LOGGER.warning("Could not get the agreement for tariff %s: %s", tariff.code, err)
                return date.max
        matches = [
            a
            for a in self._agreements[tariff.code]
            if a.tariff_code == tariff.code and (not a.meter_point_ids or point.id in a.meter_point_ids)
        ]
        if not matches:
            _LOGGER.warning("No agreement found for tariff %s; costs start from today", tariff.code)
            return dt_util.now(self._tz).date()
        return max(a.from_date for a in matches)

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

    def _import_costs(
        self,
        point: MeterPoint,
        statistic_id: str,
        readings: list[MeterReading],
        last: dict | None,
        tariff: Tariff,
        priced_from: date | None,
    ) -> None:
        """Write the energy cost (usage x unit rate) of each new read, in GBP.

        Unlike the energy statistic, this import only adds reads that are newer
        than the last cost point. Each point is priced once, with the unit rate
        at that time, so a later rate change does not rewrite past days.

        Reads before the start of the current agreement are not priced, because
        the current rate may not apply to them. The first priced read (or the
        read at the agreement start) is the starting point, with sum 0. The
        standing charge is not included.
        """
        rate = tariff.unit_rate / 100  # pence to GBP
        last_start = last["start"] if last else None
        running = float(last["sum"]) if last and last.get("sum") is not None else 0.0

        statistics: list[StatisticData] = []
        previous: MeterReading | None = None
        for reading in readings:
            if priced_from is not None and reading.reading_at.date() < priced_from:
                continue
            start = reading.reading_at.replace(tzinfo=self._tz) - timedelta(hours=1)
            if last_start is not None and start.timestamp() <= last_start:
                previous = reading
                continue
            if previous is None and last is None:
                # The first point of a new statistic.
                statistics.append(StatisticData(start=start, state=0.0, sum=0.0))
                previous = reading
                continue
            # Use the register difference, like the energy statistic. Without an
            # earlier read in this window, use the read's own consumption.
            usage = reading.cumulative - previous.cumulative if previous is not None else reading.consumption or 0.0
            cost = round(max(usage, 0.0) * rate, 4)
            running = round(running + cost, 4)
            statistics.append(StatisticData(start=start, state=cost, sum=running))
            previous = reading

        if not statistics:
            return
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Ecotricity {point.fuel.lower()} cost {point.mpan}",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=None,
            unit_of_measurement="GBP",
        )
        async_add_external_statistics(self.hass, metadata, statistics)
