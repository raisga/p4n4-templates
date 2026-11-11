"""MQTT publishing: JSON payloads, and a retained status with a last will."""

from __future__ import annotations

import json
import logging
import threading

import paho.mqtt.client as mqtt

from .config import Config

log = logging.getLogger(__name__)

OFFLINE = {"online": False}


def dumps(payload: dict) -> str:
    return json.dumps(payload, separators=(",", ":"))


class MqttPublisher:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.connected = False
        self._status: tuple[str, dict] | None = None
        self._lock = threading.Lock()
        self.status_topic = f"status/{cfg.device_id}/online"
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"p4n4-obd2-{cfg.device_id}")
        if cfg.mqtt_user:
            self.client.username_pw_set(cfg.mqtt_user, cfg.mqtt_password or None)
        # The broker marks the device offline if obd2 dies without saying goodbye
        self.client.will_set(self.status_topic, dumps(OFFLINE), qos=1, retain=True)
        self.client.reconnect_delay_set(1, 30)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect

    def start(self) -> None:
        self.client.connect_async(self.cfg.mqtt_host, self.cfg.mqtt_port, keepalive=30)
        self.client.loop_start()

    def stop(self) -> None:
        try:
            self.client.publish(self.status_topic, dumps(OFFLINE), qos=1, retain=True).wait_for_publish(2)
        except (RuntimeError, ValueError):
            pass  # no broker to tell: its last will says the same
        self.client.disconnect()
        self.client.loop_stop()

    def publish(self, topic: str, payload: dict, retain: bool = False, qos: int = 0) -> None:
        # QoS 0 readings are dropped while the broker is away; QoS 1 ones are queued
        self.client.publish(topic, dumps(payload), qos=qos, retain=retain)

    def set_status(self, topic: str, payload: dict) -> None:
        with self._lock:
            self._status = (topic, payload)
        self.publish(topic, payload, retain=True, qos=1)

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            log.warning("MQTT %s:%s refused the connection: %s", self.cfg.mqtt_host, self.cfg.mqtt_port, reason_code)
            return
        self.connected = True
        log.info("MQTT connected to %s:%s", self.cfg.mqtt_host, self.cfg.mqtt_port)
        with self._lock:
            status = self._status
        if status:  # the will may have fired while we were away
            self.publish(*status, retain=True, qos=1)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected = False
        if reason_code.is_failure:
            log.warning("MQTT disconnected (%s); reconnecting", reason_code)
