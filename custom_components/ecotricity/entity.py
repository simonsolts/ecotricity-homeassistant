"""Base entity for Ecotricity."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EcotricityCoordinator


class EcotricityEntity(CoordinatorEntity[EcotricityCoordinator]):
    """An entity that belongs to the device of one supply address."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EcotricityCoordinator, key: str, unique_suffix: str) -> None:
        super().__init__(coordinator)
        self._attr_translation_key = key
        self._attr_unique_id = f"{coordinator.account_id}_{unique_suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.account_id)},
            name=coordinator.config_entry.title,
            manufacturer="Ecotricity",
            entry_type=DeviceEntryType.SERVICE,
            configuration_url="https://my.ecotricity.co.uk/",
        )
