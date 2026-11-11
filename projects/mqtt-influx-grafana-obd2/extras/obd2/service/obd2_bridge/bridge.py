"""
The bridge: connects to the adapter with python-OBD, polls readings in groups at
their own rates, reads trouble codes, and publishes it all over MQTT (specs §8.6):

  sensors/<id>/<measurement>  {"value": 2150, "unit": "rpm"}   fast, slow and voltage groups
  sensors/<id>/raw            {"values": [...]}                 1 Hz, feeds the edge runner
  alerts/<id>/dtc             codes, MIL, freeze frame          when they change
  status/<id>/online          adapter, protocol, vehicle        retained

It never stops on its own: with no adapter, or the ignition off, it says so on the
status topic and keeps trying.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Protocol

import obd
from obd.decoders import dtc as decode_dtcs
from obd.protocols import ECU

from .config import Config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Measurement:
    name: str
    command: str      # python-OBD command name
    unit: str
    group: str        # fast, slow or voltage


MEASUREMENTS = (
    Measurement("rpm", "RPM", "rpm", "fast"),
    Measurement("speed", "SPEED", "km/h", "fast"),
    Measurement("maf", "MAF", "g/s", "fast"),
    Measurement("engine_load", "ENGINE_LOAD", "%", "slow"),
    Measurement("coolant_temp", "COOLANT_TEMP", "C", "slow"),
    Measurement("stft_b1", "SHORT_FUEL_TRIM_1", "%", "slow"),
    Measurement("ltft_b1", "LONG_FUEL_TRIM_1", "%", "slow"),
    Measurement("intake_temp", "INTAKE_TEMP", "C", "slow"),
    Measurement("throttle_pos", "THROTTLE_POS", "%", "slow"),
    Measurement("fuel_level", "FUEL_LEVEL", "%", "slow"),
    # The adapter's own reading of pin 16, not a PID: it works with the ignition off
    Measurement("battery_v", "ELM_VOLTAGE", "V", "voltage"),
)

# The edge runner's feature vector (design §3.5), in this order
RAW = ("rpm", "engine_load", "coolant_temp", "stft_b1", "ltft_b1", "maf", "speed")

# python-OBD has no mode 0A command
GET_PERMANENT_DTC = obd.OBDCommand(
    "GET_PERMANENT_DTC", "Get permanent DTCs", b"0A", 0, decode_dtcs, ECU.ALL, False
)

# Rounds of a group with no answer at all before the car counts as gone
MISSES_BEFORE_LOST = 5
RETRY_NO_ADAPTER = (2.0, 30.0)   # backoff, from and to, in seconds
RETRY_NO_VEHICLE = 10.0          # ignition off: look for the car again this often


class Publisher(Protocol):
    def publish(self, topic: str, payload: dict, retain: bool = False, qos: int = 0) -> None: ...
    def set_status(self, topic: str, payload: dict) -> None: ...


def decode_vin(response) -> str | None:
    """The VIN from a mode 09 PID 02 answer. python-OBD's own decoder strips
    leading and trailing 0, 1 and 2 characters, so read the bytes directly."""
    if response.is_null() or not response.messages:
        return None
    data = bytes(response.messages[0].data[3:20])
    vin = data.decode("ascii", "replace")
    return vin if len(vin) == 17 and vin.isalnum() else None


class Bridge:
    def __init__(self, cfg: Config, publisher: Publisher,
                 open_adapter: Callable[[Config], obd.OBD] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg
        self.pub = publisher
        self.open_adapter = open_adapter or self._open
        self.clock = clock
        self.measurements = [m for m in MEASUREMENTS if m.name in cfg.measurements]
        self._lock = threading.Lock()
        self._status: dict = {}
        self._probe: dict = {"device_id": cfg.device_id, "vehicle": False}
        self._dtc: dict | None = None
        self._dtc_read_at: float | None = None
        self._latest: dict[str, tuple[float, float]] = {}
        self.connects = 0

    # ---- topics ------------------------------------------------------------

    def topic(self, kind: str, leaf: str) -> str:
        return f"{kind}/{self.cfg.device_id}/{leaf}"

    # ---- state for the HTTP API --------------------------------------------

    def status(self) -> dict:
        with self._lock:
            return dict(self._status)

    def probe(self) -> dict:
        with self._lock:
            return dict(self._probe)

    def dtc(self) -> dict | None:
        with self._lock:
            if self._dtc is None:
                return None
            return {**self._dtc, "read_age_s": round(self.clock() - self._dtc_read_at, 1)}

    def last_reading_age(self) -> float | None:
        with self._lock:
            if not self._latest:
                return None
            return round(self.clock() - max(t for _, t in self._latest.values()), 1)

    # ---- the loop ----------------------------------------------------------

    def _open(self, cfg: Config) -> obd.OBD:
        return obd.OBD(cfg.device, baudrate=cfg.baudrate, protocol=cfg.protocol,
                       fast=True, timeout=0.5, check_voltage=True)

    def run(self, stop: threading.Event) -> None:
        backoff = RETRY_NO_ADAPTER[0]
        while not stop.is_set():
            try:
                conn = self.open_adapter(self.cfg)
            except Exception as e:  # a missing device, a refused socket
                log.warning("adapter: %s", e)
                conn = None
            state = conn.status() if conn is not None else obd.OBDStatus.NOT_CONNECTED

            if state == obd.OBDStatus.NOT_CONNECTED:
                self._set_status(adapter=None, protocol=None, vehicle=False)
                log.info("no adapter at %s; retrying in %.0f s", self.cfg.device, backoff)
                stop.wait(backoff)
                backoff = min(backoff * 2, RETRY_NO_ADAPTER[1])
                continue
            backoff = RETRY_NO_ADAPTER[0]

            try:
                if state != obd.OBDStatus.CAR_CONNECTED:
                    # The adapter answers but the car doesn't: ignition off
                    self._set_status(adapter=self._adapter_name(conn), protocol=None, vehicle=False)
                    self._read_group(conn, "voltage")
                    log.info("adapter up, no vehicle (ignition off?); retrying in %.0f s", RETRY_NO_VEHICLE)
                    stop.wait(RETRY_NO_VEHICLE)
                    continue
                self.connects += 1
                self._on_vehicle(conn)
                why = self._poll(conn, stop)
                if not stop.is_set():
                    log.warning("vehicle connection ended: %s", why)
            except Exception:
                log.exception("unexpected error; reconnecting")
                stop.wait(RETRY_NO_ADAPTER[0])
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

    def _poll(self, conn: obd.OBD, stop: threading.Event) -> str:
        periods = {
            "fast": 1 / self.cfg.fast_hz,
            "slow": 1 / self.cfg.slow_hz,
            "voltage": self.cfg.voltage_interval,
            "dtc": self.cfg.dtc_interval,
        }
        if self.cfg.raw and set(RAW) <= {m.name for m in self.measurements}:
            periods["raw"] = 1.0
        now = self.clock()
        due = dict.fromkeys(periods, now)
        misses = 0
        while not stop.is_set():
            now = self.clock()
            for task in [t for t, at in due.items() if at <= now]:
                if task == "dtc":
                    self._read_dtc(conn)
                elif task == "raw":
                    self._publish_raw()
                else:
                    answered = self._read_group(conn, task)
                    if task in ("fast", "slow") and answered is not None:
                        misses = 0 if answered else misses + 1
                # No catching up after a slow round: the next one is a period from now
                due[task] = max(due[task] + periods[task], now)
            if conn.status() == obd.OBDStatus.NOT_CONNECTED:
                return "adapter disconnected"
            if misses >= MISSES_BEFORE_LOST:
                return f"no answer from the car in {misses} rounds (ignition off?)"
            stop.wait(max(0.0, min(due.values()) - self.clock()))
        return "stopping"

    # ---- connecting --------------------------------------------------------

    def _adapter_name(self, conn: obd.OBD) -> str | None:
        r = conn.query(obd.commands.ELM_VERSION)
        if r.is_null():
            return None
        return str(r.value).strip() or None

    def _on_vehicle(self, conn: obd.OBD) -> None:
        adapter = self._adapter_name(conn)
        protocol = conn.protocol_name()
        vin = decode_vin(conn.query(obd.commands.VIN)) if conn.supports(obd.commands.VIN) else None
        supported = [m.name for m in self.measurements if conn.supports(obd.commands[m.command])]
        with self._lock:
            self._probe = {
                "device_id": self.cfg.device_id,
                "vehicle": True,
                "adapter": adapter,
                "protocol": protocol,
                # Never published or stored: the VIN identifies the car and its owner
                "vin_hash": hashlib.sha256(vin.encode()).hexdigest()[:16] if vin else None,
                "measurements": {
                    "supported": supported,
                    "unsupported": [m.name for m in self.measurements if m.name not in supported],
                },
                "connected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        log.info("vehicle connected: %s via %s; %d of %d measurements supported",
                 protocol, adapter, len(supported), len(self.measurements))
        self._set_status(adapter=adapter, protocol=protocol, vehicle=True)

    def _set_status(self, **status) -> None:
        payload = {"online": True, **status}
        with self._lock:
            if payload == self._status:
                return
            self._status = payload
            if not status.get("vehicle"):
                self._probe = {**self._probe, "vehicle": False}
        self.pub.set_status(self.topic("status", "online"), payload)

    # ---- reading -----------------------------------------------------------

    def _read_group(self, conn: obd.OBD, group: str) -> bool | None:
        """Read and publish a group; True if anything answered, None if nothing to read."""
        answered = None
        for m in self.measurements:
            if m.group != group:
                continue
            cmd = obd.commands[m.command]
            if not conn.supports(cmd):
                continue
            answered = answered or False
            r = conn.query(cmd)
            if r.is_null() or r.value is None:
                continue
            value = round(float(getattr(r.value, "magnitude", r.value)), 2)
            answered = True
            with self._lock:
                self._latest[m.name] = (value, self.clock())
            self.pub.publish(self.topic("sensors", m.name), {"value": value, "unit": m.unit})
        return answered

    def _publish_raw(self) -> None:
        now = self.clock()
        with self._lock:
            fresh = {n: v for n, (v, t) in self._latest.items() if now - t <= 2.0}
        if all(n in fresh for n in RAW):
            self.pub.publish(self.topic("sensors", "raw"), {"values": [fresh[n] for n in RAW]})

    def _read_dtc(self, conn: obd.OBD) -> None:
        stored = conn.query(obd.commands.GET_DTC)
        if stored.is_null():
            return  # no answer this time: keep what we know
        pending = conn.query(obd.commands.GET_CURRENT_DTC)
        permanent = conn.query(GET_PERMANENT_DTC, force=True)
        status = conn.query(obd.commands.STATUS) if conn.supports(obd.commands.STATUS) else None
        freeze = conn.query(obd.commands.FREEZE_DTC) if conn.supports(obd.commands.FREEZE_DTC) else None

        def codes(r) -> list[tuple[str, str]]:
            return [] if r is None or r.is_null() or not r.value else list(r.value)

        found = codes(stored) + codes(pending) + codes(permanent)
        state = {
            "codes": [c for c, _ in codes(stored)],
            "pending": [c for c, _ in codes(pending)],
            "permanent": [c for c, _ in codes(permanent)],
            "mil": bool(status.value.MIL) if status is not None and not status.is_null() else bool(codes(stored)),
            "descriptions": {c: d for c, d in found if d},
            "freeze_frame": {"dtc": freeze.value[0]} if freeze is not None and not freeze.is_null() and freeze.value else {},
        }
        with self._lock:
            before = self._dtc
            self._dtc, self._dtc_read_at = state, self.clock()

        def key(s):
            return None if s is None else (s["codes"], s["pending"], s["permanent"], s["mil"])

        nothing = (state["codes"], state["pending"], state["permanent"], state["mil"]) == ([], [], [], False)
        if key(state) != key(before) and not (before is None and nothing):
            log.info("trouble codes: %s (pending %s, MIL %s)", state["codes"], state["pending"], state["mil"])
            self.pub.publish(self.topic("alerts", "dtc"), state, qos=1)
