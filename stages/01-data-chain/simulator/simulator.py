"""Deterministic, explicitly synthetic photovoltaic inverter telemetry."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Sequence

if TYPE_CHECKING:
    from fleet import Fleet

DEVICES = ("INV-1001", "INV-1002", "INV-1003")
CHINA_TZ = timezone(timedelta(hours=8))
STOP = threading.Event()

DEFAULT_SEED = 2026
BASELINE_STATION_CODE = "ST-01"
BASELINE_RATED_POWER_KW = 100.0
BASELINE_PROFILE = "generic-pv-100kw"

# Five controlled ten-minute windows per Beijing calendar day. Each window
# pushes exactly one metric of exactly one station device past the backend
# natural-alarm threshold. The window starts are the same 09/11/13/15/17 slots
# the retired controlled-demo scheduler used, so downstream expectations about
# "five cases per day" stay intact while alarms are now derived from telemetry.
WINDOW_MINUTES = 10


@dataclass(frozen=True)
class WindowScenario:
    """One daily telemetry anomaly window for a station.

    ``threshold`` mirrors the backend alarm rule and is documentation and test
    material only: the simulator never evaluates rules or writes alarms.
    ``target`` is the floor applied during the window so the injected value is
    deterministic instead of depending on the time-of-day base value.
    """

    hour: int
    station_code: str
    metric: str
    threshold: float
    target: float


WINDOW_SCENARIOS = (
    WindowScenario(9, "PV-01", "temperature", 55.0, 65.0),
    WindowScenario(11, "PV-02", "voltage", 430.0, 445.0),
    WindowScenario(13, "PV-03", "temperature", 80.0, 92.0),
    WindowScenario(15, "PV-04", "current", 180.0, 200.0),
    WindowScenario(17, "ST-01", "power", 110.0, 112.0),
)
WINDOW_SCENARIO_BY_STATION: Mapping[str, WindowScenario] = {
    scenario.station_code: scenario for scenario in WINDOW_SCENARIOS
}

# paho's MQTT_ERR_SUCCESS. Kept as a literal so offline tests and the shared
# publisher never need to import paho just to compare a publish result code.
MQTT_ERR_SUCCESS = 0
DEFAULT_SHARED_CLIENT_ID = "simulator-fleet"
DEFAULT_FLUSH_LIMIT = 1000


@dataclass(frozen=True)
class DeviceConfig:
    """Frozen runtime identity and stable characteristics for one device.

    This is not a ledger row: it deliberately carries no ``vendor`` /
    ``is_simulated`` / reference-model fields, so building runtime configs can
    never rewrite the original three devices as synthetic ledger entries.
    """

    device_code: str
    station_code: str
    station_name: str
    scope: str
    daylight_mode: str
    daylight_loop_minutes: float | None
    rated_power_kw: float
    profile: str
    group_name: str | None
    derate_factor: float
    temperature_offset_c: float
    voltage_offset_v: float
    station_phase: float
    station_cloud_base: float
    station_device_codes: tuple[str, ...] = ()


def _stable_int(*parts: object) -> int:
    """Derive a deterministic integer from identity parts, independent of list order."""
    digest = hashlib.sha256("|".join(str(part) for part in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _stable_unit(*parts: object) -> float:
    return _stable_int(*parts) / float(1 << 64)


def build_device_configs(fleet: Fleet | None = None, *, seed: int = DEFAULT_SEED) -> tuple[DeviceConfig, ...]:
    """Build frozen runtime configs for every device in the fleet manifest.

    Business devices take power/profile/daylight metadata from the manifest.
    The baseline station fills the existing 100 kW / generic-pv-100kw / loop
    runtime values because its ledger rows only carry identity fields.
    """
    # The current simulator image copies only simulator.py; keep the fleet
    # dependency lazy so that image keeps starting until the fleet rollout.
    from fleet import expand_devices, load_fleet

    manifest = load_fleet() if fleet is None else fleet
    rows = expand_devices(manifest)
    # Same ordering rule as the backend slot planner: device_code ASC per
    # station, so the daily rotation picks the same device on both sides.
    station_codes: dict[str, tuple[str, ...]] = {
        station.station_code: tuple(
            sorted(row["device_code"] for row in rows if row["station_code"] == station.station_code)
        )
        for station in manifest.stations
    }
    configs: list[DeviceConfig] = []
    for row in rows:
        station = manifest.station(row["station_code"])
        if row["scope"] == "business":
            rated_power_kw = float(row["rated_power_kw"])
            profile = str(row["profile"])
            group_name = str(row["group_name"])
            derate_factor = 0.96 + 0.04 * _stable_unit("device-derate", seed, station.station_code, row["device_code"])
            temperature_offset_c = -1.5 + 3.0 * _stable_unit("device-temperature", seed, station.station_code, row["device_code"])
            voltage_offset_v = -2.0 + 4.0 * _stable_unit("device-voltage", seed, station.station_code, row["device_code"])
        else:
            rated_power_kw = BASELINE_RATED_POWER_KW
            profile = BASELINE_PROFILE
            group_name = None
            derate_factor = 1.0
            temperature_offset_c = 0.0
            voltage_offset_v = 0.0
        configs.append(
            DeviceConfig(
                device_code=str(row["device_code"]),
                station_code=station.station_code,
                station_name=station.station_name,
                scope=station.scope,
                daylight_mode=station.daylight_mode,
                daylight_loop_minutes=station.daylight_loop_minutes,
                rated_power_kw=rated_power_kw,
                profile=profile,
                group_name=group_name,
                derate_factor=derate_factor,
                temperature_offset_c=temperature_offset_c,
                voltage_offset_v=voltage_offset_v,
                station_phase=2.0 * math.pi * _stable_unit("station-phase", seed, station.station_code),
                station_cloud_base=0.90 + 0.08 * _stable_unit("station-cloud", seed, station.station_code),
                station_device_codes=station_codes[station.station_code],
            )
        )
    return tuple(configs)


def simulation_day_index(day: date, count: int) -> int:
    """Mirror ``service.SimulationDeviceIndex`` for the Shanghai calendar day.

    The backend derives the day number from ``SimulationDay(day).Unix()/86400``.
    At UTC+8 that is the previous UTC calendar day, so the same expression is
    reproduced here rather than a plain ``date.toordinal()`` difference.
    """
    if count <= 0:
        return 0
    midnight = datetime(day.year, day.month, day.day, tzinfo=CHINA_TZ)
    seconds = int(midnight.timestamp())
    # Go's integer division truncates toward zero; Python floor-divides, so the
    # two differ before 1970. Reproduce the truncation explicitly.
    days = seconds // 86400 if seconds >= 0 else -((-seconds) // 86400)
    if days < 0:
        days = -days
    return days % count


def window_active(instant: datetime, scenario: WindowScenario) -> bool:
    """True inside ``[HH:00, HH:10)`` of the real Beijing sample clock."""
    minute = instant.hour * 60 + instant.minute
    start = scenario.hour * 60
    return start <= minute < start + WINDOW_MINUTES


def window_device_code(scenario: WindowScenario, device_codes: Sequence[str], day: date) -> str | None:
    """Stable per-day device rotation: sorted device_code, next day shifts by one."""
    codes = sorted(device_codes)
    if not codes:
        return None
    return codes[simulation_day_index(day, len(codes))]


def apply_window_injection(
    payload: dict[str, object],
    *,
    device_id: str,
    station_code: str,
    device_codes: Sequence[str],
    instant: datetime,
    window_mode: str,
) -> str | None:
    """Raise one metric of the selected station device inside its daily window.

    Only the payload value changes. No alarm row, fault code or ``fault_active``
    label is produced. Windows are independent of ``--fault-mode``: the retired
    scheduled-fault injection is gone, so ``window_mode`` is the only switch.
    Voltage injection recomputes current to keep the electrical identity intact.
    Current injection is a meter-level overcurrent: power is left untouched so a
    second rule cannot fire. Outside the window nothing is modified, so the
    natural value returns by itself.
    """
    if window_mode != "on":
        return None
    scenario = WINDOW_SCENARIO_BY_STATION.get(station_code)
    if scenario is None or not window_active(instant, scenario):
        return None
    if window_device_code(scenario, device_codes, instant.date()) != device_id:
        return None
    metric = scenario.metric
    if metric == "power":
        power = max(float(payload["power"]), scenario.target)
        payload["power"] = round(power, 3)
        voltage = float(payload["voltage"])
        payload["current"] = round(power * 1000.0 / (math.sqrt(3) * voltage * 0.99), 3)
        payload["status"] = 1
    elif metric == "voltage":
        voltage = max(float(payload["voltage"]), scenario.target)
        payload["voltage"] = round(voltage, 3)
        payload["current"] = round(float(payload["power"]) * 1000.0 / (math.sqrt(3) * voltage * 0.99), 3)
    else:
        payload[metric] = round(max(float(payload[metric]), scenario.target), 3)
    return metric


def log(event: str, **fields: object) -> None:
    print(json.dumps({"at": datetime.now(timezone.utc).isoformat(), "event": event, **fields}, ensure_ascii=False), flush=True)


class Ledger:
    def __init__(self, path: Path | str) -> None:
        if str(path) == ":memory:":
            path = ":memory:"
        else:
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS generated (device_id TEXT NOT NULL, seq INTEGER NOT NULL, ts_ms INTEGER NOT NULL, payload TEXT NOT NULL, fault_active INTEGER NOT NULL, PRIMARY KEY(device_id, seq))")
        self.db.execute("CREATE TABLE IF NOT EXISTS outbox (device_id TEXT NOT NULL, seq INTEGER NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(device_id, seq))")

    def start_ms(self) -> int:
        with self.lock:
            row = self.db.execute("SELECT value FROM meta WHERE key='start_ms'").fetchone()
            if row:
                return int(row[0])
            value = int(time.time()) * 1000
            self.db.execute("INSERT INTO meta(key,value) VALUES('start_ms',?)", (str(value),))
            return value

    def next_seq(self, device_id: str) -> int:
        with self.lock:
            row = self.db.execute("SELECT COALESCE(MAX(seq), -1) + 1 FROM generated WHERE device_id=?", (device_id,)).fetchone()
            return int(row[0])

    def add(self, device_id: str, seq: int, ts_ms: int, payload: str, fault_active: bool) -> None:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                self.db.execute("INSERT INTO generated VALUES (?,?,?,?,?)", (device_id, seq, ts_ms, payload, int(fault_active)))
                self.db.execute("INSERT INTO outbox VALUES (?,?,?)", (device_id, seq, payload))
                self.db.execute("COMMIT")
            except Exception:
                self.db.execute("ROLLBACK")
                raise

    def pending(self, device_id: str) -> list[tuple[int, str]]:
        with self.lock:
            return [(int(row[0]), str(row[1])) for row in self.db.execute("SELECT seq,payload FROM outbox WHERE device_id=? ORDER BY seq", (device_id,))]

    def pending_all(self, limit: int) -> list[tuple[str, int, str]]:
        """Return up to ``limit`` unacknowledged rows without deleting any."""
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        with self.lock:
            rows = self.db.execute(
                "SELECT device_id,seq,payload FROM outbox ORDER BY device_id,seq LIMIT ?",
                (limit,),
            )
            return [(str(row[0]), int(row[1]), str(row[2])) for row in rows]

    def acknowledge(self, device_id: str, seq: int) -> None:
        with self.lock:
            self.db.execute("DELETE FROM outbox WHERE device_id=? AND seq=?", (device_id, seq))

    def counts(self) -> tuple[int, int]:
        with self.lock:
            generated = int(self.db.execute("SELECT COUNT(*) FROM generated").fetchone()[0])
            pending = int(self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])
            return generated, pending

    def export_fault_labels(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock, path.open("w", encoding="utf-8", newline="") as target:
            writer = csv.writer(target)
            writer.writerow(("device_id", "seq", "ts_ms", "fault_precursor_active"))
            writer.writerows(self.db.execute("SELECT device_id,seq,ts_ms,fault_active FROM generated ORDER BY device_id,seq"))


class MidRegistry:
    """Thread-safe ``mid -> (device_code, seq)`` ownership map.

    A mid is owned by exactly one device/sequence pair until it is popped by a
    publish callback. Registering a mid that is still owned is rejected instead
    of silently overwriting the previous owner.
    """

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self._entries: dict[int, tuple[str, int]] = {}

    def register(self, mid: int, device_code: str, seq: int) -> bool:
        with self.lock:
            if mid in self._entries:
                return False
            self._entries[mid] = (device_code, seq)
            return True

    def pop(self, mid: int) -> tuple[str, int] | None:
        with self.lock:
            return self._entries.pop(mid, None)

    def clear(self) -> int:
        with self.lock:
            count = len(self._entries)
            self._entries.clear()
            return count

    def __len__(self) -> int:
        with self.lock:
            return len(self._entries)


class SharedPublisher:
    """One MQTT client shared by the whole fleet, with exact mid ownership.

    The constructor never imports paho and never connects when a fake client or
    ``client_factory`` is injected, so offline tests stay independent of the
    MQTT library and the network. Only the default factory imports paho.

    Locking rules, which remove the publish/PUBACK lock inversion:

    * ``_publish_lock`` serialises ``client.publish`` calls only. No callback
      takes it, so paho's internal ``_out_message_mutex`` is never held by a
      callback that is waiting for a publisher lock.
    * ``_state_lock`` guards short bookkeeping only: the publish-in-progress
      marker, the mid registry and at most one early PUBACK. It is never held
      while ``client.publish`` runs.
    * ``on_publish`` takes ``_state_lock`` briefly, releases it, then writes the
      acknowledgement to the ledger (which has its own lock).
    """

    def __init__(
        self,
        ledger: Ledger,
        *,
        client_id: str = DEFAULT_SHARED_CLIENT_ID,
        topic: str = "device/telemetry",
        client: object | None = None,
        client_factory: Callable[[], object] | None = None,
        registry: MidRegistry | None = None,
        reconnect_flush_limit: int = DEFAULT_FLUSH_LIMIT,
    ) -> None:
        if client is not None and client_factory is not None:
            raise ValueError("pass either client or client_factory, not both")
        if not client_id:
            raise ValueError("client_id must be non-empty")
        if isinstance(reconnect_flush_limit, bool) or not isinstance(reconnect_flush_limit, int) or reconnect_flush_limit <= 0:
            raise ValueError("reconnect_flush_limit must be a positive integer")
        self.ledger = ledger
        self.client_id = client_id
        self.topic = topic
        self.registry = registry if registry is not None else MidRegistry()
        self.reconnect_flush_limit = reconnect_flush_limit
        # Serialises publish calls only; callbacks must never touch it.
        self._publish_lock = threading.Lock()
        # Short-lived bookkeeping shared with the callbacks.
        self._state_lock = threading.Lock()
        self._publishing = False
        self._connection_generation = 0
        self._early_mid: int | None = None
        self._flush_requested = False
        self._flush_worker: threading.Thread | None = None
        factory = client_factory if client_factory is not None else self._default_client_factory
        self.client = client if client is not None else factory()
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_publish = self.on_publish

    def _default_client_factory(self) -> object:
        import paho.mqtt.client as mqtt

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self.client_id, clean_session=False)
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        return client

    def publish(self, device_code: str, seq: int, payload: str) -> int | None:
        """Publish and register the returned mid without holding a callback lock.

        ``client.publish`` runs while holding only ``_publish_lock``, which no
        callback acquires. A synchronous callback can still deliver the PUBACK
        before ``publish`` returns: that one unknown mid is staged for this
        in-flight publish and is only applied when it matches the returned mid.
        Any other unknown mid is dropped.
        """
        with self._publish_lock:
            with self._state_lock:
                self._publishing = True
                self._early_mid = None
                generation = self._connection_generation
            try:
                info = self.client.publish(self.topic, payload, qos=1)
            except BaseException:
                with self._state_lock:
                    self._publishing = False
                    self._early_mid = None
                raise
            # Clear the marker and register in one short state-lock section so a
            # PUBACK either sees the publish in progress (and is staged) or sees
            # the registered mid. The state lock never spans client.publish.
            with self._state_lock:
                early_mid = self._early_mid
                self._early_mid = None
                self._publishing = False
                accepted = (generation == self._connection_generation
                            and info.rc == MQTT_ERR_SUCCESS and isinstance(info.mid, int))
                registered = False
                if accepted and early_mid != info.mid:
                    registered = self.registry.register(info.mid, device_code, seq)
            if not accepted:
                log("publish_deferred", device_id=device_code, seq=seq, code=info.rc)
                return None
            if early_mid is not None and early_mid == info.mid:
                # The acknowledgement already arrived for this exact mid, so the
                # entry never needs to enter the registry.
                self.ledger.acknowledge(device_code, seq)
                return info.mid
            if not registered:
                log("publish_mid_conflict", device_id=device_code, seq=seq, mid=info.mid)
                return None
            return info.mid

    def flush_pending(self, limit: int = DEFAULT_FLUSH_LIMIT) -> int:
        """Publish at most ``limit`` outbox rows and return how many registered."""
        entries = self.ledger.pending_all(limit)
        sent = 0
        for device_code, seq, payload in entries:
            if self.publish(device_code, seq, payload) is not None:
                sent += 1
        return sent

    def _request_flush(self) -> None:
        """Ask the single flush worker to republish pending rows.

        The callback only flips a flag; publishing happens on a worker thread so
        ``on_connect`` never blocks on the publish lock or on paho internals.
        """
        with self._state_lock:
            self._flush_requested = True
            if self._flush_worker is not None:
                return
            worker = threading.Thread(target=self._run_flush, name=f"{self.client_id}-flush", daemon=True)
            self._flush_worker = worker
        worker.start()

    def _run_flush(self) -> None:
        while True:
            with self._state_lock:
                if not self._flush_requested:
                    self._flush_worker = None
                    return
                self._flush_requested = False
            try:
                self.flush_pending(self.reconnect_flush_limit)
            except Exception as exc:  # pragma: no cover - defensive logging
                log("flush_failed", client_id=self.client_id, error=type(exc).__name__)

    def on_connect(self, _client: object, _userdata: object, _flags: object, reason_code: object, _properties: object = None) -> None:
        log("mqtt_connected", client_id=self.client_id, reason=str(reason_code))
        self._request_flush()

    def on_disconnect(self, _client: object, _userdata: object, _flags: object, reason_code: object, _properties: object = None) -> None:
        # Dropping the registry and any staged early mid keeps stale mids from a
        # previous session from acknowledging (or crossing) devices after reconnect.
        with self._state_lock:
            self._connection_generation += 1
            cleared = self.registry.clear()
            self._early_mid = None
        log("mqtt_disconnected", client_id=self.client_id, reason=str(reason_code), cleared_mids=cleared)

    def on_publish(self, _client: object, _userdata: object, mid: int, _reason_code: object, _properties: object = None) -> None:
        if not isinstance(mid, int):
            return
        with self._state_lock:
            entry = self.registry.pop(mid)
            if entry is None:
                # A fast PUBACK may arrive before publish() has the mid. Stage at
                # most one candidate for the single in-flight publish; everything
                # else is unknown and stays unacknowledged.
                if self._publishing and self._early_mid is None:
                    self._early_mid = mid
                return
        device_code, seq = entry
        self.ledger.acknowledge(device_code, seq)

    def start(self, host: str, port: int, user: str | None, password: str | None) -> None:
        if user:
            self.client.username_pw_set(user, password)
        self.client.connect_async(host, port, keepalive=30)
        self.client.loop_start()

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


def daylight_profile_ms(ts_ms: int, elapsed_seconds: float, cycle_minutes: float) -> int:
    """Repeat synthetic 08:00–16:00 daylight while telemetry keeps its real timestamp."""
    if not math.isfinite(cycle_minutes) or cycle_minutes <= 0:
        raise ValueError("daylight cycle must be positive and finite")
    morning = datetime.fromtimestamp(ts_ms / 1000, CHINA_TZ).replace(hour=8, minute=0, second=0, microsecond=0)
    phase = max(0.0, elapsed_seconds) % (cycle_minutes * 60) / (cycle_minutes * 60)
    return round((morning + timedelta(hours=8 * phase)).timestamp() * 1000)


def make_sample(device_id: str, seq: int, ts_ms: int, seed: int, run_id: str, *, profile_ts_ms: int | None = None,
                fault_mode: str = "scheduled", window_mode: str = "on",
                config: DeviceConfig | None = None) -> tuple[str, bool]:
    # ``fault_mode`` is kept for command-line and caller compatibility but is now
    # inert: the deterministic scheduled-fault ramp was removed, so the five
    # telemetry windows are the only source that can push a metric past a rule.
    if fault_mode not in ("scheduled", "off"):
        raise ValueError("fault-mode must be scheduled or off")
    if window_mode not in ("on", "off"):
        raise ValueError("window-mode must be on or off")
    if config is None:
        return _legacy_sample(device_id, seq, ts_ms, seed, run_id, profile_ts_ms=profile_ts_ms,
                              window_mode=window_mode)
    if config.device_code != device_id:
        raise ValueError("device_id does not match runtime config")
    if config.scope == "baseline":
        return _legacy_sample(device_id, seq, ts_ms, seed, run_id, profile_ts_ms=profile_ts_ms,
                              window_mode=window_mode, station_device_codes=config.station_device_codes)
    return _fleet_sample(device_id, config, seq, ts_ms, seed, run_id, profile_ts_ms=profile_ts_ms,
                         window_mode=window_mode)


def _legacy_sample(device_id: str, seq: int, ts_ms: int, seed: int, run_id: str, *, profile_ts_ms: int | None,
                   window_mode: str, station_device_codes: Sequence[str] | None = None) -> tuple[str, bool]:
    """ST-01 baseline shape; fleet mode adds the daily window injection."""
    instant = datetime.fromtimestamp((ts_ms if profile_ts_ms is None else profile_ts_ms) / 1000, CHINA_TZ)
    hour = instant.hour + instant.minute / 60 + instant.second / 3600
    sun = max(0.0, math.sin(math.pi * (hour - 6.0) / 12.0))
    rng = random.Random(seed * 1_000_003 + DEVICES.index(device_id) * 1_000_000 + seq)
    cloud = 0.95 + 0.04 * math.sin(seq / 31 + DEVICES.index(device_id)) + rng.uniform(-0.015, 0.015)
    power = min(100.0, max(0.0, 100.0 * sun**1.6 * cloud))
    voltage = 400.0 + 1.5 * math.sin(seq / 71 + DEVICES.index(device_id)) + rng.uniform(-0.8, 0.8)
    current = power * 1000.0 / (math.sqrt(3) * voltage * 0.99)
    # The retired scheduled-fault ramp is gone: temperature stays purely natural
    # and ``fault_active`` is always false. Windows use the real sample clock.
    fault_instant = datetime.fromtimestamp(ts_ms / 1000, CHINA_TZ)
    active = False
    temperature = 25.0 + 0.25 * power + 3.0 * max(0.0, math.sin(math.pi * (hour - 7.0) / 12.0)) + rng.uniform(-0.35, 0.35)
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "device_id": device_id,
        "station_id": BASELINE_STATION_CODE,
        "seq": seq,
        "ts_ms": ts_ms,
        "voltage": round(voltage, 3),
        "current": round(current, 3),
        "temperature": round(temperature, 3),
        "power": round(power, 3),
        "status": 1 if power > 0.05 else 0,
        "fault_code": 0,
    }
    if station_device_codes is not None:
        apply_window_injection(
            payload,
            device_id=device_id,
            station_code=BASELINE_STATION_CODE,
            device_codes=station_device_codes,
            instant=fault_instant,
            window_mode=window_mode,
        )
    return json.dumps(payload, separators=(",", ":")), active


def _fleet_sample(device_id: str, config: DeviceConfig, seq: int, ts_ms: int, seed: int, run_id: str, *,
                  profile_ts_ms: int | None, window_mode: str) -> tuple[str, bool]:
    instant = datetime.fromtimestamp((ts_ms if profile_ts_ms is None else profile_ts_ms) / 1000, CHINA_TZ)
    hour = instant.hour + instant.minute / 60 + instant.second / 3600
    # Daylight and the main cloud swing are station-level: every device in a
    # station sees the same weather. Only small stable derating, temperature
    # offset and measurement noise differ per device.
    sun = max(0.0, math.sin(math.pi * (hour - 6.0) / 12.0))
    cloud = config.station_cloud_base + 0.04 * math.sin(seq / 31 + config.station_phase)
    rng = random.Random(_stable_int("sample", seed, config.station_code, config.device_code, seq))
    cloud += rng.uniform(-0.008, 0.008)
    power = min(config.rated_power_kw, max(0.0, config.rated_power_kw * sun**1.6 * cloud * config.derate_factor))
    voltage = 400.0 + config.voltage_offset_v + 1.5 * math.sin(seq / 71 + config.station_phase) + rng.uniform(-0.8, 0.8)
    current = power * 1000.0 / (math.sqrt(3) * voltage * 0.99)
    # No scheduled-fault ramp for any scope: baseline and business devices alike
    # stay on the natural curve, and faults reach production only through the
    # five injected windows below.
    fault_instant = datetime.fromtimestamp(ts_ms / 1000, CHINA_TZ)
    active = False
    temperature = 25.0 + 0.25 * power + 3.0 * max(0.0, math.sin(math.pi * (hour - 7.0) / 12.0)) \
        + config.temperature_offset_c + rng.uniform(-0.35, 0.35)
    payload = {
        "schema_version": 1,
        "run_id": run_id,
        "device_id": device_id,
        "station_id": config.station_code,
        "seq": seq,
        "ts_ms": ts_ms,
        "voltage": round(voltage, 3),
        "current": round(current, 3),
        "temperature": round(temperature, 3),
        "power": round(power, 3),
        "status": 1 if power > 0.05 else 0,
        "fault_code": 0,
    }
    apply_window_injection(
        payload,
        device_id=device_id,
        station_code=config.station_code,
        device_codes=config.station_device_codes or (device_id,),
        instant=fault_instant,
        window_mode=window_mode,
    )
    return json.dumps(payload, separators=(",", ":")), active


class Publisher:
    def __init__(self, device_id: str, ledger: Ledger, host: str, port: int, user: str | None, password: str | None) -> None:
        # Imported here so the pure generation/offline tests do not require the
        # MQTT client; runtime behaviour is unchanged.
        import paho.mqtt.client as mqtt

        self.mqtt = mqtt
        self.device_id = device_id
        self.ledger = ledger
        self.pending_mid: dict[int, int] = {}
        self.lock = threading.RLock()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sim-{device_id}", clean_session=False)
        if user:
            self.client.username_pw_set(user, password)
        self.client.on_connect = self.on_connect
        self.client.on_disconnect = self.on_disconnect
        self.client.on_publish = self.on_publish
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)
        self.client.connect_async(host, port, keepalive=30)
        self.client.loop_start()

    def on_connect(self, _client: mqtt.Client, _userdata: object, _flags: object, reason_code: object, _properties: object) -> None:
        log("mqtt_connected", device_id=self.device_id, reason=str(reason_code))
        for seq, payload in self.ledger.pending(self.device_id):
            self.publish(seq, payload)

    def on_disconnect(self, _client: mqtt.Client, _userdata: object, _flags: object, reason_code: object, _properties: object) -> None:
        log("mqtt_disconnected", device_id=self.device_id, reason=str(reason_code))

    def on_publish(self, _client: mqtt.Client, _userdata: object, mid: int, _reason_code: object, _properties: object) -> None:
        with self.lock:
            seq = self.pending_mid.pop(mid, None)
            if seq is not None:
                self.ledger.acknowledge(self.device_id, seq)

    def publish(self, seq: int, payload: str) -> None:
        if not self.client.is_connected():
            return
        with self.lock:
            info = self.client.publish("device/telemetry", payload, qos=1)
            if info.rc == self.mqtt.MQTT_ERR_SUCCESS:
                self.pending_mid[info.mid] = seq
            else:
                log("publish_deferred", device_id=self.device_id, seq=seq, code=info.rc)

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--broker", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--interval-seconds", type=float, default=5.0)
    parser.add_argument("--samples", type=int, default=720, help="total samples per device, including samples from a resumed run")
    parser.add_argument("--continuous", action="store_true", help="keep producing current-time samples until stopped")
    parser.add_argument("--daylight-loop-minutes", type=float, default=0.0,
                        help="continuous demo only: repeat synthetic 08:00–16:00 daylight in this many real minutes; 0 keeps the real day/night cycle")
    parser.add_argument("--fault-mode", choices=("scheduled", "off"), default="scheduled",
                        help="deprecated and inert: the scheduled-fault ramp was removed; accepted only so existing deployments keep starting")
    parser.add_argument("--window-mode", choices=("on", "off"), default="on",
                        help="on: inject the five daily ten-minute telemetry windows; off: publish the natural curve only")
    parser.add_argument("--time-scale", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--run-id", default="manual")
    parser.add_argument("--outbox", type=Path, required=True)
    parser.add_argument("--fault-labels", type=Path, required=True)
    parser.add_argument("--fleet", type=Path, help="validated fleet manifest; omitted keeps the original three-device mode")
    parser.add_argument("--shared-client-id", default=DEFAULT_SHARED_CLIENT_ID)
    parser.add_argument("--mqtt-user")
    parser.add_argument("--mqtt-password")
    args = parser.parse_args()
    if args.samples < 1 or args.interval_seconds <= 0 or args.time_scale <= 0:
        parser.error("samples, interval-seconds, and time-scale must be positive")
    if not math.isfinite(args.daylight_loop_minutes) or args.daylight_loop_minutes < 0:
        parser.error("daylight-loop-minutes must be nonnegative and finite")
    if args.daylight_loop_minutes and not args.continuous:
        parser.error("daylight-loop-minutes requires continuous mode")
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    ledger = Ledger(args.outbox)
    start_ms = ledger.start_ms()
    fleet_mode = args.fleet is not None
    if fleet_mode:
        from fleet import load_fleet

        manifest = load_fleet(args.fleet)
        configs = build_device_configs(manifest, seed=args.seed)
        devices = tuple(config.device_code for config in configs)
        config_by_device = {config.device_code: config for config in configs}
        shared_publisher = SharedPublisher(ledger, client_id=args.shared_client_id)
        shared_publisher.start(args.broker, args.port, args.mqtt_user, args.mqtt_password)
        publishers: list[Publisher] = []
    else:
        devices = DEVICES
        config_by_device: dict[str, DeviceConfig] = {}
        shared_publisher = None
        publishers = [Publisher(device, ledger, args.broker, args.port, args.mqtt_user, args.mqtt_password) for device in devices]
    log("simulator_started", run_id=args.run_id, continuous=args.continuous, samples_per_device=args.samples, interval_seconds=args.interval_seconds, time_scale=args.time_scale,
        daylight_loop_minutes=args.daylight_loop_minutes, window_mode=args.window_mode,
        fault_mode_ignored=args.fault_mode, fault_clock="sample_timestamp",
        fleet_mode=fleet_mode, fleet_path=str(args.fleet) if args.fleet else None, device_count=len(devices))
    loop_started = time.monotonic()
    next_tick = loop_started
    interrupted = False
    try:
        while not STOP.is_set() and (args.continuous or any(ledger.next_seq(device) < args.samples for device in devices)):
            cycle_started = time.monotonic()
            for index, device in enumerate(devices):
                seq = ledger.next_seq(device)
                if not args.continuous and seq >= args.samples:
                    continue
                ts_ms = int(time.time() * 1000) if args.continuous else start_ms + round(seq * args.interval_seconds * 1000 * args.time_scale)
                config = config_by_device.get(device)
                loop_minutes = args.daylight_loop_minutes
                if fleet_mode and config is not None:
                    loop_minutes = config.daylight_loop_minutes or 0.0
                profile_ts_ms = daylight_profile_ms(ts_ms, time.monotonic() - loop_started, loop_minutes) if args.continuous and loop_minutes else None
                payload, active = make_sample(device, seq, ts_ms, args.seed, args.run_id,
                                              profile_ts_ms=profile_ts_ms, fault_mode=args.fault_mode,
                                              window_mode=args.window_mode, config=config)
                ledger.add(device, seq, ts_ms, payload, active)
                if shared_publisher is not None:
                    shared_publisher.publish(device, seq, payload)
                else:
                    publishers[index].publish(seq, payload)
                if fleet_mode and index + 1 < len(devices):
                    target = cycle_started + args.interval_seconds * (index + 1) / len(devices)
                    STOP.wait(max(0.0, target - time.monotonic()))
            generated, pending = ledger.counts()
            if generated % 180 == 0:
                log("generation_progress", generated=generated, pending= pending)
            next_tick += args.interval_seconds
            STOP.wait(max(0.0, next_tick - time.monotonic()))
        interrupted = STOP.is_set()
        if not interrupted:
            deadline = time.monotonic() + 90
            while not STOP.is_set() and ledger.counts()[1] and time.monotonic() < deadline:
                STOP.wait(0.5)
    finally:
        for publisher in publishers:
            publisher.close()
        if shared_publisher is not None:
            shared_publisher.close()
        if not args.continuous:
            ledger.export_fault_labels(args.fault_labels)
        generated, pending = ledger.counts()
        log("simulator_stopped", generated=generated, pending=pending, interrupted=interrupted)
    return 130 if interrupted else (0 if pending == 0 else 2)


if __name__ == "__main__":
    sys.exit(main())
