# Turns the obd2 service's status, the retained message on
# status/<device-id>/online, into a vehicle_status point:
#
#   vehicle_status,device=<device-id> online=true,vehicle=true,
#       adapter="ELM327 v1.5",protocol="ISO 15765-4 (CAN 11/500)"
#
# vehicle is true while a car answers; adapter and protocol are "" when they
# aren't known. When the service stops or drops off the broker, the broker
# sends its last will, {"online": false}: online=false, vehicle=false and
# empty strings. The message is retained, so Telegraf gets the current status
# again each time it subscribes, e.g. after a restart.

load("json.star", "json")
load("logging.star", "log")

def apply(metric):
    topic = metric.tags.get("topic", "")
    parts = topic.split("/")
    if len(parts) != 3 or not parts[1]:
        log.warn("ignored " + topic + ": expected status/<device-id>/online")
        return None

    raw = metric.fields.get("value", "")
    # An empty retained message is how a status is deleted from the broker
    if not raw.strip():
        return None
    data = json.decode(raw)
    if type(data) != "dict" or type(data.get("online")) != "bool":
        log.warn("ignored " + topic + ": expected a JSON object with a boolean \"online\"")
        return None

    point = Metric("vehicle_status")
    point.tags["device"] = parts[1]
    point.fields["online"] = data["online"]
    point.fields["vehicle"] = data.get("vehicle") == True
    for key in ("adapter", "protocol"):
        val = data.get(key)
        point.fields[key] = val if type(val) == "string" else ""

    point.time = metric.time
    return point
