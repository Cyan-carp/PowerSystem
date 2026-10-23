"""Fleet manifest loading, strict validation and device expansion.

``fleet.json`` is the single source of truth for simulated stations and
devices. Business stations generate their devices from a station template;
the baseline station lists its existing devices explicitly and never receives
synthetic ledger identity fields (``vendor`` / ``is_simulated`` /
``reference_vendor`` / ``reference_model``).
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

DEFAULT_FLEET_PATH = Path(__file__).with_name("fleet.json")
SCHEMA_VERSION = 1
SYNTHETIC_VENDOR = "synthetic-fleet"
DEVICE_TYPE = "inverter"
CODE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SCOPES = ("business", "baseline")
KINDS = ("pv",)
DAYLIGHT_MODES = ("real", "loop")

_TOP_LEVEL_KEYS = frozenset({"schema_version", "fleet_id", "sampling_seconds", "stations"})
_STATION_KEYS = frozenset(
    {
        "station_code",
        "station_name",
        "scope",
        "kind",
        "daylight_mode",
        "device_prefix",
        "device_count",
        "rated_power_kw",
        "profile",
        "group_size",
        "explicit",
        "daylight_loop_minutes",
    }
)
_STATION_REQUIRED_KEYS = frozenset({"station_code", "station_name", "scope", "kind", "daylight_mode"})
_BUSINESS_KEYS = frozenset({"device_prefix", "device_count", "rated_power_kw", "profile", "group_size"})


class FleetError(ValueError):
    """Raised when a fleet manifest is malformed, inconsistent or unsafe."""


@dataclass(frozen=True)
class Station:
    station_code: str
    station_name: str
    scope: str
    kind: str
    daylight_mode: str
    device_prefix: str | None = None
    device_count: int = 0
    rated_power_kw: float = 0.0
    profile: str | None = None
    group_size: int = 0
    explicit: tuple[str, ...] = ()
    daylight_loop_minutes: float | None = None

    @property
    def device_total(self) -> int:
        return len(self.explicit) if self.scope == "baseline" else self.device_count


@dataclass(frozen=True)
class Fleet:
    schema_version: int
    fleet_id: str
    sampling_seconds: int
    stations: tuple[Station, ...]

    @property
    def business_stations(self) -> tuple[Station, ...]:
        return tuple(station for station in self.stations if station.scope == "business")

    @property
    def baseline_stations(self) -> tuple[Station, ...]:
        return tuple(station for station in self.stations if station.scope == "baseline")

    def station(self, station_code: str) -> Station:
        for station in self.stations:
            if station.station_code == station_code:
                return station
        raise KeyError(station_code)


def load_fleet(path: str | Path | None = None) -> Fleet:
    """Load and strictly validate a fleet manifest from disk."""
    source = Path(path) if path is not None else DEFAULT_FLEET_PATH
    try:
        text = source.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FleetError(f"{source}: fleet manifest not found") from exc
    except OSError as exc:
        raise FleetError(f"{source}: cannot read fleet manifest: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FleetError(f"{source}: invalid JSON: {exc}") from exc
    return parse_fleet(data, source=str(source))


def parse_fleet(data: Any, *, source: str = "<memory>") -> Fleet:
    """Validate already-parsed manifest data and return a ``Fleet``."""
    if not isinstance(data, dict):
        _fail(source, "top level must be a JSON object")
    _reject_unknown(data, _TOP_LEVEL_KEYS, "top level", source)
    missing = sorted(_TOP_LEVEL_KEYS - set(data))
    if missing:
        _fail(source, f"missing top-level fields: {missing}")

    schema_version = data["schema_version"]
    if not _is_int(schema_version) or schema_version != SCHEMA_VERSION:
        _fail(source, f"schema_version must be the integer {SCHEMA_VERSION}")

    fleet_id = data["fleet_id"]
    if not isinstance(fleet_id, str) or not fleet_id.strip():
        _fail(source, "fleet_id must be a non-empty string")

    sampling_seconds = _positive_int(data["sampling_seconds"], "sampling_seconds", source)

    raw_stations = data["stations"]
    if not isinstance(raw_stations, list) or not raw_stations:
        _fail(source, "stations must be a non-empty list")

    stations = tuple(
        _parse_station(raw_station, index, source) for index, raw_station in enumerate(raw_stations)
    )
    _require_unique([station.station_code for station in stations], "station_code", source)
    device_codes: list[str] = []
    for station in stations:
        device_codes.extend(_station_device_codes(station, source))
    _require_unique(device_codes, "device_code", source)

    return Fleet(
        schema_version=schema_version,
        fleet_id=fleet_id,
        sampling_seconds=sampling_seconds,
        stations=stations,
    )


def expand_devices(fleet: Fleet) -> list[dict[str, Any]]:
    """Expand a validated fleet into device rows.

    Business rows carry the full synthetic ledger identity. Baseline rows only
    repeat the existing device codes and their station, so importing them can
    never rewrite the three original devices as ``synthetic-fleet`` entries.
    """
    devices: list[dict[str, Any]] = []
    seen: set[str] = set()
    for station in fleet.stations:
        if station.scope == "business":
            for index in range(1, station.device_count + 1):
                code = _business_device_code(station, index, "<fleet>")
                _require_new_code(code, seen)
                devices.append(
                    {
                        "device_code": code,
                        "name": f"{station.station_name} {index:02d}号逆变器",
                        "dev_type": DEVICE_TYPE,
                        "vendor": SYNTHETIC_VENDOR,
                        "is_simulated": True,
                        "reference_vendor": None,
                        "reference_model": None,
                        "station_code": station.station_code,
                        "station_name": station.station_name,
                        "group_name": _group_name(station.station_code, index, station.group_size),
                        "rated_power_kw": station.rated_power_kw,
                        "profile": station.profile,
                        "scope": station.scope,
                    }
                )
        else:
            for code in station.explicit:
                _require_new_code(code, seen)
                devices.append(
                    {
                        "device_code": code,
                        "dev_type": DEVICE_TYPE,
                        "station_code": station.station_code,
                        "station_name": station.station_name,
                        "scope": station.scope,
                    }
                )
    return devices


def _parse_station(raw: Any, index: int, source: str) -> Station:
    label = f"stations[{index}]"
    if not isinstance(raw, dict):
        _fail(source, f"{label} must be a JSON object")
    _reject_unknown(raw, _STATION_KEYS, label, source)
    missing = sorted(_STATION_REQUIRED_KEYS - set(raw))
    if missing:
        _fail(source, f"{label} is missing required fields: {missing}")

    station_code = _code(raw["station_code"], f"{label}.station_code", source)
    station_name = _text(raw["station_name"], f"{label}.station_name", source)

    scope = raw["scope"]
    if scope not in SCOPES:
        _fail(source, f"{label}.scope must be one of {list(SCOPES)}")
    kind = raw["kind"]
    if kind not in KINDS:
        _fail(source, f"{label}.kind must be one of {list(KINDS)}")
    daylight_mode = raw["daylight_mode"]
    if daylight_mode not in DAYLIGHT_MODES:
        _fail(source, f"{label}.daylight_mode must be one of {list(DAYLIGHT_MODES)}")

    loop_minutes = raw.get("daylight_loop_minutes")
    if daylight_mode == "loop":
        if loop_minutes is None:
            _fail(source, f"{label}.daylight_loop_minutes is required when daylight_mode is loop")
        loop_minutes = _positive_number(loop_minutes, f"{label}.daylight_loop_minutes", source)
    elif loop_minutes is not None:
        _fail(source, f"{label}.daylight_loop_minutes is only valid with daylight_mode loop")

    if scope == "business":
        if "explicit" in raw:
            _fail(source, f"{label}.explicit is not allowed for business stations")
        missing = sorted(_BUSINESS_KEYS - set(raw))
        if missing:
            _fail(source, f"{label} is missing business fields: {missing}")
        return Station(
            station_code=station_code,
            station_name=station_name,
            scope=scope,
            kind=kind,
            daylight_mode=daylight_mode,
            device_prefix=_code(raw["device_prefix"], f"{label}.device_prefix", source),
            device_count=_positive_int(raw["device_count"], f"{label}.device_count", source),
            rated_power_kw=_positive_number(raw["rated_power_kw"], f"{label}.rated_power_kw", source),
            profile=_text(raw["profile"], f"{label}.profile", source),
            group_size=_positive_int(raw["group_size"], f"{label}.group_size", source),
            daylight_loop_minutes=loop_minutes,
        )

    forbidden = sorted(_BUSINESS_KEYS & set(raw))
    if forbidden:
        _fail(source, f"{label} must not declare business fields: {forbidden}")
    if "explicit" not in raw:
        _fail(source, f"{label}.explicit is required for baseline stations")
    raw_explicit = raw["explicit"]
    if not isinstance(raw_explicit, list) or not raw_explicit:
        _fail(source, f"{label}.explicit must be a non-empty list")
    explicit = tuple(
        _code(item, f"{label}.explicit[{position}]", source) for position, item in enumerate(raw_explicit)
    )
    _require_unique(list(explicit), f"{label}.explicit", source)
    return Station(
        station_code=station_code,
        station_name=station_name,
        scope=scope,
        kind=kind,
        daylight_mode=daylight_mode,
        explicit=explicit,
        daylight_loop_minutes=loop_minutes,
    )


def _station_device_codes(station: Station, source: str) -> list[str]:
    if station.scope == "business":
        return [
            _business_device_code(station, index, source)
            for index in range(1, station.device_count + 1)
        ]
    return list(station.explicit)


def _business_device_code(station: Station, index: int, source: str) -> str:
    code = f"{station.station_code}-{station.device_prefix}-{index:02d}"
    if not CODE_PATTERN.fullmatch(code):
        _fail(source, f"generated device_code {code!r} must match [A-Za-z0-9_-]{{1,64}}")
    return code


def _group_name(station_code: str, index: int, group_size: int) -> str:
    group_index = (index - 1) // group_size + 1
    return f"{station_code}-{group_index:02d}区"


def _reject_unknown(raw: dict[str, Any], allowed: frozenset[str], label: str, source: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        _fail(source, f"{label} has unknown fields: {unknown}")


def _require_unique(values: list[str], field: str, source: str) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        else:
            seen.add(value)
    if duplicates:
        _fail(source, f"duplicate {field}: {sorted(duplicates)}")


def _require_new_code(code: str, seen: set[str]) -> None:
    if code in seen:
        raise FleetError(f"duplicate device_code: {code}")
    seen.add(code)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_int(value: Any, field: str, source: str) -> int:
    if not _is_int(value) or value <= 0:
        _fail(source, f"{field} must be a positive integer")
    return value


def _positive_number(value: Any, field: str, source: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _fail(source, f"{field} must be a positive number")
    if not math.isfinite(value) or value <= 0:
        _fail(source, f"{field} must be a positive number")
    return float(value)


def _text(value: Any, field: str, source: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail(source, f"{field} must be a non-empty string")
    return value


def _code(value: Any, field: str, source: str) -> str:
    if not isinstance(value, str) or not CODE_PATTERN.fullmatch(value):
        _fail(source, f"{field} must match [A-Za-z0-9_-]{{1,64}}")
    return value


def _fail(source: str, message: str) -> NoReturn:
    raise FleetError(f"{source}: {message}")
