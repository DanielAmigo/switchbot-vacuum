"""Camera entity providing the floor plan map for SwitchBot Vacuum."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DEVICE_TYPE_TO_MODEL, DOMAIN
from .coordinator import SwitchBotS10Coordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up SwitchBot Vacuum map camera."""
    coordinator: SwitchBotS10Coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([SwitchBotMapCamera(coordinator)])


class SwitchBotMapCamera(CoordinatorEntity[SwitchBotS10Coordinator], Camera):
    """Camera entity that provides the floor plan map for SwitchBot Vacuum."""

    _attr_has_entity_name = True
    _attr_name = "Map"

    def __init__(self, coordinator: SwitchBotS10Coordinator) -> None:
        """Initialize the map camera."""
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._attr_unique_id = f"{coordinator.device_mac}_map"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.device_mac)},
            name=coordinator.device_name,
            manufacturer="SwitchBot",
            model=DEVICE_TYPE_TO_MODEL.get(
                coordinator.entry.data.get("device_type", ""), "Robot Vacuum"
            ),
        )

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return bytes of the latest map image (PNG)."""
        return self.coordinator.map_png

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes for vacuum map card."""
        w, h = self.coordinator.map_size
        if w <= 0 or h <= 0:
            w, h = 100, 100

        calibration_points = [
            {"vacuum": {"x": 0, "y": 0}, "map": {"x": 0, "y": 0}},
            {"vacuum": {"x": w, "y": 0}, "map": {"x": w, "y": 0}},
            {"vacuum": {"x": w, "y": h}, "map": {"x": w, "y": h}},
            {"vacuum": {"x": 0, "y": h}, "map": {"x": 0, "y": h}},
        ]

        return {
            "rooms": self.coordinator.map_rooms,
            "calibration_points": calibration_points,
        }

