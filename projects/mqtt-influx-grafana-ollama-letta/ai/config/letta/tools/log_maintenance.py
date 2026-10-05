def log_maintenance(device: str, work: str, technician: str, agent_state: "AgentState") -> str:
    """Records maintenance work in the maintenance log: shown on the Grafana dashboard, and kept in your archival memory so you can recall it later. Use it whenever someone reports work done on a machine (repairs, replaced parts, lubrication, inspections).

    Args:
        device: The machine id, e.g. pump-2.
        work: What was done, in one or two sentences, e.g. "Replaced the drive-end bearing; vibration back to 2.1 mm/s".
        technician: Who did it; empty if not given.

    Returns:
        str: Confirmation, with the date recorded.
    """
    # Runs inside Letta's tool sandbox: self-contained, standard library only.
    # Writes a maintenance_event point to InfluxDB (Grafana's maintenance log)
    # and the same note to this agent's archival memory, tagged with the
    # machine, through Letta's API (LETTA_URL, LETTA_PASSWORD secrets).
    import datetime
    import json
    import os
    import re
    import time
    import urllib.request

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", device or ""):
        return "Unknown machine id: %r" % device
    work = (work or "").strip()
    technician = (technician or "").strip()
    if not work:
        return "Nothing recorded: describe the work that was done."

    # Line protocol string fields: escape backslashes and quotes. (No nested
    # functions: Letta reads every function in a tool's source as a tool.)
    work_field, technician_field = (
        '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"' for v in (work, technician))
    line = "maintenance_event,device=%s,source=assistant work=%s,technician=%s %d" % (
        device, work_field, technician_field, int(time.time() * 1000))
    request = urllib.request.Request(
        "%s/api/v2/write?org=%s&bucket=%s&precision=ms" % (
            os.environ["INFLUXDB_URL"], os.environ["INFLUXDB_ORG"], os.environ["INFLUXDB_BUCKET"]),
        data=line.encode(), headers={"Authorization": "Token " + os.environ["INFLUXDB_TOKEN"]})
    urllib.request.urlopen(request, timeout=20).close()

    today = datetime.date.today().isoformat()
    note = "%s %s: %s%s" % (today, device, work, (" Technician: %s." % technician) if technician else "")
    request = urllib.request.Request(
        "%s/v1/agents/%s/archival-memory" % (os.environ["LETTA_URL"], agent_state.id),
        data=json.dumps({"text": note, "tags": [device, "maintenance"]}).encode(),
        headers={"Authorization": "Bearer " + os.environ["LETTA_PASSWORD"], "Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=60).close()
    return "Logged for %s on %s: %s" % (device, today, work)
