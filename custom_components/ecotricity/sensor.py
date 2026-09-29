"""Sensors for Ecotricity."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .api import Tariff
from .const import PORTAL_TIME_ZONE
from .coordinator import EcotricityConfigEntry, EcotricityCoordinator, MeterData
from .entity import EcotricityEntity

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class MeterSensorDescription(SensorEntityDescription):
    value_fn: Callable[[MeterData], Any]


@dataclass(frozen=True, kw_only=True)
class TariffSensorDescription(SensorEntityDescription):
    value_fn: Callable[[Tariff], float]


def _local(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=dt_util.get_time_zone(PORTAL_TIME_ZONE)) if value else None


# The meter reading has no state class on purpose. Reads arrive hours late, so
# long-term statistics made from the sensor state would put usage on the wrong day.
# Use the external statistic `ecotricity:electricity_<mpan>` in the Energy dashboard.
METER_SENSORS: tuple[MeterSensorDescription, ...] = (
    MeterSensorDescription(
        key="meter_reading",
        translation_key="meter_reading",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=lambda m: m.latest.cumulative if m.latest else None,
    ),
    MeterSensorDescription(
        key="last_consumption",
        translation_key="last_consumption",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=1,
        value_fn=lambda m: m.latest.consumption if m.latest else None,
    ),
    MeterSensorDescription(
        key="last_reading_time",
        translation_key="last_reading_time",
        device_class=SensorDeviceClass.TIMESTAMP,
        value_fn=lambda m: _local(m.latest.reading_at) if m.latest else None,
    ),
)

TARIFF_SENSORS: tuple[TariffSensorDescription, ...] = (
    TariffSensorDescription(
        key="unit_rate",
        translation_key="unit_rate",
        native_unit_of_measurement=f"GBP/{UnitOfEnergy.KILO_WATT_HOUR}",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda t: round(t.unit_rate / 100, 5),
    ),
    TariffSensorDescription(
        key="standing_charge",
        translation_key="standing_charge",
        native_unit_of_measurement="GBP/d",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=4,
        value_fn=lambda t: round(t.standing_charge / 100, 5),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EcotricityConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = []
    fuels: set[str] = set()
    for meter_id, meter in coordinator.data.meters.items():
        entities.extend(EcotricityMeterSensor(coordinator, meter_id, meter, d) for d in METER_SENSORS)
        fuels.add(meter.meter_point.fuel)
    for fuel in sorted(fuels):
        entities.extend(EcotricityTariffSensor(coordinator, fuel, d) for d in TARIFF_SENSORS)
    async_add_entities(entities)


class EcotricityMeterSensor(EcotricityEntity, SensorEntity):
    """A sensor for one meter point."""

    entity_description: MeterSensorDescription

    def __init__(
        self,
        coordinator: EcotricityCoordinator,
        meter_id: str,
        meter: MeterData,
        description: MeterSensorDescription,
    ) -> None:
        super().__init__(
            coordinator, description.translation_key or description.key, f"{meter.meter_point.mpan}_{description.key}"
        )
        self.entity_description = description
        self._meter_id = meter_id
        self._attr_extra_state_attributes = {"mpan": meter.meter_point.mpan}
        if description.key == "meter_reading":
            self._attr_extra_state_attributes["statistic_id"] = meter.statistic_id
            self._attr_extra_state_attributes["cost_statistic_id"] = meter.cost_statistic_id

    @property
    def _meter(self) -> MeterData | None:
        return self.coordinator.data.meters.get(self._meter_id)

    @property
    def available(self) -> bool:
        return super().available and self._meter is not None

    @property
    def native_value(self) -> Any:
        meter = self._meter
        return self.entity_description.value_fn(meter) if meter else None


class EcotricityTariffSensor(EcotricityEntity, SensorEntity):
    """A tariff rate for one fuel."""

    entity_description: TariffSensorDescription

    def __init__(self, coordinator: EcotricityCoordinator, fuel: str, description: TariffSensorDescription) -> None:
        super().__init__(coordinator, description.key, f"{fuel.lower()}_{description.key}")
        self.entity_description = description
        self._fuel = fuel

    @property
    def _tariff(self) -> Tariff | None:
        return self.coordinator.data.tariff_for(self._fuel)

    @property
    def available(self) -> bool:
        return super().available and self._tariff is not None

    @property
    def native_value(self) -> float | None:
        tariff = self._tariff
        return self.entity_description.value_fn(tariff) if tariff else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        tariff = self._tariff
        if tariff is None or self.entity_description.key != "unit_rate":
            return None
        return {"tariff_name": tariff.name, "tariff_code": tariff.code, "tariff_end_date": tariff.end_date}
