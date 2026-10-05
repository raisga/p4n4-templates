# Turns a command on actuators/<zone>/<actuator>/set into an actuator_command
# point, so Grafana can show when each actuator switched and why:
#
#   actuator_command,device=<zone>,actuator=<actuator> state=1,mode="auto",reason="..."
#
# The payload is the JSON Node-RED publishes:
#   {"state": "on", "mode": "auto", "reason": "temperature 29.1 °C ≥ 28 °C", "at": "..."}
# state is stored as 1 (on) or 0 (off). The tag is "device", like sensor_data,
# so one dashboard variable filters both. Retained commands are stored again
# each time Telegraf reconnects; that repeats a point, not a decision.

load("json.star", "json")
load("logging.star", "log")

STATES = {"on": 1.0, "off": 0.0}

def apply(metric):
    topic = metric.tags.get("topic", "")
    parts = topic.split("/")
    if len(parts) != 4 or not parts[1] or not parts[2]:
        log.warn("ignored " + topic + ": expected actuators/<zone>/<actuator>/set")
        return None

    data = json.decode(metric.fields.get("value", ""))
    if type(data) != "dict" or data.get("state") not in STATES:
        log.warn("ignored " + topic + ": payload has no state on or off")
        return None

    point = Metric("actuator_command")
    point.tags["device"] = parts[1]
    point.tags["actuator"] = parts[2]
    point.fields["state"] = STATES[data["state"]]
    for key in ("mode", "reason"):
        if type(data.get(key)) == "string":
            point.fields[key] = data[key]
    point.time = metric.time
    return point
