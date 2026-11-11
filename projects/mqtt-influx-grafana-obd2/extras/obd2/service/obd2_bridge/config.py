"""Settings, from the environment (the extras layer's .env)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

MEASUREMENT_NAMES = (
    "rpm", "speed", "maf", "engine_load", "coolant_temp", "stft_b1", "ltft_b1",
    "intake_temp", "throttle_pos", "fuel_level", "battery_v",
)
# One topic level: no /, + or # (specs §8.6), nor whitespace
_DEVICE_ID = re.compile(r"^[^/+#\s]+$")


class ConfigError(ValueError):
    pass


def _float(env: dict, key: str, default: float) -> float:
    raw = env.get(key) or str(default)
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{key}={raw!r} isn't a number") from None
    if value <= 0:
        raise ConfigError(f"{key} must be above 0")
    return value


@dataclass(frozen=True)
class Config:
    device: str
    device_id: str = "car"
    baudrate: int | None = None
    protocol: str | None = None
    fast_hz: float = 5.0
    slow_hz: float = 1.0
    voltage_interval: float = 5.0
    dtc_interval: float = 30.0
    measurements: tuple[str, ...] = MEASUREMENT_NAMES
    raw: bool = True
    mqtt_host: str = "p4n4-mqtt"
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_password: str = ""
    http_port: int = 8090
    log_level: str = "INFO"

    @classmethod
    def from_env(cls, env: dict | None = None) -> Config:
        env = dict(os.environ if env is None else env)
        device = env.get("OBD_DEVICE", "").strip()
        if not device:
            raise ConfigError("OBD_DEVICE is empty: a serial device, or socket://host:port")
        device_id = env.get("OBD_DEVICE_ID") or "car"
        if not _DEVICE_ID.match(device_id):
            raise ConfigError(f"OBD_DEVICE_ID={device_id!r} must be one topic level (no / + # or spaces)")

        baud = env.get("OBD_BAUDRATE") or None
        try:
            baudrate = int(baud) if baud else None
        except ValueError:
            raise ConfigError(f"OBD_BAUDRATE={baud!r} isn't a number") from None
        if baudrate is None and device.startswith("socket://"):
            baudrate = 38400  # no baud on TCP; skips python-OBD's probe

        protocol = (env.get("OBD_PROTOCOL") or "").upper() or None
        if protocol is not None and not re.fullmatch(r"[1-9A-C]", protocol):
            raise ConfigError(f"OBD_PROTOCOL={protocol!r}: 1-9 or A-C, or empty to detect it")

        names = tuple(n.strip() for n in (env.get("OBD_PIDS") or "").split(",") if n.strip())
        unknown = sorted(set(names) - set(MEASUREMENT_NAMES))
        if unknown:
            raise ConfigError(f"OBD_PIDS: unknown {', '.join(unknown)} (known: {', '.join(MEASUREMENT_NAMES)})")

        return cls(
            device=device,
            device_id=device_id,
            baudrate=baudrate,
            protocol=protocol,
            fast_hz=_float(env, "OBD_POLL_FAST_HZ", 5.0),
            slow_hz=_float(env, "OBD_POLL_SLOW_HZ", 1.0),
            voltage_interval=_float(env, "OBD_VOLTAGE_INTERVAL", 5.0),
            dtc_interval=_float(env, "OBD_DTC_INTERVAL", 30.0),
            measurements=names or MEASUREMENT_NAMES,
            raw=(env.get("OBD_RAW") or "true").lower() not in ("0", "false", "no", "off"),
            mqtt_host=env.get("MQTT_HOST") or "p4n4-mqtt",
            mqtt_port=int(env.get("MQTT_PORT") or 1883),
            mqtt_user=env.get("MQTT_USER") or "",
            mqtt_password=env.get("MQTT_PASSWORD") or "",
            log_level=(env.get("OBD2_LOG_LEVEL") or "INFO").upper(),
        )
