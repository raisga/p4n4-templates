"""python -m obd2_bridge: run the bridge, its MQTT client and its HTTP API."""

from __future__ import annotations

import logging
import signal
import sys
import threading

import obd

from . import __version__, guard
from .api import make_server
from .bridge import Bridge
from .config import Config, ConfigError
from .mqtt import MqttPublisher

log = logging.getLogger("obd2")


def main() -> int:
    try:
        cfg = Config.from_env()
    except ConfigError as e:
        print(f"obd2: {e}", file=sys.stderr)
        return 2

    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # python-OBD logs every query at INFO; keep it to warnings unless debugging
    obd.logger.removeHandler(obd.console_handler)
    obd.logger.setLevel(logging.DEBUG if cfg.log_level == "DEBUG" else logging.WARNING)

    guard.install()
    log.info("obd2 %s: %s → mqtt %s:%s as %s", __version__, cfg.device, cfg.mqtt_host, cfg.mqtt_port, cfg.device_id)

    publisher = MqttPublisher(cfg)
    publisher.start()
    bridge = Bridge(cfg, publisher)
    stop = threading.Event()
    loop = threading.Thread(target=bridge.run, args=(stop,), name="bridge", daemon=True)
    loop.start()
    server = make_server(bridge, loop, publisher, port=cfg.http_port)

    def shutdown(signum, frame):
        log.info("stopping")
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    server.serve_forever()
    loop.join(timeout=5)
    publisher.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
