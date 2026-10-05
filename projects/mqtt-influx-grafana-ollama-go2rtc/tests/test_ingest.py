"""
Offline checks for the ingest service (iot/ingest): contract rules, the
site's periods and upserts, retention, what the sinks leave out, and the
agent API's numbers, against a hand-made road and the demo simulator's plan.
No Docker.

    python3 tests/test_ingest.py
"""

from __future__ import annotations

import copy
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT / "iot/ingest"), str(ROOT / "iot/scripts")]
import contract  # noqa: E402
import simulate  # noqa: E402
import sinks  # noqa: E402
from store import PeriodError, Store, one_edit  # noqa: E402

failed = False


def check(label: str, ok: bool, detail: object = "") -> None:
    global failed
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f": {detail}"))
    failed |= not ok


TZ = timezone(timedelta(hours=-5))
S = "road-test"
# Saturday 2026-10-03, 19:00 site time
NOW = datetime(2026, 10, 3, 19, 0, tzinfo=TZ).timestamp()
# The hand-made data is from 2026: keep it whatever the date the test runs on
FOREVER = 36500


def read(seq: int, when: str, plate: str | None = "ABC-1234", direction: str = "inbound", **extra) -> dict:
    doc = {
        "schema": contract.READ, "read_id": f"RD-20261003-{seq:06d}", "site_id": S,
        "camera_id": "cam-in-01" if direction == "inbound" else "cam-out-01", "timestamp": when,
        "direction": direction, "vehicle_type": "car", "plate": plate, "synthetic": False, **extra,
    }
    if plate:
        doc.setdefault("plate_confidence", 0.93)
    return doc


print("contract")
ok_read = read(1, "2026-10-03T08:00:00-05:00", speed_kmh=48.5, lane=1, plate_region="PA",
               evidence={"image_uri": "frames/1.jpg", "plate_crop_uri": "plates/1.jpg"})
check("a contract read passes", contract.check_read(ok_read) is None, contract.check_read(ok_read))
check("a vehicle without a plate passes", contract.check_read(read(2, "2026-10-03T08:00:00-05:00", plate=None)) is None)
for label, change in [
    ("bad read id", {"read_id": "RD-1"}),
    ("unknown direction", {"direction": "north"}),
    ("unknown vehicle type", {"vehicle_type": "tank"}),
    ("a plate without confidence", {"plate_confidence": None}),
    ("confidence over 1", {"plate_confidence": 1.5}),
    ("confidence without a plate", {"plate": None}),
    ("a plate with a slash", {"plate": "AB/123"}),
    ("a plate over 16 characters", {"plate": "ABCDEFGHIJ-1234567"}),
    ("a plate ending in a dash", {"plate": "ABC-"}),
    ("a speed of 900 km/h", {"speed_kmh": 900}),
    ("lane 0", {"lane": 0}),
    ("evidence with an unknown key", {"evidence": {"image": "x"}}),
    ("missing synthetic", {"synthetic": None}),
    ("a camera id with a slash", {"camera_id": "cam/1"}),
]:
    bad = {**ok_read, **change}
    check(f"rejects {label}", contract.check_read(bad) is not None)
errors = [contract.check_read({**ok_read, "plate": p}) for p in ("SECRET/1", "TOPSECRET-12345678901")]
check("a refusal never repeats the plate", all(e and "SECRET" not in e for e in errors), errors)
check("plates in other scripts pass", contract.check_read({**ok_read, "plate": "ÄÖ 123"}) is None)
check("plate keys ignore case, spaces, dashes and dots", contract.plate_key("abc-12.34 ") == "ABC1234")
check("naive times are site time", contract.parse_time("2026-10-03T18:00:00", TZ).utcoffset() == timedelta(hours=-5))
check("site_tz takes offsets", contract.site_tz("-05:00").utcoffset(None) == timedelta(hours=-5))
check("misreads are one edit away", one_edit("ABC1234", "A8C1234") and one_edit("ABC1234", "ABC234")
      and one_edit("ABC1234", "BAC1234") and not one_edit("ABC1234", "XYZ1234"))

print("store")
db = Store(":memory:", TZ, retention_days=FOREVER)
check("new read is created", db.put_read(ok_read) == "created")
check("same read again is unchanged", db.put_read(copy.deepcopy(ok_read)) == "unchanged")
check("a corrected read is updated", db.put_read({**ok_read, "plate": "ABC-1284"}) == "updated")

lo, hi, p = db.period("today", now=NOW)
check("today starts at site midnight", p["from"] == "2026-10-03T00:00:00-05:00", p)
lo, hi, p = db.period("last_week", now=NOW)
check("last_week is Monday to Monday", (p["from"], p["to"]) == ("2026-09-21T00:00:00-05:00", "2026-09-28T00:00:00-05:00"), p)
lo, hi, p = db.period("martes", now=NOW)
check("a weekday is its latest occurrence", p["from"] == "2026-09-29T00:00:00-05:00", p)
lo, hi, p = db.period(None, "2026-09-01", "2026-09-02", now=NOW)
check("a 'to' date includes the whole day", p["to"] == "2026-09-03T00:00:00-05:00", p)
try:
    db.period("fortnight", now=NOW)
    check("unknown periods are refused", False)
except PeriodError:
    check("unknown periods are refused", True)

print("retention")
db = Store(":memory:", TZ, retention_days=30)
now = datetime.now(TZ)
fresh = read(1, (now - timedelta(days=2)).isoformat())
stale = read(2, (now - timedelta(days=29, hours=23)).isoformat())
db.put_read(fresh)
db.put_read(stale)
try:
    db.put_read(read(3, (now - timedelta(days=31)).isoformat()))
    check("reads older than the retention period are refused", False)
except ValueError as e:
    check("reads older than the retention period are refused", "retention" in str(e), e)
check("purge keeps reads inside the period", db.purge() == 0)
check("purge deletes them once they age out", db.purge(now=(now + timedelta(days=1)).timestamp()) == 1)
check("and keeps the newer one", db.db.execute("SELECT COUNT(*) FROM reads").fetchone()[0] == 1)

# A hand-made Saturday: a morning rush in, an evening rush out, a night with glare
db = Store(":memory:", TZ, retention_days=FOREVER, speed_limit=50)
seq = 0


def put(when: str, plate: str | None, direction: str = "inbound", **extra) -> None:
    global seq
    seq += 1
    db.put_read(read(seq, f"2026-10-03T{when}:00-05:00", plate, direction, **extra))


for i in range(6):
    put(f"08:{i:02d}", f"COM-{i:04d}", speed_kmh=40.0 + i)
for i in range(6):
    put(f"17:{i:02d}", f"COM-{i:04d}", "outbound", speed_kmh=45.0 + 2 * i)
put("12:00", "KDL-4821", vehicle_type="van")
put("12:30", "KDL-4821", "outbound", vehicle_type="van")
put("13:00", "KDL-4B21", vehicle_type="van")  # a misread of the van
put("12:10", None, vehicle_type="motorcycle")

lo, hi, _ = db.period("today", now=NOW)
s = db.summary(S, lo, hi)
check("vehicles count reads with and without a plate", s["vehicles"] == 16, s["vehicles"])
check("vehicles per direction", s["by_direction"] == {"inbound": 9, "outbound": 7}, s["by_direction"])
check("read rate is plates / vehicles", s["plates_read"] == 15 and s["read_rate"] == round(15 / 16, 3), s)
check("distinct and repeat plates", s["distinct_plates"] == 8 and s["repeat_plates"] == 7, s)
check("peak hour is site-local", s["peak_hour"]["hour"] in ("08:00 - 09:00", "17:00 - 18:00"), s["peak_hour"])
check("speed: average, 85th percentile and over the limit",
      s["speed"]["readings"] == 12 and s["speed"]["p85_kmh"] == 53.0 and s["speed"]["over_limit"] == 3, s["speed"])
check("each camera's read rate", s["by_camera"]["cam-in-01"] == {"vehicles": 9, "read_rate": round(8 / 9, 3)}, s["by_camera"])
check("a one-day period has no busiest day", s["busiest_day"] is None, s["busiest_day"])

t = db.traffic(S, lo, hi, direction="outbound")
hours = {h["hour"]: h for h in t["by_hour"]}
check("traffic by hour, for one direction", hours["17:00"]["vehicles"] == 6 and hours["08:00"]["vehicles"] == 0, hours["17:00"])
check("traffic by day", t["by_day"] == [{"day": "2026-10-03 saturday", "vehicles": 7, "outbound": 7}], t["by_day"])
try:
    db.traffic(S, lo, hi, direction="north")
    check("an unknown direction is refused, not an empty answer", False)
except PeriodError:
    check("an unknown direction is refused, not an empty answer", True)

pl = db.plate(S, "kdl 4821", lo, hi)
check("a plate lookup ignores case and spaces", pl["passages_total"] == 2 and pl["plate"] == "KDL4821", pl)
check("passages are newest first, with direction", [x["direction"] for x in pl["passages"]] == ["outbound", "inbound"], pl["passages"])
check("similar plates catch a misread", pl["similar_plates"] == [{"plate": "KDL4B21", "passages": 1}], pl["similar_plates"])
check("a plate never seen has no passages", db.plate(S, "ZZZ-9999", lo, hi)["passages_total"] == 0)
try:
    db.plate(S, "--", lo, hi)
    check("a plate with no letters or digits is refused", False)
except PeriodError:
    check("a plate with no letters or digits is refused", True)

f = db.frequent(S, lo, hi, limit=3)
check("frequent plates: only repeats, most first", f["repeat_plates"] == 7 and f["plates"][0]["passages"] == 2, f)
check("frequent plates: days seen and directions", f["plates"][0]["by_direction"] == {"inbound": 1, "outbound": 1}, f["plates"][0])

print("checks")
db = Store(":memory:", TZ, retention_days=FOREVER)
seq = 0
for day in range(6):
    for i in range(60):
        night = i % 3 == 0  # a third of the traffic after dark
        hour = 22 if night else 12
        when = (datetime(2026, 9, 27, hour, i % 60, tzinfo=TZ) + timedelta(days=day)).isoformat()
        seq += 1
        # cam-out-01 reads 1 in 3 plates at night; cam-in-01 reads all
        reads_it = not night or seq % 3 == 0
        db.put_read({**read(seq, when, f"P{seq:05d}" if reads_it else None, "outbound"), "read_id": f"RD-20260927-{seq:06d}"})
        seq += 1
        db.put_read({**read(seq, when, f"Q{seq:05d}", "inbound"), "read_id": f"RD-20260927-{seq:06d}"})
c = db.checks(S, now=datetime(2026, 10, 3, 12, 30, tzinfo=TZ).timestamp())
fired = {(x["check"], x["evidence"]["camera_id"]) for x in c["checks"]}
check("C2 fires for the camera that can't read at night", ("C2 night", "cam-out-01") in fired, c["checks"])
check("C1 fires when a camera reads too few plates overall", ("C1 read rate", "cam-out-01") in fired, fired)
check("C3 fires for cameras silent at a busy hour", ("C3 silent", "cam-in-01") in fired, fired)
check("the camera that reads well passes C1 and C2", not {("C1 read rate", "cam-in-01"), ("C2 night", "cam-in-01")} & fired, fired)
quiet = db.checks(S, now=datetime(2026, 10, 3, 3, 30, tzinfo=TZ).timestamp())
check("C3 stays quiet at an hour that is usually empty", not any(x["check"] == "C3 silent" for x in quiet["checks"]), quiet["checks"])

print("SITE_LANG")
es = Store(":memory:", TZ, "es", retention_days=FOREVER)
es.put_read(ok_read)
lo, hi, _ = es.period("today", now=NOW)
check("es: weekdays in Spanish", es.traffic(S, lo, hi)["by_day"][0]["day"] == "2026-10-03 sábado")
check("es: check texts in Spanish", "Ningún" in es.checks(S, now=NOW)["checks"][0]["issue"])
check("weekdays work in either language", es.period("martes", now=NOW)[:2] == es.period("tuesday", now=NOW)[:2])
try:
    Store(":memory:", TZ, "fr")
    check("an unknown language is refused", False)
except ValueError:
    check("an unknown language is refused", True)

print("sinks: no plates in InfluxDB or MQTT")
lines = sinks.read_lines(ok_read, TZ)
check("a read line has tags, count and speed",
      lines[0].startswith("alpr_read,camera_id=cam-in-01,direction=inbound,plate_read=yes,site_id=road-test,vehicle_type=car count=1i,speed_kmh=48.5,confidence=0.93"), lines)
check("the InfluxDB line has no plate", "ABC" not in lines[0], lines)
message = sinks.read_message(ok_read)
check("the MQTT message has no plate or evidence", "plate" not in message and "evidence" not in message
      and "plate_region" not in message and message["plate_read"] is True, message)
check("the MQTT topic names the direction, not the plate", sinks.topic(S, "read", "inbound") == "traffic/road-test/read/inbound")
sinks.MQTT_PLATES = True
check("MQTT_PLATES=true keeps the plate", sinks.read_message(ok_read)["plate"] == "ABC-1234")
sinks.MQTT_PLATES = False
hb = {"schema": contract.HEARTBEAT, "site_id": S, "timestamp": "2026-10-03T18:00:00-05:00", "status": "healthy",
      "device": {"hostname": "jetson 1"}, "metrics": {"temperature_gpu_c": 55}, "pipeline": {"vehicles_last_minute": 4}}
lines = sinks.heartbeat_lines(hb, TZ)
check("heartbeat numbers become sensor_data",
      any(line.startswith("sensor_data,device=jetson\\ 1,sensor=temperature_gpu_c value=55.0") for line in lines), lines)

print("demo simulator")
plan = simulate.plan_day(date(2026, 9, 29))
again = simulate.plan_day(date(2026, 9, 29))
check("a day's plan is the same on every run", plan["reads"] == again["reads"])
errors = [contract.check_read(d) for _, d in plan["reads"]]
check("every simulated read follows the contract", not any(errors), [e for e in errors if e][:3])
ids = [d["read_id"] for _, d in plan["reads"]]
check("read ids are unique and in time order", len(set(ids)) == len(ids) and ids == sorted(ids))
check("weekday traffic is heavier than the weekend's",
      plan["vehicles"] > simulate.plan_day(date(2026, 10, 4))["vehicles"] * 1.2)

sys.exit(1 if failed else 0)
