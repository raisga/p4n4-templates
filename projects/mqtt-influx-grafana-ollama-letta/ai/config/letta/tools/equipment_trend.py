def equipment_trend(device: str, days: int = 7) -> str:
    """Daily averages of one machine's sensors over the last days, oldest first, with the change over the period. Use it to see whether vibration or bearing temperature is creeping up, which is how bearing wear shows.

    Args:
        device: The machine id, e.g. pump-2.
        days: How many days back to look (1 to 30, default 7).

    Returns:
        str: One line per sensor with its daily means and the change.
    """
    # Runs inside Letta's tool sandbox: self-contained, standard library only.
    import csv
    import io
    import json
    import os
    import re
    import urllib.request

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", device or ""):
        return "Unknown machine id: %r" % device
    days = max(1, min(int(days or 7), 30))
    units = {"vibration": "mm/s", "bearing_temp": "°C", "current": "A"}
    flux = """
from(bucket: "%s")
  |> range(start: -%dd)
  |> filter(fn: (r) => r._measurement == "sensor_data" and r._field == "value" and r.device == "%s")
  |> group(columns: ["sensor"])
  |> aggregateWindow(every: 1d, fn: mean, createEmpty: false)
  |> group()
  |> keep(columns: ["_time", "sensor", "_value"])
""" % (os.environ["INFLUXDB_BUCKET"], days, device)
    request = urllib.request.Request(
        os.environ["INFLUXDB_URL"] + "/api/v2/query?org=" + os.environ["INFLUXDB_ORG"],
        data=json.dumps({"query": flux, "type": "flux", "dialect": {"header": True, "annotations": []}}).encode(),
        headers={"Authorization": "Token " + os.environ["INFLUXDB_TOKEN"],
                 "Content-Type": "application/json", "Accept": "application/csv"})
    with urllib.request.urlopen(request, timeout=20) as response:
        rows = list(csv.DictReader(io.StringIO(response.read().decode())))
    series = {}
    for row in sorted(rows, key=lambda r: r.get("_time", "")):
        if row.get("sensor") and row.get("_value"):
            series.setdefault(row["sensor"], []).append(float(row["_value"]))
    if not series:
        return "No readings for %s in the last %d days." % (device, days)
    lines = ["%s, daily means over the last %d days (oldest first):" % (device, days)]
    for sensor, values in sorted(series.items()):
        change = values[-1] - values[0]
        percent = (" (%+.0f%%)" % (100 * change / values[0])) if values[0] else ""
        lines.append("%s (%s): %s; change %+.1f%s" % (
            sensor, units.get(sensor, ""), ", ".join("%.1f" % v for v in values), change, percent))
    return "\n".join(lines)
