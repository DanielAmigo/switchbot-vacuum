"""Tests for the SwitchBot S10 coordinator."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import ClientSession

from custom_components.switchbot_vacuum.coordinator import SwitchBotS10Coordinator


@pytest.fixture(autouse=True)
def patch_frame_helper():
    """Patch HA frame helper so DataUpdateCoordinator can initialize."""
    with patch("homeassistant.helpers.frame.report_usage"):
        yield


@pytest.fixture
def mock_hass():
    """Create a mock HomeAssistant instance."""
    hass = MagicMock()
    hass.data = {}
    return hass


@pytest.fixture
def mock_entry():
    """Create a mock config entry."""
    entry = MagicMock()
    entry.data = {"username": "test@test.com", "password": "testpass", "device_mac": "AABBCCDDEEFF"}
    entry.entry_id = "test_entry_id"
    return entry


def _make_response(data: dict, status: int = 200):
    """Create a mock aiohttp response."""
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=data)
    return resp


def _patch_session(response):
    """Create a patched aiohttp.ClientSession context manager."""
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    mock_session.post = MagicMock(return_value=AsyncMock(
        __aenter__=AsyncMock(return_value=response),
        __aexit__=AsyncMock(return_value=False),
    ))
    return patch("aiohttp.ClientSession", return_value=mock_session)


class TestLogin:
    """Test login functionality."""

    @pytest.mark.asyncio
    async def test_login_success(self, mock_hass, mock_entry):
        """Test successful login returns access token."""
        resp = _make_response({
            "body": {
                "access_token": "test_jwt_token",
                "refresh_token": "test_refresh",
            }
        })
        with _patch_session(resp):
            coordinator = SwitchBotS10Coordinator(mock_hass, mock_entry)
            await coordinator.async_login()
            assert coordinator.access_token == "test_jwt_token"

    @pytest.mark.asyncio
    async def test_login_failure_raises(self, mock_hass, mock_entry):
        """Test login with bad credentials raises."""
        resp = _make_response({"statusCode": 401, "message": "unauthorized"})
        with _patch_session(resp):
            coordinator = SwitchBotS10Coordinator(mock_hass, mock_entry)
            with pytest.raises(Exception):
                await coordinator.async_login()


class TestDiscoverDevices:
    """Test device discovery."""

    @pytest.mark.asyncio
    async def test_discovers_multiple_s10s(self, mock_hass, mock_entry):
        """Test that discover_devices finds all S10 devices."""
        resp = _make_response({
            "body": {
                "Items": [
                    {
                        "device_mac": "AABBCCDDEEFF",
                        "device_name": "Floor Cleaning Robot S10 B6",
                        "device_detail": {"device_type": "WoSweeperOrigin"},
                        "userID": "user-123",
                        "groupID": "group-123",
                    },
                    {
                        "device_mac": "AABBCCDD",
                        "device_name": "Light",
                        "device_detail": {"device_type": "WoLight"},
                    },
                    {
                        "device_mac": "C0D1E2F30044",
                        "device_name": "Floor Cleaning Robot S10 44",
                        "device_detail": {"device_type": "WoSweeperOrigin"},
                        "userID": "user-123",
                        "groupID": "group-456",
                    },
                ]
            }
        })
        with _patch_session(resp):
            coordinator = SwitchBotS10Coordinator(mock_hass, mock_entry)
            coordinator.access_token = "fake_token"
            devices = await coordinator.async_discover_devices()

            assert len(devices) == 2
            assert devices[0]["device_mac"] == "AABBCCDDEEFF"
            assert devices[1]["device_mac"] == "C0D1E2F30044"

    @pytest.mark.asyncio
    async def test_discovers_single_s10(self, mock_hass, mock_entry):
        """Test discovery with single S10."""
        resp = _make_response({
            "body": {
                "Items": [
                    {
                        "device_mac": "AABBCCDDEEFF",
                        "device_name": "Floor Cleaning Robot S10 B6",
                        "device_detail": {"device_type": "WoSweeperOrigin"},
                        "userID": "user-123",
                        "groupID": "group-123",
                    },
                ]
            }
        })
        with _patch_session(resp):
            coordinator = SwitchBotS10Coordinator(mock_hass, mock_entry)
            coordinator.access_token = "fake_token"
            devices = await coordinator.async_discover_devices()

            assert len(devices) == 1
            assert devices[0]["device_name"] == "Floor Cleaning Robot S10 B6"


class TestGetProperties:
    """Test property fetching."""

    @pytest.mark.asyncio
    async def test_get_status_properties(self, mock_hass, mock_entry):
        """Test fetching status properties."""
        resp = _make_response({
            "resultCode": 100,
            "data": {
                "1003": {"id": 1003, "value": True},
                "1004": {"id": 1004, "value": 85},
                "1010": {"id": 1010, "value": 3},
                "1053": {"id": 1053, "value": {
                    "fan_level": 2, "times": 1,
                    "type": "sweep_mop", "water_level": 1,
                }},
                "1052": {"id": 1052, "value": {
                    "clean_area": 45, "clean_time": 30,
                    "duration": 1800, "total_area": 1,
                }},
            },
        })
        with _patch_session(resp):
            coordinator = SwitchBotS10Coordinator(mock_hass, mock_entry)
            coordinator.access_token = "fake_token"
            coordinator.device_mac = "AABBCCDDEEFF"
            props = await coordinator.async_get_properties([1003, 1004, 1010, 1053, 1052])

            assert props[1003] is True
            assert props[1004] == 85
            assert props[1010] == 3


class TestSendCommand:
    """Test command sending."""

    @pytest.mark.asyncio
    async def test_send_clean_rooms(self, mock_hass, mock_entry):
        """Test sending a clean_rooms command."""
        resp = _make_response({"resultCode": 100, "data": "CMD-UUID"})
        with _patch_session(resp):
            coordinator = SwitchBotS10Coordinator(mock_hass, mock_entry)
            coordinator.access_token = "fake_token"
            coordinator.device_mac = "AABBCCDDEEFF"
            coordinator._uuid = "test-uuid"
            result = await coordinator.async_send_command(
                1001,
                {"0": "clean_rooms", "1": {
                    "force_order": True,
                    "mode": {"fan_level": 1, "times": 1, "type": "mop", "water_level": 2},
                    "rooms": [{"room_id": "ROOM_013", "mode": {
                        "fan_level": 1, "times": 1, "type": "mop", "water_level": 2,
                    }}],
                }},
            )
            assert result["resultCode"] == 100


class TestMapParsing:
    """Test map zip parsing, coordinate transformation, and geometry algorithms."""

    def test_simplify_polygon_collinear(self):
        """Test removing redundant collinear vertices along straight lines."""
        # A rectangle with intermediate collinear points on each side
        points = [
            [0, 0], [5, 0], [10, 0],
            [10, 5], [10, 10],
            [5, 10], [0, 10],
            [0, 5]
        ]
        simplified = SwitchBotS10Coordinator._simplify_polygon(points)
        assert simplified == [[0, 0], [10, 0], [10, 10], [0, 10]]

    def test_calculate_centroid(self):
        """Test calculating centroid of a polygon."""
        points = [[0, 0], [10, 0], [10, 10], [0, 10]]
        cx, cy = SwitchBotS10Coordinator._calculate_centroid(points)
        assert cx == 5.0
        assert cy == 5.0

    def test_get_room_icon(self):
        """Test assigning MDI icon based on room name keywords."""
        assert SwitchBotS10Coordinator._get_room_icon("Living room") == "mdi:sofa"
        assert SwitchBotS10Coordinator._get_room_icon("Salón principal") == "mdi:sofa"
        assert SwitchBotS10Coordinator._get_room_icon("Cocina") == "mdi:silverware-fork-knife"
        assert SwitchBotS10Coordinator._get_room_icon("Dormitorio") == "mdi:bed"
        assert SwitchBotS10Coordinator._get_room_icon("Baño") == "mdi:shower"
        assert SwitchBotS10Coordinator._get_room_icon("Pasillo") == "mdi:hallway"
        assert SwitchBotS10Coordinator._get_room_icon("Despacho") == "mdi:desk"
        assert SwitchBotS10Coordinator._get_room_icon("Terraza") == "mdi:balcony"
        assert SwitchBotS10Coordinator._get_room_icon("Lavadero") == "mdi:washing-machine"
        assert SwitchBotS10Coordinator._get_room_icon("Habitación invitados") == "mdi:bed"
        assert SwitchBotS10Coordinator._get_room_icon("Unknown Room") == "mdi:floor-plan"

    def test_options_persistence_on_init(self, mock_hass):
        """Test that rooms, map_rooms, and map_size are restored from entry.options."""
        entry = MagicMock()
        entry.data = {"username": "test@test.com", "password": "pw", "device_mac": "AABB"}
        entry.options = {
            "rooms": {"ROOM_001": "Living room"},
            "map_rooms": {
                "ROOM_001": {
                    "name": "Living room",
                    "outline": [[0, 0], [10, 0], [10, 10], [0, 10]],
                    "x": 5.0,
                    "y": 5.0,
                    "icon": "mdi:sofa",
                }
            },
            "map_size": [231, 340],
        }
        coordinator = SwitchBotS10Coordinator(mock_hass, entry)
        assert coordinator.rooms == {"ROOM_001": "Living room"}
        assert "ROOM_001" in coordinator.map_rooms
        assert coordinator.map_size == (231, 340)

    def test_parse_map_zip(self, mock_hass, mock_entry):
        """Test full map zip parsing with PGM, map.json, and labels.json."""
        import io
        import zipfile

        # Create synthetic P5 PGM (10x10)
        pgm_bytes = b"P5\n10 10\n255\n" + b"\x80" * 100
        map_json = json.dumps({
            "resolution": 0.05,
            "origin": [-2.0, -10.0, 0.0]
        }).encode("utf-8")
        labels_json = json.dumps({
            "data": [
                {
                    "id": "ROOM_001",
                    "name": "Living room",
                    "geometry": [
                        [-2.0, -10.0],
                        [-1.0, -10.0],
                        [-1.0, -9.0],
                        [-2.0, -9.0]
                    ]
                }
            ]
        }).encode("utf-8")

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("refined/map.pgm", pgm_bytes)
            zf.writestr("map.json", map_json)
            zf.writestr("labels.json", labels_json)
        zip_bytes = buf.getvalue()

        coordinator = SwitchBotS10Coordinator(mock_hass, mock_entry)
        map_png, map_rooms, map_size, rooms = coordinator._parse_map_zip(zip_bytes)

        # Map size
        assert map_size == (10, 10)
        # Rooms
        assert rooms == {"ROOM_001": "Living room"}
        assert "ROOM_001" in map_rooms
        room = map_rooms["ROOM_001"]
        assert room["name"] == "Living room"
        assert room["icon"] == "mdi:sofa"

        # Coordinate transformation check:
        # mx = -2.0, origin_x = -2.0 -> px = round((-2.0 - (-2.0)) / 0.05) = 0
        # my = -10.0, origin_y = -10.0, height = 10 -> py = round(10 - (-10.0 - (-10.0)) / 0.05) = 10
        # mx = -1.0 -> px = round((-1.0 - (-2.0)) / 0.05) = 20
        # my = -9.0 -> py = round(10 - (-9.0 - (-10.0)) / 0.05) = round(10 - 20) = -10
        expected_outline = [[0, 10], [20, 10], [20, -10], [0, -10]]
        assert room["outline"] == expected_outline
        assert room["x"] == 10.0
        assert room["y"] == 0.0
