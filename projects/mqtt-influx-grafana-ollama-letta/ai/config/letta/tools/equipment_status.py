def equipment_status() -> str:
    """Latest readings of every machine: vibration, bearing temperature and motor current, with the last 24 hours' mean and max. Use it first for questions about the whole site or "is anything wrong?".

    Returns:
        str: One line per machine and sensor.
    """
    # Runs inside Letta's tool sandbox: self-contained, standard library only.
    # INFLUXDB_* come from the agent's secrets (set by provision.py).
    import csv
    import io
    import json
    import os
    import urllib.request

    units = {"vibration": "mm/s", "bearing_temp": "°C", "current": "A"}
    flux = """
data = from(bucket: "%s")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "sensor_data" and r._field == "value")
  |> group(columns: ["device", "sensor"])
union(tables: [
  data |> last() |> set(key: "stat", value: "last"),
  data |> mean() |> set(key: "stat", value: "mean"),
  data |> max() |> set(key: "stat", value: "max"),
])
  |> group()
  |> keep(columns: ["device", "sensor", "stat", "_value"])
""" % os.environ["INFLUXDB_BUCKET"]
    request = urllib.request.Request(
        os.environ["INFLUXDB_URL"] + "/api/v2/query?org=" + os.environ["INFLUXDB_ORG"],
        data=json.dumps({"query": flux, "type": "flux", "dialect": {"header": True, "annotations": []}}).encode(),
        headers={"Authorization": "Token " + os.environ["INFLUXDB_TOKEN"],
                 "Content-Type": "application/json", "Accept": "application/csv"})
    with urllib.request.urlopen(request, timeout=20) as response:
        rows = list(csv.DictReader(io.StringIO(response.read().decode())))
    stats = {}
    for row in rows:
        if row.get("device") and row.get("_value"):
            stats.setdefault((row["device"], row["sensor"]), {})[row["stat"]] = float(row["_value"])
    if not stats:
        return "No readings in the last 24 hours."
    lines = []
    for (device, sensor), s in sorted(stats.items()):
        unit = units.get(sensor, "")
        lines.append("%s %s: now %.1f %s (24 h mean %.1f, max %.1f)" % (
            device, sensor, s.get("last", float("nan")), unit, s.get("mean", float("nan")), s.get("max", float("nan"))))
    return "\n".join(lines)
