"""Tests for the SwitchBot Vacuum map camera entity."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.switchbot_vacuum.camera import (
    SwitchBotMapCamera,
    async_setup_entry,
)
from custom_components.switchbot_vacuum.const import DOMAIN


@pytest.fixture
def mock_coordinator():
    """Create a mock coordinator with map data."""
    coord = MagicMock()
    coord.device_mac = "AABBCCDDEEFF"
    coord.device_name = "SwitchBot S10"
    coord.entry = MagicMock()
    coord.entry.data = {"device_type": "WoSweeperOrigin"}
    coord.map_png = b"\x89PNG\r\n\x1a\nfake_png_data"
    coord.map_size = (231, 340)
    coord.map_rooms = {
        "ROOM_001": {
            "name": "Living room",
            "outline": [[10, 20], [50, 20], [50, 60], [10, 60]],
            "x": 30.0,
            "y": 40.0,
            "icon": "mdi:sofa",
        },
        "ROOM_002": {
            "name": "Cocina",
            "outline": [[60, 20], [100, 20], [100, 60], [60, 60]],
            "x": 80.0,
            "y": 40.0,
            "icon": "mdi:silverware-fork-knife",
        },
    }
    return coord


class TestMapCamera:
    """Test map camera entity."""

    def test_camera_init_and_unique_id(self, mock_coordinator):
        """Test camera initialization and unique_id."""
        camera = SwitchBotMapCamera(mock_coordinator)
        assert camera.unique_id == "AABBCCDDEEFF_map"
        assert camera.name == "Map"
        assert camera.has_entity_name is True

    @pytest.mark.asyncio
    async def test_camera_image_bytes(self, mock_coordinator):
        """Test async_camera_image returns map PNG bytes from coordinator."""
        camera = SwitchBotMapCamera(mock_coordinator)
        img = await camera.async_camera_image()
        assert img == b"\x89PNG\r\n\x1a\nfake_png_data"

    def test_extra_state_attributes(self, mock_coordinator):
        """Test extra_state_attributes contains rooms and calibration_points."""
        camera = SwitchBotMapCamera(mock_coordinator)
        attrs = camera.extra_state_attributes
        assert "rooms" in attrs
        assert "calibration_points" in attrs
        assert "ROOM_001" in attrs["rooms"]
        assert attrs["rooms"]["ROOM_001"]["name"] == "Living room"
        assert attrs["rooms"]["ROOM_001"]["icon"] == "mdi:sofa"

        # 4 identity calibration corners
        cal = attrs["calibration_points"]
        assert len(cal) == 4
        assert cal[0] == {"vacuum": {"x": 0, "y": 0}, "map": {"x": 0, "y": 0}}
        assert cal[1] == {"vacuum": {"x": 231, "y": 0}, "map": {"x": 231, "y": 0}}
        assert cal[2] == {"vacuum": {"x": 231, "y": 340}, "map": {"x": 231, "y": 340}}
        assert cal[3] == {"vacuum": {"x": 0, "y": 340}, "map": {"x": 0, "y": 340}}

    def test_extra_state_attributes_fallback_size(self, mock_coordinator):
        """Test calibration_points fallback size when map_size is not yet loaded."""
        mock_coordinator.map_size = (0, 0)
        camera = SwitchBotMapCamera(mock_coordinator)
        attrs = camera.extra_state_attributes
        cal = attrs["calibration_points"]
        assert len(cal) == 4
        assert cal[1] == {"vacuum": {"x": 100, "y": 0}, "map": {"x": 100, "y": 0}}

    @pytest.mark.asyncio
    async def test_async_setup_entry(self, mock_coordinator):
        """Test async_setup_entry adds camera entity."""
        hass = MagicMock()
        entry = MagicMock()
        entry.entry_id = "test_entry"
        hass.data = {DOMAIN: {"test_entry": mock_coordinator}}
        async_add_entities = MagicMock()

        await async_setup_entry(hass, entry, async_add_entities)
        async_add_entities.assert_called_once()
        added_entities = async_add_entities.call_args[0][0]
        assert len(added_entities) == 1
        assert isinstance(added_entities[0], SwitchBotMapCamera)

