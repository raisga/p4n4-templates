# Turns a message on sensors/<device-id>/<measurement> into a sensor_data point,
# the same schema the MING stack's Node-RED flow writes:
#
#   sensor_data,device=<device-id>,sensor=<measurement> value=23.4,unit="C"
#
# The payload is either a JSON object, whose numeric, string and boolean
# members become fields, or a bare JSON value, which becomes the "value" field.
# Payloads that aren't valid JSON fail json.decode; Telegraf logs the error
# and drops the message (it is still kept in the raw archive).

load("json.star", "json")
load("logging.star", "log")

TAG_KEYS = ("device", "sensor")

def apply(metric):
    topic = metric.tags.get("topic", "")
    parts = topic.split("/")
    # "+" also matches an empty level, e.g. sensors//temperature
    if len(parts) != 3 or not parts[1] or not parts[2]:
        log.warn("ignored " + topic + ": expected sensors/<device-id>/<measurement>")
        return None

    data = json.decode(metric.fields.get("value", ""))
    if type(data) != "dict":
        data = {"value": data}

    point = Metric("sensor_data")
    point.tags["device"] = parts[1]
    point.tags["sensor"] = parts[2]
    for key, val in data.items():
        if key in TAG_KEYS:
            continue
        kind = type(val)
        if kind == "int":
            # Always write numbers as floats so 23 and 23.4 don't conflict in InfluxDB
            point.fields[key] = float(val)
        elif kind in ("float", "string", "bool"):
            point.fields[key] = val

    if not point.fields:
        log.warn("ignored " + topic + ": payload has no usable fields")
        return None

    point.time = metric.time
    return point
