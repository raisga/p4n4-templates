# Turns the obd2 service's trouble codes, a message on alerts/<device-id>/dtc,
# into a dtc point:
#
#   dtc,device=<device-id> codes="P0171",pending="P0300",permanent="",count=2,
#       mil=true,descriptions="P0171: System Too Lean (Bank 1); P0300: ...",
#       freeze_frame="P0171"
#
# The service sends one when the codes or the MIL change (and at startup if
# any are set), so the latest point is the car's current state: codes="" and
# count=0 once they're cleared.
#
#   codes, pending, permanent  the stored, pending and permanent codes, comma-joined
#   count                      distinct codes across the three lists (a float, like
#                              every number these parsers write)
#   mil                        the check-engine light
#   descriptions               "<code>: <description>" for each code, "; "-joined
#   freeze_frame               the code that stored the freeze frame, or ""

load("json.star", "json")
load("logging.star", "log")

LISTS = ("codes", "pending", "permanent")

def strings(val):
    if type(val) != "list":
        return []
    return [v for v in val if type(v) == "string" and v]

def apply(metric):
    topic = metric.tags.get("topic", "")
    parts = topic.split("/")
    if len(parts) != 3 or not parts[1]:
        log.warn("ignored " + topic + ": expected alerts/<device-id>/dtc")
        return None

    raw = metric.fields.get("value", "")
    if not raw.strip():
        return None
    data = json.decode(raw)
    if type(data) != "dict":
        log.warn("ignored " + topic + ": payload is not a JSON object")
        return None

    point = Metric("dtc")
    point.tags["device"] = parts[1]
    seen = []
    for key in LISTS:
        codes = strings(data.get(key))
        point.fields[key] = ",".join(codes)
        for code in codes:
            if code not in seen:
                seen.append(code)
    point.fields["count"] = float(len(seen))
    point.fields["mil"] = data.get("mil") == True

    names = data.get("descriptions")
    if type(names) != "dict":
        names = {}
    described = [c for c in seen if type(names.get(c)) == "string" and names[c]]
    others = sorted([c for c in names if c not in seen and type(names[c]) == "string" and names[c]])
    point.fields["descriptions"] = "; ".join([c + ": " + names[c] for c in described + others])

    frame = data.get("freeze_frame")
    code = frame.get("dtc") if type(frame) == "dict" else None
    point.fields["freeze_frame"] = code if type(code) == "string" else ""

    point.time = metric.time
    return point
