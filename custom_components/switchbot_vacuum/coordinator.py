"""Data update coordinator for SwitchBot Vacuum."""
from __future__ import annotations

import io
import json
import logging
import os
import time
import uuid
import zipfile
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    API_AUTH_HOST,
    API_TIMEOUT,
    APP_VERSION,
    CLIENT_ID,
    CONF_DEVICE_MAC,
    CONF_PASSWORD,
    CONF_PRODUCT_KEY,
    CONF_USERNAME,
    DEVICE_TYPE_K10,
    DEVICE_TYPE_K10PRO,
    SUPPORTED_DEVICE_TYPES,
    DOMAIN,
    K10_WORK_STATUS_STANDBY,
    PROP_AWS_CREDS,
    PROP_BATTERY,
    PROP_CLEAN_MODE,
    PROP_CLEAN_SUMMARY,
    PROP_ERROR_CODE,
    PROP_FIRMWARE,
    PROP_MAP_INFO,
    PROP_ONLINE,
    PROP_ROOM_PLANS,
    PROP_S3_BUCKET,
    PROP_WORK_STATUS,
    K10PRO_PROP_ONLINE,
    K10PRO_PROP_BATTERY,
    K10PRO_PROP_SUCTION_POW_LEVEL,
    K10PRO_PROP_WORK_STATUS,
    K10PRO_PROP_DUST_COLECT_FREQUENCY,
    K10PRO_PROP_CHILD_LOCK,
    K10PRO_PROP_DUST_COLECT_TIME,
    K10PRO_PROP_AUTO_RESTART,
    S3_REGION,
    TOKEN_REFRESH_SECONDS,
    UPDATE_INTERVAL_SECONDS,
)
_LOGGER = logging.getLogger(__name__)

STATUS_PROPS = [PROP_ONLINE, PROP_BATTERY, PROP_WORK_STATUS, PROP_ERROR_CODE,
                PROP_CLEAN_MODE, PROP_CLEAN_SUMMARY, PROP_FIRMWARE]
K10PRO_STATUS_PROPS = [
    K10PRO_PROP_ONLINE,
    K10PRO_PROP_BATTERY,
    K10PRO_PROP_SUCTION_POW_LEVEL,
    K10PRO_PROP_WORK_STATUS,
    K10PRO_PROP_DUST_COLECT_FREQUENCY,
    K10PRO_PROP_CHILD_LOCK,
    K10PRO_PROP_DUST_COLECT_TIME,
    K10PRO_PROP_AUTO_RESTART,
]


class SwitchBotS10Coordinator(DataUpdateCoordinator):
    """Manage fetching data from SwitchBot Vacuum API."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize."""
        self.entry = entry
        self.access_token: str | None = None
        self.bot_region: str | None = None
        self.wonderlab_endpoint: str | None = None
        self.device_mac: str | None = None
        self.device_name: str | None = None
        self.user_id: str | None = None
        self._uuid: str = str(uuid.uuid4())
        self._token_expiry: float = 0
        self._map_png: bytes | None = None
        self._map_rooms: dict[str, Any] = dict(entry.options.get("map_rooms", {}))
        self._map_size: tuple[int, int] = tuple(entry.options.get("map_size", (0, 0)))
        self._rooms: dict[str, str] = dict(entry.options.get("rooms", {}))  # ROOM_ID -> name
        self._last_room_refresh: float = 0

        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )

    def _is_k10(self) -> bool:
        """Return True if device is K10+."""
        return self.entry.data.get("device_type") == DEVICE_TYPE_K10

    def _is_k10_pro(self) -> bool:
        """Return True if device is K10+."""
        return self.entry.data.get("device_type") == DEVICE_TYPE_K10PRO

    def _headers(self, auth: str | None = None) -> dict[str, str]:
        """Build common request headers."""
        return {
            "authorization": auth if auth is not None else (self.access_token or ""),
            "uuid": self._uuid,
            "requestid": str(uuid.uuid4()),
            "appversion": APP_VERSION,
            "content-type": "application/json; charset=UTF-8",
        }

    async def async_login(self) -> None:
        """Authenticate with SwitchBot API."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{API_AUTH_HOST}/account/api/v1/user/login",
                headers=self._headers(auth=""),
                json={
                    "clientId": CLIENT_ID,
                    "deviceInfo": {
                        "deviceId": self._uuid,
                        "deviceName": "Home Assistant",
                        "model": "Home Assistant",
                    },
                    "grantType": "password",
                    "password": self.entry.data[CONF_PASSWORD],
                    "username": self.entry.data[CONF_USERNAME],
                    "verifyCode": "",
                },
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                data = await resp.json()
                body = data.get("body", {})
                token = body.get("access_token")
                if not token:
                    raise ConfigEntryAuthFailed(
                        f"Login failed: {data.get('message', data.get('statusCode', 'unknown'))}"
                    )
                self.access_token = token
                self._token_expiry = time.time() + TOKEN_REFRESH_SECONDS
            async with session.post(
                f"{API_AUTH_HOST}/account/api/v1/user/userinfo",
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                data = await resp.json()
                body = data.get("body", {})
                self.bot_region = body.get("botRegion")
                if not self.bot_region:
                    raise ConfigEntryAuthFailed(
                        f"Get UserInfo failed: {data.get('message', data.get('statusCode', 'unknown'))}"
                    )
            async with session.post(
                f"{API_AUTH_HOST}/admin/admin/api/v1/botregion/endpoint",
                headers=self._headers(),
                json={
                    "botRegion": self.bot_region,
                },
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                data = await resp.json()
                body = data.get("data", [])
                self.wonderlab_endpoint = next(
                    (v["host"] for v in body if v["name"] == "wonderlabs"), None
                )
                if not self.wonderlab_endpoint:
                    raise ConfigEntryAuthFailed(
                        f"Get Endpoints failed: {data.get('message', data.get('resultCode', 'unknown'))}"
                    )

    async def async_discover_devices(self) -> list[dict[str, Any]]:
        """Find all supported vacuum devices in the account."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.wonderlab_endpoint}/wonder/device/v3/getdevice",
                headers=self._headers(),
                json={"required_type": "All"},
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                data = await resp.json()
                devices = []
                for device in data.get("body", {}).get("Items", []):
                    device_type = device.get("device_detail", {}).get("device_type")
                    if device_type in SUPPORTED_DEVICE_TYPES:
                        devices.append({
                            "device_mac": device["device_mac"],
                            "device_name": device.get("device_name", "SwitchBot Vacuum"),
                            "device_type": device_type,
                            "product_key": device.get("product_key", ""),
                            "user_id": device.get("userID"),
                            "group_id": device.get("groupID"),
                        })
                return devices

    def set_device(self, device_mac: str, device_name: str, user_id: str | None = None) -> None:
        """Set the target device after config flow discovery."""
        self.device_mac = device_mac
        self.device_name = device_name
        self.user_id = user_id

    async def async_get_properties(self, property_ids: list[int]) -> dict[int, Any]:
        """Fetch device properties from shadow API (S10 only)."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.wonderlab_endpoint}/device/device/v1/shadow/getByIDs",
                headers=self._headers(),
                json={"deviceID": self.device_mac, "propertyIDs": property_ids},
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                data = await resp.json()
                if data.get("resultCode") != 100:
                    raise UpdateFailed(f"Property fetch failed: {data}")
                result = {}
                for pid_str, prop in (data.get("data") or {}).items():
                    result[int(pid_str)] = prop.get("value")
                return result

    async def async_send_command(
        self, function_id: int, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Send a command to the device via invokeFunc (S10 only)."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.wonderlab_endpoint}/command/cmd/api/v1/func/invoke",
                headers=self._headers(),
                json={
                    "deviceID": self.device_mac,
                    "functionID": function_id,
                    "params": params,
                    "notify": {
                        "type": "mqtt",
                        "url": f"v1_1/{self._uuid}/APP_HA_{self._uuid}/funcResp",
                    },
                    "optSrc": "app",
                    "timeout": 65535,
                },
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                return await resp.json()

    async def _get_product_key(self) -> str:
        """Return product_key from config entry or re-discover it."""
        key = self.entry.data.get(CONF_PRODUCT_KEY, "")
        if key:
            return key
        _LOGGER.info("product_key missing from config, re-discovering devices")
        devices = await self.async_discover_devices()
        for device in devices:
            if device["device_mac"] == self.device_mac:
                key = device.get("product_key", "")
                if key:
                    _LOGGER.info("Found product_key for %s: %s", self.device_mac, key)
                break
        return key

    async def async_send_action(
        self, identifier: str, input_data: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Send a command to K10+ via setAction."""
        product_key = await self._get_product_key()
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.wonderlab_endpoint}/wonder/sweeper360/v1/device/setAction",
                headers=self._headers(),
                json={
                    "productKey": product_key,
                    "deviceName": self.device_mac,
                    "identifier": identifier,
                    "input": input_data or {},
                },
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                return await resp.json()

    async def async_send_info(self, items: dict[str, Any]) -> dict[str, Any]:
        """Set K10+ device properties via setInfo endpoint."""
        product_key = await self._get_product_key()
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.wonderlab_endpoint}/wonder/sweeper360/v1/device/setInfo",
                headers=self._headers(),
                json={
                    "productKey": product_key,
                    "deviceName": self.device_mac,
                    "items": items,
                },
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                return await resp.json()

    async def async_get_k10_status(self) -> dict[str, Any]:
        """Fetch real-time K10+ status via getstatus endpoint."""
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.wonderlab_endpoint}/wonder/devicestatus/v1/getstatus",
                headers=self._headers(),
                json={"items": [self.device_mac]},
                timeout=aiohttp.ClientTimeout(total=API_TIMEOUT),
            ) as resp:
                data = await resp.json()
                if data.get("statusCode") != 100:
                    raise UpdateFailed(f"K10+ getstatus failed: {data}")
                items = data.get("body", {}).get("items", [])
                if not items:
                    raise UpdateFailed("K10+ getstatus returned no items")
                return items[0]

    async def async_refresh_k10_rooms(self) -> None:
        """Fetch K10+ room IDs from GetCleanPolicyList and build room map."""
        try:
            resp = await self.async_send_action("GetCleanPolicyList")
            if resp.get("statusCode") != 100:
                _LOGGER.warning("GetCleanPolicyList failed: %s", resp)
                return

            result = json.loads(resp["body"]["result"])
            policy = json.loads(result["CleanPolicyList"])

            room_ids: set[int] = set()
            for entry in policy.get("value", []):
                for rid in entry.get("smartAreaIds", []):
                    room_ids.add(rid)

            if room_ids:
                self._rooms = {f"room{rid}": f"room{rid}" for rid in sorted(room_ids)}
                self._last_room_refresh = time.time()
                _LOGGER.info("Loaded %d K10+ rooms: %s", len(self._rooms), list(self._rooms))
        except Exception as exc:
            _LOGGER.warning("Failed to refresh K10+ rooms: %s", exc)

    def _extract_rooms_from_room_plans(self, room_plans: Any) -> dict[str, str]:
        """Try to extract room names from PROP_ROOM_PLANS property."""
        rooms: dict[str, str] = {}
        if not room_plans:
            return rooms
        plans = room_plans if isinstance(room_plans, list) else []
        if isinstance(room_plans, dict):
            plans = room_plans.get("data", room_plans.get("rooms", []))
        for room in plans:
            if not isinstance(room, dict):
                continue
            room_id = str(room.get("id", room.get("roomId", "")))
            name = room.get("name", room.get("roomName", room_id))
            if room_id.startswith("ROOM_"):
                rooms[room_id] = name
        return rooms

    @staticmethod
    def _extract_polygon_meters(room: dict[str, Any]) -> list[tuple[float, float]]:
        """Extract polygon coordinates in meters from room geometry."""
        geom = (
            room.get("geometry")
            or room.get("polygon")
            or room.get("points")
            or room.get("outline")
        )
        if not geom:
            return []
        if isinstance(geom, dict):
            if "coordinates" in geom:
                geom = geom["coordinates"]
            elif "points" in geom:
                geom = geom["points"]
        # Unwrap nested rings (e.g. GeoJSON [[[x, y], ...]])
        while (
            isinstance(geom, list)
            and len(geom) > 0
            and isinstance(geom[0], list)
            and len(geom[0]) > 0
            and isinstance(geom[0][0], (list, tuple))
        ):
            geom = geom[0]

        result: list[tuple[float, float]] = []
        if isinstance(geom, list):
            for pt in geom:
                if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    result.append((float(pt[0]), float(pt[1])))
                elif isinstance(pt, dict) and "x" in pt and "y" in pt:
                    result.append((float(pt["x"]), float(pt["y"])))
        return result

    @staticmethod
    def _simplify_polygon(points: list[list[int]]) -> list[list[int]]:
        """Remove redundant collinear vertices from a polygon."""
        if len(points) < 3:
            return points

        # Remove consecutive duplicate points
        cleaned: list[list[int]] = []
        for p in points:
            if not cleaned or (cleaned[-1][0] != p[0] or cleaned[-1][1] != p[1]):
                cleaned.append(p)

        # Remove closing point if duplicate of start point
        if len(cleaned) > 2 and cleaned[0] == cleaned[-1]:
            cleaned.pop()

        if len(cleaned) < 3:
            return cleaned

        simplified = list(cleaned)
        changed = True
        while changed and len(simplified) >= 3:
            changed = False
            n = len(simplified)
            i = 0
            while i < n and n >= 3:
                prev_pt = simplified[(i - 1) % n]
                curr_pt = simplified[i]
                next_pt = simplified[(i + 1) % n]

                dx1 = curr_pt[0] - prev_pt[0]
                dy1 = curr_pt[1] - prev_pt[1]
                dx2 = next_pt[0] - curr_pt[0]
                dy2 = next_pt[1] - curr_pt[1]

                # Cross product: (dx1 * dy2 - dy1 * dx2)
                cross = dx1 * dy2 - dy1 * dx2
                dot = dx1 * dx2 + dy1 * dy2

                # If cross product is 0 and direction is the same (dot >= 0), curr_pt is collinear
                if cross == 0 and dot >= 0:
                    simplified.pop(i)
                    changed = True
                    n -= 1
                else:
                    i += 1

        return simplified

    @staticmethod
    def _calculate_centroid(points: list[list[int]]) -> tuple[float, float]:
        """Calculate the centroid (x, y) of a polygon."""
        if not points:
            return 0.0, 0.0
        n = len(points)
        if n < 3:
            avg_x = sum(p[0] for p in points) / n
            avg_y = sum(p[1] for p in points) / n
            return round(avg_x, 1), round(avg_y, 1)

        signed_area = 0.0
        cx = 0.0
        cy = 0.0
        for i in range(n):
            x0, y0 = points[i]
            x1, y1 = points[(i + 1) % n]
            cross = x0 * y1 - x1 * y0
            signed_area += cross
            cx += (x0 + x1) * cross
            cy += (y0 + y1) * cross

        signed_area *= 0.5
        if abs(signed_area) < 1e-6:
            avg_x = sum(p[0] for p in points) / n
            avg_y = sum(p[1] for p in points) / n
            return round(avg_x, 1), round(avg_y, 1)

        cx = cx / (6.0 * signed_area)
        cy = cy / (6.0 * signed_area)
        return round(cx, 1), round(cy, 1)

    @staticmethod
    def _get_room_icon(name: str) -> str:
        """Assign an MDI icon based on room name."""
        lower = name.lower()
        mapping: list[tuple[tuple[str, ...], str]] = [
            (("living", "salon", "salón", "comedor", "lounge", "sitting", "estar"), "mdi:sofa"),
            (("bed", "dormitorio", "habitacion", "habitación", "cuarto", "noche"), "mdi:bed"),
            (("kitchen", "cocina"), "mdi:silverware-fork-knife"),
            (("bath", "baño", "bano", "aseo", "toilet", "wc", "lavabo", "ducha"), "mdi:shower"),
            (("corridor", "hall", "pasillo", "entry", "entrada", "recibidor", "vestibulo", "vestíbulo"), "mdi:hallway"),
            (("office", "despacho", "estudio", "study", "work"), "mdi:desk"),
            (("balcony", "balcon", "balcón", "terraza", "patio"), "mdi:balcony"),
            (("laundry", "lavadero", "colada"), "mdi:washing-machine"),
            (("storage", "trastero", "despensa", "closet", "almacen", "almacén"), "mdi:cupboard"),
            (("gym", "gimnasio"), "mdi:dumbbell"),
            (("garage", "cochera"), "mdi:garage"),
        ]
        for keywords, icon in mapping:
            if any(kw in lower for kw in keywords):
                return icon
        return "mdi:floor-plan"

    def _parse_map_zip(
        self, zip_bytes: bytes
    ) -> tuple[bytes | None, dict[str, Any], tuple[int, int], dict[str, str]]:
        """Parse map zip archive from S3, returning (png_bytes, map_rooms, map_size, rooms)."""
        map_png: bytes | None = None
        map_rooms: dict[str, Any] = {}
        rooms: dict[str, str] = {}
        width, height = 0, 0

        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            namelist = zf.namelist()

            # 1. Extract map.pgm / refined/map.pgm and convert to PNG
            pgm_name = None
            for candidate in ("refined/map.pgm", "map.pgm"):
                if candidate in namelist:
                    pgm_name = candidate
                    break
            if not pgm_name:
                for name in namelist:
                    if name.endswith(".pgm"):
                        pgm_name = name
                        break

            if pgm_name:
                try:
                    from PIL import Image

                    pgm_data = zf.read(pgm_name)
                    image = Image.open(io.BytesIO(pgm_data))
                    width, height = image.size
                    buf = io.BytesIO()
                    image.save(buf, format="PNG")
                    map_png = buf.getvalue()
                except Exception as exc:
                    _LOGGER.warning("Failed to convert %s to PNG: %s", pgm_name, exc)

            # Save PNG to disk at config/www/switchbot_map/map.png
            if map_png:
                try:
                    output_dir = self.hass.config.path("www", "switchbot_map")
                    os.makedirs(output_dir, exist_ok=True)
                    file_path = os.path.join(output_dir, "map.png")
                    with open(file_path, "wb") as f:
                        f.write(map_png)
                except Exception as exc:
                    _LOGGER.warning("Failed to save map to %s: %s", file_path, exc)

            # 2. Read map.json for resolution and origin
            resolution = 0.05
            origin = [0.0, 0.0, 0.0]
            if "map.json" in namelist:
                try:
                    map_meta = json.loads(zf.read("map.json").decode("utf-8"))
                    resolution = float(map_meta.get("resolution", 0.05))
                    origin = map_meta.get("origin", [0.0, 0.0, 0.0])
                except Exception as exc:
                    _LOGGER.warning("Failed to parse map.json: %s", exc)
            origin_x = float(origin[0]) if len(origin) > 0 else 0.0
            origin_y = float(origin[1]) if len(origin) > 1 else 0.0

            # 3. Parse labels.json
            if "labels.json" in namelist:
                try:
                    labels = json.loads(zf.read("labels.json").decode("utf-8"))
                    raw_rooms = labels.get("data", labels) if isinstance(labels, dict) else labels
                    if isinstance(raw_rooms, list):
                        for r in raw_rooms:
                            room_id = r.get("id", "")
                            name = r.get("name", room_id)
                            if not room_id.startswith("ROOM_"):
                                continue
                            rooms[room_id] = name

                            raw_geom = self._extract_polygon_meters(r)
                            if not raw_geom or height <= 0 or resolution <= 0:
                                continue

                            # Transform coordinates
                            pixel_points: list[list[int]] = []
                            for mx, my in raw_geom:
                                px = int(round((mx - origin_x) / resolution))
                                py = int(round(height - ((my - origin_y) / resolution)))
                                pixel_points.append([px, py])

                            # Simplify collinear points
                            simplified = self._simplify_polygon(pixel_points)

                            # Compute centroid
                            cx, cy = self._calculate_centroid(simplified)

                            # Assign MDI icon
                            icon = self._get_room_icon(name)

                            map_rooms[room_id] = {
                                "name": name,
                                "outline": simplified,
                                "x": cx,
                                "y": cy,
                                "icon": icon,
                            }
                except Exception as exc:
                    _LOGGER.warning("Failed to parse labels.json: %s", exc)

        return map_png, map_rooms, (width, height), rooms

    async def async_refresh_rooms(self) -> None:
        """Refresh rooms and map — branches per device type."""
        if self._is_k10() or self._is_k10_pro():
            await self.async_refresh_k10_rooms()
            return

        # S10: download map from S3
        try:
            props = await self.async_get_properties(
                [PROP_MAP_INFO, PROP_AWS_CREDS, PROP_S3_BUCKET, PROP_ROOM_PLANS]
            )
        except UpdateFailed:
            _LOGGER.warning("Failed to fetch map properties for room refresh")
            return

        creds = props.get(PROP_AWS_CREDS)
        map_info = props.get(PROP_MAP_INFO)
        bucket = props.get(PROP_S3_BUCKET, "prod-eu-sweeper-origin")

        # If credentials are missing or expired, attempt to wake the robot to obtain fresh S3 tokens
        if not creds or not isinstance(creds, dict) or creds.get("expiration", 0) < time.time():
            _LOGGER.info("AWS credentials expired or missing, waking robot to refresh")
            try:
                await self.async_send_command(CMD_CONTROL, {"0": "pause"})
                import asyncio
                await asyncio.sleep(10)
                props = await self.async_get_properties(
                    [PROP_MAP_INFO, PROP_AWS_CREDS, PROP_S3_BUCKET, PROP_ROOM_PLANS]
                )
                creds = props.get(PROP_AWS_CREDS)
                map_info = props.get(PROP_MAP_INFO)
                bucket = props.get(PROP_S3_BUCKET, bucket)
            except Exception as exc:
                _LOGGER.warning("Failed to wake robot for AWS credentials: %s", exc)

        # Try downloading map from S3 if credentials are available and valid
        zip_bytes: bytes | None = None
        if creds and isinstance(creds, dict) and creds.get("expiration", 0) >= time.time():
            resource = map_info.get("resource") if isinstance(map_info, dict) else None
            if resource:
                try:
                    import aiobotocore.session

                    boto_session = aiobotocore.session.get_session()
                    async with boto_session.create_client(
                        "s3",
                        region_name=S3_REGION,
                        aws_access_key_id=creds["accessKeyId"],
                        aws_secret_access_key=creds["secretAccessKey"],
                        aws_session_token=creds["sessionToken"],
                    ) as s3:
                        resp = await s3.get_object(Bucket=bucket, Key=resource)
                        zip_bytes = await resp["Body"].read()
                except Exception as exc:
                    _LOGGER.warning("Failed to download map from S3: %s", exc)

        if zip_bytes:
            try:
                map_png, map_rooms, map_size, rooms = await self.hass.async_add_executor_job(
                    self._parse_map_zip, zip_bytes
                )
                if map_png:
                    self._map_png = map_png
                if map_rooms:
                    self._map_rooms = map_rooms
                if map_size[0] > 0 and map_size[1] > 0:
                    self._map_size = map_size
                if rooms:
                    self._rooms = rooms
                    self._last_room_refresh = time.time()
                    _LOGGER.info("Loaded %d rooms and map from S3", len(rooms))

                # Persist to entry.options so rooms survive restarts without S3
                new_options = dict(self.entry.options)
                if self._rooms:
                    new_options["rooms"] = self._rooms
                if self._map_rooms:
                    new_options["map_rooms"] = self._map_rooms
                if self._map_size != (0, 0):
                    new_options["map_size"] = list(self._map_size)
                self.hass.config_entries.async_update_entry(self.entry, options=new_options)
                return
            except Exception as exc:
                _LOGGER.warning("Failed to parse map zip: %s", exc)

        # Fallback to room plans property if S3 map was not obtained
        room_plans = props.get(PROP_ROOM_PLANS)
        rooms_from_plans = self._extract_rooms_from_room_plans(room_plans)
        if rooms_from_plans:
            self._rooms = rooms_from_plans
            self._last_room_refresh = time.time()
            _LOGGER.info("Loaded %d rooms from room plans property", len(rooms_from_plans))
            new_options = dict(self.entry.options)
            new_options["rooms"] = self._rooms
            self.hass.config_entries.async_update_entry(self.entry, options=new_options)

    async def _background_room_refresh(self) -> None:
        """Refresh rooms in the background so it doesn't block coordinator updates."""
        try:
            await self.async_refresh_rooms()
            if self._rooms:
                self.async_set_updated_data(self.data | {"rooms": self._rooms})
        except Exception:
            _LOGGER.debug("Background room refresh failed", exc_info=True)

    @property
    def rooms(self) -> dict[str, str]:
        """Return room ID to name mapping."""
        return self._rooms

    @property
    def map_rooms(self) -> dict[str, Any]:
        """Return rooms formatted for vacuum map card."""
        return self._map_rooms

    @property
    def map_size(self) -> tuple[int, int]:
        """Return map size (width, height)."""
        return self._map_size

    @property
    def map_png(self) -> bytes | None:
        """Return map PNG bytes, loading from disk cache if needed."""
        if self._map_png is not None:
            return self._map_png
        try:
            path = self.hass.config.path("www", "switchbot_map", "map.png")
            if os.path.exists(path):
                with open(path, "rb") as f:
                    self._map_png = f.read()
                return self._map_png
        except Exception:
            pass
        return None

    async def _ensure_token(self) -> None:
        """Refresh token if needed."""
        if not self.access_token or time.time() >= self._token_expiry:
            await self.async_login()

    async def _async_update_data_k10(self) -> dict[str, Any]:
        """Fetch real-time status data from K10+ via getstatus."""
        status = await self.async_get_k10_status()

        if time.time() - self._last_room_refresh > 86400:
            self.hass.async_create_task(self._background_room_refresh())

        return {
            "online": status.get("online_status") == "online",
            "battery": status.get("BatteryLevel", 0),
            "work_status": status.get("WorkingStatus", K10_WORK_STATUS_STANDBY),
            "error_code": 0,
            "clean_mode": {
                "fan_level": status.get("SuctionPowLevel", 1),
                "type": "sweep",
                "times": 1,
                "water_level": 1,
            },
            "clean_summary": {},
            "firmware": "",
            "rooms": self._rooms,
        }

    async def _async_update_data_k10_pro(self) -> dict[str, Any]:
        """Fetch real-time status data from K10+ Pro via getstatus."""

        if time.time() - self._last_room_refresh > 86400:
            self.hass.async_create_task(self._background_room_refresh())

        props = await self.async_get_properties(K10PRO_STATUS_PROPS)

        return {
            "online": props.get(K10PRO_PROP_ONLINE, 1) == 1,
            "battery": props.get(K10PRO_PROP_BATTERY, 0),
            "work_status": props.get(K10PRO_PROP_WORK_STATUS, 0),
            "error_code": 0,
            "clean_mode": {
                "fan_level": props.get(K10PRO_PROP_SUCTION_POW_LEVEL, 1),
                "type": "sweep",
                "times": 1,
                "water_level": 1,
            },
            "clean_summary": {},
            "firmware": "",
            "rooms": self._rooms,
        }

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch status data from device."""
        await self._ensure_token()

        if not self.device_mac:
            self.device_mac = self.entry.data.get(CONF_DEVICE_MAC)

        if self._is_k10():
            return await self._async_update_data_k10()

        if self._is_k10_pro():
            return await self._async_update_data_k10_pro()

        props = await self.async_get_properties(STATUS_PROPS)

        if time.time() - self._last_room_refresh > 86400:
            self.hass.async_create_task(self._background_room_refresh())

        return {
            "online": props.get(PROP_ONLINE, False),
            "battery": props.get(PROP_BATTERY, 0),
            "work_status": props.get(PROP_WORK_STATUS, 1),
            "error_code": props.get(PROP_ERROR_CODE, 0),
            "clean_mode": props.get(PROP_CLEAN_MODE, {}),
            "clean_summary": props.get(PROP_CLEAN_SUMMARY, {}),
            "firmware": props.get(PROP_FIRMWARE, ""),
            "rooms": self._rooms,
        }
