# obd2

The **obd2** extra from [p4n4-extras](https://github.com/raisga/p4n4-extras) (`obd2/` at commit `d684d4d`), copied in unchanged so the template builds on its own. Its README there covers it in full: the topics, the settings, a USB adapter, the simulator and its tests.

| | |
|---|---|
| `service/` | The bridge: reads the car through an ELM327 adapter, read-only, and publishes over MQTT. Image `p4n4-obd2:0.1.0`, built locally on the first start |
| `simulator/` | A simulated car and ELM327 adapter on TCP 35000, playing `scenarios/<name>.yaml`. Image `p4n4-obd2-sim:0.1.0`, built locally |
| `LICENSE` | GPL-2.0-only |

**Licence.** This directory is **GPL-2.0-only** (copyleft), because the bridge imports [python-OBD](https://github.com/brendan-w/python-OBD), which is. The rest of the template is under the repository's licence. Whoever distributes the `p4n4-obd2` image, or a project with this directory, distributes GPL-2.0 code: its source is all here, and the licence goes with it.

## How the template runs it

`../docker-compose.yml` defines both services itself (no `include:`). The changes from the extra's own compose file:
- `obd2` always runs, and `obd2-sim` runs with the `demo` profile, instead of profiles `obd2` and `obd2-sim`.
- `extra_hosts: host.docker.internal:host-gateway`, for the phone app's OBD relay: `OBD_DEVICE=socket://host.docker.internal:35100`.
- `OBD_RAW` defaults to `false`. `sensors/<id>/raw` is a feature vector for an edge model this template doesn't have, and Telegraf drops it anyway.
- The simulator runs with `--quiet`: it doesn't log each of the 20 or so requests a second.

The settings are in `../.env.example`. To add a scenario, put it in `simulator/scenarios/` and set `OBD_SIM_SCENARIO` to its name: the directory is mounted, so there's nothing to rebuild.

## In short

- **Topics** (`<id>` is `OBD_DEVICE_ID`, default `car`): `sensors/<id>/<measurement>` with `{"value", "unit"}`, `alerts/<id>/dtc` and `status/<id>/online`. The [template's README](../../README.md#mqtt-topics) lists them with their rates.
- **Trouble codes** go out when obd2 starts and finds some, then whenever the codes or the check-engine light (MIL) change, clearing included. A car with none at the start sends nothing until that changes.
- **Read-only.** obd2 sends diagnostic requests only (modes 01, 03, 06, 07, 09 and 0A) and the AT commands that set the adapter up. `service/obd2_bridge/guard.py` refuses anything else before it reaches the car, clearing codes (mode 04) included, and `/health` counts the refusals (`blocked_writes`).
- **The VIN is never published.** `/api/v1/probe` shows a hash of it, to tell cars apart.
- **API** on `127.0.0.1:8090` (`OBD2_BIND`, `OBD2_PORT`): `/health`, `/api/v1/probe` and `/api/v1/dtc`, all read-only.
- **Simulator.** `warmup` (the default) warms the engine up, drives in a loop and sets P0171 after two minutes. `OBD_SIM_DROP_EVERY` closes the connection every N seconds, like a pulled adapter.

To update it, copy `service/`, `simulator/` and `LICENSE` from p4n4-extras again, then run `../../tests/smoke.sh`.
