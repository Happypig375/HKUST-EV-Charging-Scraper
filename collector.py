import argparse
import asyncio
import csv
import json
import logging
import os
import re
import signal
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import aiohttp
from dotenv import load_dotenv


UTC = timezone.utc


def now_utc_iso() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_timestamp(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(float(value), tz=UTC)
        return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    text = str(value).strip()
    if not text:
        return None

    candidates = [text]
    if text.endswith("Z"):
        candidates.append(text.replace("Z", "+00:00"))

    for candidate in candidates:
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except ValueError:
            continue

    fmts = [
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%d/%m/%Y %H:%M:%S",
    ]
    for fmt in fmts:
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=UTC)
            return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
        except ValueError:
            continue
    return None


# Canonical Carpark ID mapping (Existing -> New) from the mapping spreadsheet.
CARPARK_ID_MAP: dict[str, str] = {
    "LG2-125": "LG2-11",
    "LG2-126": "LG2-09",
    "LG2-127": "LG2-07",
    "LG5-36": "LG5-10",
    "LG5-37": "LG5-09",
    "LG5-38": "LG5-08",
    "LG5-39": "LG5-07",
    "LG5-40": "LG5-06",
    "LG5-41": "LG5-05",
    "LG5-42": "LG5-04",
    "LG5-43": "LG5-03",
    "LG5-44": "LG5-02",
    "LG5-45": "LG5-01",
    "LG5-46": "LG5-13",
    "LG5-47": "LG5-14",
    "LG5-48": "LG5-15",
    "LG5-49": "LG5-16",
    "LG5-50": "LG5-17",
    "LG5-51": "LG5-18",
    "LG5-52": "LG5-19",
    "LG5-53": "LG5-20",
    "LG5-54": "LG5-30",
    "LG5-55": "LG5-29",
    "LG5-56": "LG5-28",
    "LG5-57": "LG5-27",
    "LG5-58": "LG5-26",
    "LG6-01": "LG6-08",
    "LG6-02": "LG6-07",
    "LG6-03": "LG6-06",
    "LG6-04": "LG6-05",
    "LG6-05": "LG6-04",
    "LG6-06": "LG6-03",
    "LG6-07": "LG6-02",
    "LG6-08": "LG6-01",
    "LG6-09": "LG6-10",
    "LG6-10": "LG6-11",
    "LG6-11": "LG6-12",
    "LG6-12": "LG6-13",
    "LG6-13": "LG6-14",
    "LG6-14": "LG6-15",
    "LG6-15": "LG6-16",
    "LG6-16": "LG6-17",
    "LG6-17": "LG6-25",
    "LG6-18": "LG6-24",
    "LG6-19": "LG6-23",
    "LG6-20": "LG6-22",
    "LG6-21": "LG6-21",
    "LG6-22": "LG6-20",
    "LG6-23": "LG6-19",
}


def normalize_charger_id(value: Any) -> str:
    """Normalize charger IDs so cross-source joins are stable.

    Steps:
    1) Canonicalize case/spacing.
    2) Zero-pad LG5/LG6 numeric suffix to 2 digits.
    3) Apply explicit Carpark Existing->New mapping from `CARPARK_ID_MAP`.
    """
    text = str(value or "").strip().upper()
    if not text:
        return ""

    match = re.fullmatch(r"(LG[56])-(\d+)", text)
    if match:
        prefix, number = match.groups()
        text = f"{prefix}-{int(number):02d}"

    return CARPARK_ID_MAP.get(text, text)


@dataclass
class Config:
    api_base_url: str
    token_path: str
    charger_path: str
    app_id: str
    api_key: str
    poll_interval_seconds: int
    http_timeout_seconds: int
    log_level: str
    log_file: str
    sessions_csv: str
    live_csv: str
    portal_api_base: str
    portal_username: str
    portal_password: str
    charger_id_keys: list[str]
    connector_id_keys: list[str]

    @staticmethod
    def from_env() -> "Config":
        load_dotenv()

        # Only these three must be supplied — everything else has a baked-in default.
        secrets = {
            "API_KEY": os.getenv("API_KEY", "").strip(),
            "PORTAL_USERNAME": os.getenv("PORTAL_USERNAME", "").strip(),
            "PORTAL_PASSWORD": os.getenv("PORTAL_PASSWORD", "").strip(),
        }
        missing = [name for name, value in secrets.items() if not value]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        return Config(
            api_base_url="https://ust-ev.cstl.com.hk/portal/api/thirdparty/v1",
            token_path="/accesstoken",
            charger_path="/charger",
            app_id="ust-uat-app",
            api_key=secrets["API_KEY"],
            poll_interval_seconds=30,
            http_timeout_seconds=20,
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            log_file=os.getenv("LOG_FILE", "logs/collector.log"),
            sessions_csv="charging_sessions.csv",
            live_csv="charging_live.csv",
            portal_api_base="https://ust-ev.cstl.com.hk/portal/api/api",
            portal_username=secrets["PORTAL_USERNAME"],
            portal_password=secrets["PORTAL_PASSWORD"],
            charger_id_keys=["chargerid", "charger_id", "chargerno", "chargercode", "chargepointid", "cpid", "name", "id"],
            connector_id_keys=["connectorid", "connector_id", "connectorno", "connector"],
        )


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        text = str(record.getMessage())
        blocked = ["PORTAL_PASSWORD", "API_KEY", "access_token", "Authorization"]
        lowered = text.lower()
        if any(token.lower() in lowered for token in blocked):
            record.msg = "[redacted-sensitive-log-entry]"
            record.args = ()
        return True


def setup_logging(config: Config) -> logging.Logger:
    Path(config.log_file).parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ev_collector")
    logger.setLevel(getattr(logging, config.log_level, logging.INFO))
    logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        "%Y-%m-%dT%H:%M:%SZ",
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(RedactingFilter())
    logger.addHandler(stream_handler)

    file_handler = logging.FileHandler(config.log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(RedactingFilter())
    logger.addHandler(file_handler)

    logging.Formatter.converter = lambda *args: datetime.now(tz=UTC).timetuple()
    return logger


class CsvWriter:
    def __init__(self, path: str, headers: list[str]):
        self.path = Path(path)
        self.headers = headers
        self._lock = asyncio.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists() or self.path.stat().st_size == 0:
            with self.path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=self.headers)
                writer.writeheader()

    async def append_row(self, row: dict[str, Any]) -> None:
        async with self._lock:
            with self.path.open("a", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=self.headers)
                writer.writerow({key: row.get(key, "") for key in self.headers})


class TokenManager:
    def __init__(self, config: Config, http: aiohttp.ClientSession, logger: logging.Logger):
        self.config = config
        self.http = http
        self.logger = logger
        self._token: str | None = None
        self._expires_at: datetime = datetime.now(tz=UTC)
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        async with self._lock:
            refresh_margin = timedelta(seconds=60)
            if self._token and datetime.now(tz=UTC) + refresh_margin < self._expires_at:
                return self._token
            await self._refresh_token()
            return self._token or ""

    async def _refresh_token(self) -> None:
        url = f"{self.config.api_base_url}{self.config.token_path}"
        headers = {
            "X-APP-ID": self.config.app_id,
            "X-API-KEY": self.config.api_key,
            "Accept": "application/json",
        }

        for attempt in range(1, 4):
            try:
                async with self.http.get(url, headers=headers, timeout=self.config.http_timeout_seconds) as response:
                    payload = await response.json(content_type=None)
                    if response.status >= 400:
                        raise RuntimeError(f"Token endpoint returned status {response.status}")

                token = self._extract_token(payload)
                expires_in = self._extract_expiry_seconds(payload)
                if not token:
                    raise RuntimeError("Token not found in response")

                self._token = token
                self._expires_at = datetime.now(tz=UTC) + timedelta(seconds=expires_in)
                self.logger.info("Token refreshed successfully")
                return
            except Exception as exc:
                self.logger.warning("Token refresh attempt %s failed: %s", attempt, exc)
                await asyncio.sleep(min(2 ** attempt, 8))
        raise RuntimeError("Token refresh failed after retries")

    @staticmethod
    def _extract_token(payload: Any) -> str | None:
        if isinstance(payload, dict):
            direct_keys = ["access_token", "accessToken", "token"]
            for key in direct_keys:
                value = payload.get(key)
                if isinstance(value, str) and value:
                    return value
            for child in payload.values():
                token = TokenManager._extract_token(child)
                if token:
                    return token
        elif isinstance(payload, list):
            for item in payload:
                token = TokenManager._extract_token(item)
                if token:
                    return token
        return None

    @staticmethod
    def _extract_expiry_seconds(payload: Any) -> int:
        if isinstance(payload, dict):
            for key in ["expires_in", "expiresIn", "expire", "expiry"]:
                value = payload.get(key)
                if isinstance(value, (int, float)):
                    return max(120, int(value))
            for child in payload.values():
                nested = TokenManager._extract_expiry_seconds(child)
                if nested:
                    return nested
        elif isinstance(payload, list):
            for item in payload:
                nested = TokenManager._extract_expiry_seconds(item)
                if nested:
                    return nested
        return 600


class SessionTracker:
    def __init__(self, state_file: str | None = None):
        self._state: dict[str, dict[str, str | None]] = {}
        self._state_file = Path(state_file) if state_file else None
        if self._state_file and self._state_file.exists():
            try:
                loaded = json.loads(self._state_file.read_text(encoding="utf-8"))
                # Migrate legacy keys to normalized charger IDs on startup.
                migrated: dict[str, dict[str, str | None]] = {}
                for key, value in loaded.items():
                    if not isinstance(key, str) or "::" not in key:
                        continue
                    charger_id, connector_id = key.split("::", 1)
                    normalized = normalize_charger_id(charger_id)
                    migrated[self.make_key(normalized, connector_id)] = value
                self._state = migrated
            except Exception:
                self._state = {}

    def _save(self) -> None:
        if not self._state_file:
            return
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state), encoding="utf-8")
        tmp.replace(self._state_file)

    @staticmethod
    def make_key(charger_id: str, connector_id: str | None) -> str:
        connector = connector_id or charger_id
        return f"{charger_id}::{connector}"

    def transition(
        self,
        charger_id: str,
        connector_id: str | None,
        status: str,
        transaction_start: str | None,
        detected_at: str,
    ) -> dict[str, str] | None:
        key = self.make_key(charger_id, connector_id)
        previous = self._state.get(key, {"status": None, "session_start": None})
        prev_status = (previous.get("status") or "").lower()
        current_status = (status or "").lower()

        result: dict[str, str] | None = None

        if current_status == "charging" and prev_status != "charging":
            start_time = transaction_start or detected_at
            self._state[key] = {"status": status, "session_start": start_time}
            result = {"status": status, "session_start": start_time, "session_end": ""}
        elif current_status != "charging" and prev_status == "charging":
            session_start = previous.get("session_start") or detected_at
            self._state[key] = {"status": status, "session_start": None}
            result = {"status": status, "session_start": session_start, "session_end": detected_at}
        else:
            self._state[key] = {
                "status": status,
                "session_start": previous.get("session_start") if current_status == "charging" else None,
            }

        self._save()
        return result


class PortalApiCollector:
    """Polls the portal REST API directly for live connector telemetry."""

    _AUTH_PATH = "/authenticate"
    _QUICKINFO_PATH = "/v2/quickInfo"
    _TOKEN_LIFETIME_SECONDS = 2700  # 45 minutes

    def __init__(self, config: Config, http: aiohttp.ClientSession, logger: logging.Logger):
        self.config = config
        self.http = http
        self.logger = logger
        self._token: str | None = None
        self._token_expires_at: datetime = datetime.now(tz=UTC)

    async def _ensure_token(self) -> str:
        if self._token and datetime.now(tz=UTC) < self._token_expires_at:
            return self._token
        return await self._authenticate()

    async def _authenticate(self) -> str:
        url = f"{self.config.portal_api_base}{self._AUTH_PATH}"
        body = {"username": self.config.portal_username, "password": self.config.portal_password}
        for attempt in range(1, 4):
            try:
                async with self.http.post(url, json=body) as resp:
                    payload = await resp.json(content_type=None)
                    if resp.status >= 400:
                        raise RuntimeError(f"Portal auth returned HTTP {resp.status}")
                token_raw = (payload.get("response") or {}).get("token") or ""
                token = token_raw.removeprefix("Bearer ").strip()
                if not token:
                    raise RuntimeError("Empty or missing token in portal auth response")
                self._token = token
                self._token_expires_at = datetime.now(tz=UTC) + timedelta(seconds=self._TOKEN_LIFETIME_SECONDS)
                self.logger.info("Portal token refreshed")
                return token
            except Exception as exc:
                self.logger.warning("Portal auth attempt %s failed: %s", attempt, exc)
                if attempt < 3:
                    await asyncio.sleep(min(2 ** attempt, 10))
        raise RuntimeError("Portal authentication failed after retries")

    async def fetch_quickinfo(self) -> dict[str, Any]:
        token = await self._ensure_token()
        url = f"{self.config.portal_api_base}{self._QUICKINFO_PATH}"
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        async with self.http.get(url, headers=headers) as resp:
            payload = await resp.json(content_type=None)
            if resp.status >= 400:
                self._token = None  # force re-auth on next call
                raise RuntimeError(f"quickInfo returned HTTP {resp.status}")
            return payload



class CollectorApp:
    def __init__(self, config: Config, run_seconds: int | None = None):
        self.config = config
        self.run_seconds = run_seconds
        self.logger = setup_logging(config)
        self.stop_event = asyncio.Event()
        self.portal_collector: PortalApiCollector | None = None
        self.session_tracker = SessionTracker(state_file="collector_state.json")
        self.http: aiohttp.ClientSession | None = None
        self.token_manager: TokenManager | None = None
        self.sessions_writer = CsvWriter(
            config.sessions_csv,
            [
                "timestamp_utc",
                "charger_id",
                "canonical_charger_id",
                "connector_id",
                "status",
                "session_start_utc",
                "session_end_utc",
                "charger_status",
                "charger_type",
                "charger_point_model",
                "charge_point_serial_number",
                "charge_box_serial_number",
                "is_enabled",
                "boot_dttm_utc",
                "last_status_dttm_utc",
                "bay_no",
                "connector_name",
                "connector_type",
                "connector_status",
                "connector_status_last_updated_utc",
                "connector_max_output_kw",
                "connector_expected_end_utc",
                "reservation_flag",
                "rsr_status",
                "rsr_id",
                "tariff_max_charging_duration_mins",
                "tariff_max_charging_unit",
                "tariff_max_penalty_unit",
                "tariff_gracing_period",
                "tariff_gracing_period_unit",
                "location_loc_id",
                "location_address",
                "location_station_code",
                "location_contact_number",
                "source",
            ],
        )
        self.live_writer = CsvWriter(
            config.live_csv,
            [
                "timestamp_utc",
                "charger_id",
                "canonical_charger_id",
                "connector_id",
                "current_A",
                "voltage_V",
                "power_kW",
                "power_kW_est",
                "energy_kWh",
                "soc_pct",
                "status",
                "tran_start_utc",
                "tran_stop_utc",
                "last_update_utc",
                "loc_id",
                "source_endpoint",
            ],
        )
        self.dashboard_writer = CsvWriter(
            "charging_dashboard.csv",
            [
                "timestamp_utc",
                "n_charging",
                "n_available",
                "n_unavailable",
                "yesterday_kwh",
                "yesterday_duration_s",
                "today_kwh",
                "today_duration_s",
                "last7d_kwh",
                "last7d_duration_s",
            ],
        )

    async def _init_http(self) -> None:
        timeout = aiohttp.ClientTimeout(total=self.config.http_timeout_seconds)
        self.http = aiohttp.ClientSession(timeout=timeout)
        self.token_manager = TokenManager(self.config, self.http, self.logger)
        self.portal_collector = PortalApiCollector(self.config, self.http, self.logger)

    async def _close_http(self) -> None:
        if self.http:
            await self.http.close()
            self.http = None

    async def api_loop(self) -> None:
        assert self.http is not None
        assert self.token_manager is not None

        while not self.stop_event.is_set():
            cycle_started = now_utc_iso()
            try:
                token = await self.token_manager.get_token()
                url = f"{self.config.api_base_url}{self.config.charger_path}"
                headers = {
                    "X-APP-ID": self.config.app_id,
                    "X-API-KEY": self.config.api_key,
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                }
                async with self.http.get(url, headers=headers) as response:
                    payload = await response.json(content_type=None)
                    if response.status >= 400:
                        raise RuntimeError(f"Charger list status={response.status}")

                rows = self._extract_charger_rows(payload)
                self.logger.info("Fetched charger snapshot rows=%s", len(rows))
                for row in rows:
                    transition = self.session_tracker.transition(
                        charger_id=row["canonical_charger_id"],
                        connector_id=row["connector_id"],
                        status=row["status"],
                        transaction_start=row["session_start"],
                        detected_at=cycle_started,
                    )
                    if transition:
                        await self.sessions_writer.append_row(
                            {
                                "timestamp_utc": cycle_started,
                                "charger_id": row["charger_id"],
                                "canonical_charger_id": row["canonical_charger_id"],
                                "connector_id": row["connector_id"] or row["charger_id"],
                                "status": transition["status"],
                                "session_start_utc": transition["session_start"],
                                "session_end_utc": transition["session_end"],
                                "charger_status": row.get("charger_status"),
                                "charger_type": row.get("charger_type"),
                                "charger_point_model": row.get("charger_point_model"),
                                "charge_point_serial_number": row.get("charge_point_serial_number"),
                                "charge_box_serial_number": row.get("charge_box_serial_number"),
                                "is_enabled": row.get("is_enabled"),
                                "boot_dttm_utc": row.get("boot_dttm_utc"),
                                "last_status_dttm_utc": row.get("last_status_dttm_utc"),
                                "bay_no": row.get("bay_no"),
                                "connector_name": row.get("connector_name"),
                                "connector_type": row.get("connector_type"),
                                "connector_status": row.get("connector_status"),
                                "connector_status_last_updated_utc": row.get("connector_status_last_updated_utc"),
                                "connector_max_output_kw": row.get("connector_max_output_kw"),
                                "connector_expected_end_utc": row.get("connector_expected_end_utc"),
                                "reservation_flag": row.get("reservation_flag"),
                                "rsr_status": row.get("rsr_status"),
                                "rsr_id": row.get("rsr_id"),
                                "tariff_max_charging_duration_mins": row.get("tariff_max_charging_duration_mins"),
                                "tariff_max_charging_unit": row.get("tariff_max_charging_unit"),
                                "tariff_max_penalty_unit": row.get("tariff_max_penalty_unit"),
                                "tariff_gracing_period": row.get("tariff_gracing_period"),
                                "tariff_gracing_period_unit": row.get("tariff_gracing_period_unit"),
                                "location_loc_id": row.get("location_loc_id"),
                                "location_address": row.get("location_address"),
                                "location_station_code": row.get("location_station_code"),
                                "location_contact_number": row.get("location_contact_number"),
                                "source": "thirdparty_charger_api",
                            }
                        )

            except Exception as exc:
                self.logger.warning("API cycle failed: %s", exc)

            await self._wait_next_cycle()

    async def portal_loop(self) -> None:
        assert self.portal_collector is not None
        while not self.stop_event.is_set():
            ts = now_utc_iso()
            try:
                data = await self.portal_collector.fetch_quickinfo()
                connectors = data.get("cpQuickInfoDTOS") or []
                for entry in connectors:
                    raw_charger_id = entry.get("cpNo") or ""
                    canonical_charger_id = normalize_charger_id(raw_charger_id)
                    voltages = entry.get("voltages") or []
                    currents = entry.get("currents") or []
                    voltage = voltages[0] if voltages else None
                    current = currents[0] if currents else None
                    power = entry.get("kw")
                    power_est = None
                    if current is not None and voltage is not None:
                        power_est = round(current * voltage / 1000.0, 3)
                    await self.live_writer.append_row(
                        {
                            "timestamp_utc": ts,
                            "charger_id": raw_charger_id,
                            "canonical_charger_id": canonical_charger_id,
                            "connector_id": entry.get("connectorId") or "",
                            "current_A": "" if current is None else current,
                            "voltage_V": "" if voltage is None else voltage,
                            "power_kW": "" if power is None else power,
                            "power_kW_est": "" if power_est is None else power_est,
                            "energy_kWh": "" if entry.get("kwh") is None else entry.get("kwh"),
                            "soc_pct": "" if entry.get("soc") is None else entry.get("soc"),
                            "status": entry.get("connectorStatus") or "",
                            "tran_start_utc": parse_timestamp(entry.get("tranStartDate")) or "",
                            "tran_stop_utc": parse_timestamp(entry.get("tranStopDate")) or "",
                            "last_update_utc": parse_timestamp(entry.get("lastStatusUpdateDate")) or "",
                            "loc_id": entry.get("locId") if entry.get("locId") is not None else "",
                            "source_endpoint": "/v2/quickInfo",
                        }
                    )

                usage = data.get("cpUsage") or {}
                summary = data.get("cpSummaryByDayRangeQuickInfoDTO") or {}
                yesterday = summary.get("yesterdaySummary") or {}
                today_s = summary.get("todaySummary") or {}
                last7d = summary.get("last7daysSummary") or {}
                await self.dashboard_writer.append_row(
                    {
                        "timestamp_utc": ts,
                        "n_charging": usage.get("noOfChargingConnectors") if usage.get("noOfChargingConnectors") is not None else "",
                        "n_available": usage.get("noOfAvailableConnectors") if usage.get("noOfAvailableConnectors") is not None else "",
                        "n_unavailable": usage.get("noOfUnAvailableConnectors") if usage.get("noOfUnAvailableConnectors") is not None else "",
                        "yesterday_kwh": yesterday.get("kwh") if yesterday.get("kwh") is not None else "",
                        "yesterday_duration_s": yesterday.get("duration") if yesterday.get("duration") is not None else "",
                        "today_kwh": today_s.get("kwh") if today_s.get("kwh") is not None else "",
                        "today_duration_s": today_s.get("duration") if today_s.get("duration") is not None else "",
                        "last7d_kwh": last7d.get("kwh") if last7d.get("kwh") is not None else "",
                        "last7d_duration_s": last7d.get("duration") if last7d.get("duration") is not None else "",
                    }
                )
                self.logger.info(
                    "Portal snapshot: connectors=%s charging=%s",
                    len(connectors),
                    usage.get("noOfChargingConnectors"),
                )
            except Exception as exc:
                self.logger.warning("Portal cycle failed: %s", exc)

            await self._wait_next_cycle()

    async def _wait_next_cycle(self) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=self.config.poll_interval_seconds)
        except asyncio.TimeoutError:
            return

    def _extract_charger_rows(self, payload: Any) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []

        charger_nodes = payload if isinstance(payload, list) else []

        for charger in charger_nodes:
            if not isinstance(charger, dict):
                continue

            charger_id = self._find_identifier(charger, self.config.charger_id_keys)
            if not charger_id:
                continue
            canonical_charger_id = normalize_charger_id(charger_id)

            cp_loc = charger.get("cpLoc") if isinstance(charger.get("cpLoc"), dict) else {}
            current_tx = charger.get("currentTransaction") if isinstance(charger.get("currentTransaction"), dict) else {}
            connectors = charger.get("connectors") if isinstance(charger.get("connectors"), list) else []

            connector_nodes = [c for c in connectors if isinstance(c, dict)]
            if not connector_nodes:
                connector_nodes = [{}]

            for connector in connector_nodes:
                tariff = connector.get("tariff") if isinstance(connector.get("tariff"), dict) else {}

                connector_id = None
                if connector:
                    connector_id = self._find_identifier(connector, self.config.connector_id_keys)
                    if connector_id is None and connector.get("connectorId") is not None:
                        connector_id = str(connector.get("connectorId")).strip() or None

                status = connector.get("status") or charger.get("status") or charger.get("chargerStatus")
                if not status:
                    continue

                session_start = parse_timestamp(
                    current_tx.get("sessionStartDate")
                    or current_tx.get("startDate")
                    or current_tx.get("startDateTime")
                )

                records.append(
                    {
                        "charger_id": charger_id,
                        "canonical_charger_id": canonical_charger_id,
                        "connector_id": connector_id,
                        "status": str(status),
                        "session_start": session_start,
                        "charger_status": charger.get("chargerStatus") or charger.get("status"),
                        "charger_type": charger.get("chargerType"),
                        "charger_point_model": charger.get("chargerPointModel"),
                        "charge_point_serial_number": charger.get("chargePointSerialNumber"),
                        "charge_box_serial_number": charger.get("chargeBoxSerialNumber"),
                        "is_enabled": charger.get("isEnabled"),
                        "boot_dttm_utc": parse_timestamp(charger.get("bootDttm")),
                        "last_status_dttm_utc": parse_timestamp(charger.get("lastStatusDttm")),
                        "bay_no": charger.get("bayNo"),
                        "connector_name": connector.get("name"),
                        "connector_type": connector.get("type"),
                        "connector_status": connector.get("status"),
                        "connector_status_last_updated_utc": parse_timestamp(connector.get("statusLastUpdatedDt")),
                        "connector_max_output_kw": connector.get("connectorMaxOutputKw"),
                        "connector_expected_end_utc": parse_timestamp(connector.get("connectorExpectedChargingEndTimeWithBuffer")),
                        "reservation_flag": connector.get("reservationFlag"),
                        "rsr_status": connector.get("rsrStatus"),
                        "rsr_id": connector.get("rsrId"),
                        "tariff_max_charging_duration_mins": tariff.get("maxChargingDurationMins"),
                        "tariff_max_charging_unit": tariff.get("maxChargingUnit"),
                        "tariff_max_penalty_unit": tariff.get("maxPenaltyUnit"),
                        "tariff_gracing_period": tariff.get("gracingPeriod"),
                        "tariff_gracing_period_unit": tariff.get("gracingPeriodUnit"),
                        "location_loc_id": cp_loc.get("locId"),
                        "location_address": cp_loc.get("address"),
                        "location_station_code": cp_loc.get("stationCode"),
                        "location_contact_number": cp_loc.get("contactNumber"),
                    }
                )

        dedup: dict[tuple[str, str], dict[str, Any]] = {}
        for item in records:
            charger_id = str(item["canonical_charger_id"])
            connector_id = str(item["connector_id"] or item["charger_id"])
            dedup[(charger_id, connector_id)] = item
        return list(dedup.values())

    @staticmethod
    def _normalize_key(key: str) -> str:
        return "".join(ch.lower() for ch in key if ch.isalnum() or ch == "_")

    def _find_identifier(self, node: dict[str, Any], candidates: list[str]) -> str | None:
        normalized = {self._normalize_key(key): value for key, value in node.items()}
        for candidate in candidates:
            target = self._normalize_key(candidate)
            for key, value in normalized.items():
                if key == target and value is not None:
                    text = str(value).strip()
                    if text:
                        return text
        return None

    @staticmethod
    def _find_status(node: dict[str, Any]) -> str | None:
        status_keys = ["status", "chargerstatus", "connectorstatus", "state"]
        normalized = {"".join(ch.lower() for ch in key if ch.isalnum()): value for key, value in node.items()}
        for key in status_keys:
            if key in normalized and normalized[key] is not None:
                return str(normalized[key])
        return None

    async def start(self) -> None:
        await self._init_http()

        if self.run_seconds and self.run_seconds > 0:
            asyncio.create_task(self._stop_after(self.run_seconds))

        loop = asyncio.get_running_loop()
        for signame in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signame, self.stop_event.set)
            except NotImplementedError:
                signal.signal(signame, lambda *_: self.stop_event.set())

        try:
            await asyncio.gather(self.api_loop(), self.portal_loop())
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self.stop_event.set()
        await self._close_http()
        self.logger.info("Collector shutdown complete")

    async def _stop_after(self, seconds: int) -> None:
        await asyncio.sleep(seconds)
        self.logger.info("Stopping after run-seconds=%s", seconds)
        self.stop_event.set()


async def run_app(args: argparse.Namespace) -> None:
    config = Config.from_env()
    app = CollectorApp(config=config, run_seconds=args.run_seconds)
    await app.start()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="HKUST EV charging collector")
    parser.add_argument(
        "--run-seconds",
        type=int,
        default=0,
        help="If set to >0, stop after this many seconds for validation runs",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(run_app(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
