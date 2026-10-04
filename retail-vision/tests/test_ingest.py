"""
Offline checks for the ingest service (iot/ingest): contract rules, the
store's periods and upserts, and the agent API's numbers, against a
hand-made store and the demo simulator's plan. No Docker.

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
from store import PeriodError, Store  # noqa: E402

failed = False


def check(label: str, ok: bool, detail: object = "") -> None:
    global failed
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f": {detail}"))
    failed |= not ok


TZ = timezone(timedelta(hours=-5))
S = "boutique-test"
# Saturday 2026-10-03, 19:00 store time
NOW = datetime(2026, 10, 3, 19, 0, tzinfo=TZ).timestamp()


def event(seq: int, kind: str, when: str, track: str = "P001", **extra) -> dict:
    return {
        "schema": contract.EVENT, "store_id": S, "camera_id": "cam-1", "event_id": f"EVT-20261003-{seq:06d}",
        "type": kind, "ts_start": when, "ts_end": when, "track_id": track, "role": "customer", "synthetic": False,
        **extra,
    }


def alert(seq: int, kind: str, when: str, **extra) -> dict:
    return {
        "schema": contract.ALERT, "alert_id": f"ALT-20261003-{seq:06d}", "store_id": S, "camera_id": "cam-1",
        "alert_type": kind, "severity": "medium", "timestamp": when, "track_id": "P001",
        "title": "Cliente interesado", "description": "Tomó un polo blanco.", "recommended_action": "Atender.",
        "review_required": True, "synthetic": False, **extra,
    }


print("contract")
ok_event = event(1, "customer_entry", "2026-10-03T18:00:00-05:00")
check("a contract event passes", contract.check_event(ok_event) is None, contract.check_event(ok_event))
for label, change in [
    ("bad event id", {"event_id": "EVT-1"}),
    ("unknown type", {"type": "theft"}),
    ("missing synthetic", {"synthetic": None}),
    ("ts_end before ts_start", {"ts_end": "2026-10-03T17:00:00-05:00"}),
    ("track id with a slash", {"track_id": "P/1"}),
    ("confidence over 1", {"attributes": {"confidence": 1.5}}),
]:
    bad = {**ok_event, **change}
    check(f"rejects {label}", contract.check_event(bad) is not None)
check("Jetson-only event types pass", contract.check_event({**ok_event, "type": "garment_tried_on"}) is None)
ok_alert = alert(1, "high_value_opportunity", "2026-10-03T18:00:00-05:00")
check("a contract alert passes", contract.check_alert(ok_alert) is None, contract.check_alert(ok_alert))
for word in ("robo", "Ladrón", "sospechoso", "theft"):
    check(f"rejects the word {word!r}", contract.check_alert({**ok_alert, "description": f"Posible {word} en estante"}) is not None)
check("'probador' and 'probó' are not 'robo'", contract.check_alert({**ok_alert, "description": "Se probó dos polos en el probador"}) is None)
check("naive times are store time", contract.parse_time("2026-10-03T18:00:00", TZ).utcoffset() == timedelta(hours=-5))
check("store_tz takes offsets", contract.store_tz("-05:00").utcoffset(None) == timedelta(hours=-5))

print("store")
db = Store(":memory:", TZ)
check("new event is created", db.put_event(ok_event) == "created")
check("same event again is unchanged", db.put_event(copy.deepcopy(ok_event)) == "unchanged")
check("an event with the model's attributes later is updated",
      db.put_event({**ok_event, "attributes": {"garment_type": "polo", "color": "blanco"}}) == "updated")

lo, hi, p = db.period("today", now=NOW)
check("today starts at store midnight", p["from"] == "2026-10-03T00:00:00-05:00", p)
lo, hi, p = db.period("last_week", now=NOW)
check("last_week is Monday to Monday", (p["from"], p["to"]) == ("2026-09-21T00:00:00-05:00", "2026-09-28T00:00:00-05:00"), p)
lo, hi, p = db.period("martes", now=NOW)
check("a weekday is its latest occurrence", p["from"] == "2026-09-29T00:00:00-05:00", p)
lo, hi, p = db.period("sábado", now=NOW)
check("today's weekday is today", p["from"] == "2026-10-03T00:00:00-05:00", p)
lo, hi, p = db.period(None, "2026-09-01", "2026-09-02", now=NOW)
check("a 'to' date includes the whole day", p["to"] == "2026-09-03T00:00:00-05:00", p)
try:
    db.period("fortnight", now=NOW)
    check("unknown periods are refused", False)
except PeriodError:
    check("unknown periods are refused", True)

# A hand-made afternoon: 3 visitors, a polo shelf busy and selling, a dress shelf busy and not
db = Store(":memory:", TZ)
polo = {"shelf_id": "e-polo", "brand": "RL", "category": "Polos", "price_tier": "alto", "avg_price": 120.0}
dress = {"shelf_id": "e-dress", "brand": "Zara", "category": "Vestidos", "price_tier": "medio", "avg_price": 70.0}
suit = {"shelf_id": "e-suit", "brand": "HB", "category": "Trajes", "price_tier": "alto", "avg_price": 450.0}
seq = 0


def put(kind: str, when: str, track: str, **extra) -> None:
    global seq
    seq += 1
    db.put_event(event(seq, kind, f"2026-10-03T{when}:00-05:00", track, **extra))


for hour, track in [("14:00", "P001"), ("18:10", "P002"), ("18:40", "P003")]:
    put("customer_entry", hour, track)
    h, m = hour.split(":")
    put("customer_exit", f"{h}:{int(m) + 10:02d}", track)
for track in ("P001", "P002", "P003"):
    put("garment_touched", "18:15", track, zone_id="e-polo", planogram=polo, attributes={"garment_type": "polo", "color": "Blanco"})
    put("garment_taken", "18:16", track, zone_id="e-polo", planogram=polo, attributes={"garment_type": "polo", "color": "blanco"})
    put("garment_touched", "18:20", track, zone_id="e-dress", planogram=dress, attributes={"garment_type": "vestido", "color": "rojo"})
    put("garment_taken", "18:21", track, zone_id="e-dress", planogram=dress, attributes={"garment_type": "vestido", "color": "rojo"})
put("garment_touched", "18:30", "P003", zone_id="e-suit", planogram=suit, attributes={"garment_type": "traje", "color": "negro"})
db.put_event({**event(99, "customer_entry", "2026-10-03T18:00:00-05:00", "S01"), "role": "staff"})
for i, amount in enumerate((110.0, 130.0, 460.0)):
    shelf, garment, color = ("e-suit", "traje", "negro") if amount > 400 else ("e-polo", "polo", "blanco")
    db.put_sale({"tx_id": f"TX-{i}", "store_id": S, "timestamp": "2026-10-03T18:50:00-05:00", "currency": "PAB",
                 "shelf_id": shelf, "brand": "x", "garment_type": garment, "color": color, "amount": amount})
db.put_alert(alert(1, "high_value_opportunity", "2026-10-03T18:17:00-05:00", evidence={"zone_id": "e-polo"}))
db.put_alert(alert(2, "loss_risk_review", "2026-10-03T18:25:00-05:00", title="Riesgo de pérdida — revisar"))
db.review_alert(S, "ALT-20261003-000001", "attended", "ana")
# A Jetson re-sending the alert must not undo the review
db.put_alert({**alert(1, "high_value_opportunity", "2026-10-03T18:17:00-05:00", evidence={"zone_id": "e-polo"}), "severity": "high"})

lo, hi, _ = db.period("today", now=NOW)
s = db.summary(S, lo, hi)
check("visitors exclude staff", s["visitors"] == 3, s["visitors"])
check("interactions are touched + taken", s["garment_interactions"] == 13, s["garment_interactions"])
check("conversion is sales / visitors", s["conversion_rate"] == 1.0 and s["revenue"] == 700.0, s)
check("visits pair entry and exit", s["avg_visit_minutes"] == 10.0, s["avg_visit_minutes"])
check("peak hour is store-local", s["peak_hour"] == {"hour": "18:00 - 19:00", "entries": 2}, s["peak_hour"])
check("top shelf by interactions", s["top_shelf"]["shelf_id"] in ("e-polo", "e-dress"), s["top_shelf"])
check("alert counts by A-code", s["alerts"]["by_type"] == {"A2 high_value_opportunity": 1, "A4 loss_risk_review": 1}, s["alerts"])
check("a review survives a re-send", s["alerts"]["by_status"] == {"attended": 1, "pending_review": 1}, s["alerts"])

a = db.alerts(S, lo, hi, alert_type="a4")
check("alerts filter by A-code", a["total"] == 1 and a["items"][0]["code"] == "A4", a)
check("alerts cross type and status", db.alerts(S, lo, hi)["by_type_and_status"] == {
    "A2 high_value_opportunity": {"attended": 1}, "A4 loss_risk_review": {"pending_review": 1}}, db.alerts(S, lo, hi))
check("status takes Spanish", db.alerts(S, lo, hi, status="atendidas")["total"] == 1)
try:
    db.alerts(S, lo, hi, status="whatever")
    check("an unknown status is refused, not an empty answer", False)
except PeriodError:
    check("an unknown status is refused, not an empty answer", True)
check("alerts count per day with weekday", a["by_day"] == [{"day": "2026-10-03 saturday", "total": 1, "pending_review": 1}], a["by_day"])

lay = db.layout(S, lo, hi)
rows = {r["shelf_id"]: r for r in lay["shelves"]}
check("dress shelf: interest without sales", lay["interest_without_sales"] == "e-dress", lay)
check("suit shelf: high value, little traffic", "alto_valor_poco_trafico" in rows["e-suit"]["diagnosis"], rows["e-suit"])
check("polo shelf sells", rows["e-polo"]["units_sold"] == 2 and rows["e-polo"]["conversion"] == round(2 / 6, 3), rows["e-polo"])
check("best seller by revenue", lay["best_seller"] == "e-suit", lay["best_seller"])

r = db.ranking(S, lo, hi, category="Polos", color="BLANCO")
check("ranking filters plural categories and colour case",
      r["ranking"] == [{"garment_type": "polo", "color": "blanco", "interactions": 6, "units_sold": 2, "revenue": 240.0}], r)

advice = db.restock_advice(S, now=NOW)["advice"]
rules = {x["rule"] for x in advice}
check("advice: move the suit shelf (R2) and review the dresses (R3)", {"R2 reubicar", "R3 exhibicion"} <= rules, rules)
check("advice: no staff rule (R4) once the only A2 is attended", "R4 personal" not in rules, rules)

print("STORE_LANG")
es = Store(":memory:", TZ, "es")
es.put_alert(alert(1, "high_value_opportunity", "2026-10-03T18:17:00-05:00"))
lo, hi, _ = es.period("today", now=NOW)
check("es: weekdays in Spanish", es.alerts(S, lo, hi)["by_day"][0]["day"] == "2026-10-03 sábado", es.alerts(S, lo, hi)["by_day"])
check("es: rule texts in Spanish", "inventario" in es.restock_advice(S, now=NOW)["note"])
check("en: rule texts in English", "inventory" in db.restock_advice(S, now=NOW)["note"])
check("weekdays work in either language", db.period("martes", now=NOW) == db.period("tuesday", now=NOW)[:2] + (db.period("martes", now=NOW)[2],))
try:
    Store(":memory:", TZ, "fr")
    check("an unknown language is refused", False)
except ValueError:
    check("an unknown language is refused", True)

print("sinks")
lines = sinks.event_lines({**ok_event, "dwell_seconds": 12}, TZ)
check("event line has tags, count and id", lines[0].startswith("boutique_event,camera_id=cam-1,role=customer,store_id=boutique-test,type=customer_entry count=1i,dwell_seconds=12.0,event_id=\"EVT-20261003-000001\""), lines)
check("event ids keep points apart below the millisecond", lines[0].endswith("000001"), lines)
hb = {"schema": contract.HEARTBEAT, "store_id": S, "timestamp": "2026-10-03T18:00:00-05:00", "status": "healthy",
      "device": {"hostname": "jetson 1"}, "metrics": {"temperature_gpu_c": 55}, "pipeline": {"active_tracks": 2}, "alerts_pending": 1}
lines = sinks.heartbeat_lines(hb, TZ)
check("heartbeat numbers become sensor_data",
      any(line.startswith("sensor_data,device=jetson\\ 1,sensor=temperature_gpu_c value=55.0") for line in lines), lines)
check("heartbeat keeps alerts_pending", any("sensor=alerts_pending" in line for line in lines), lines)

print("demo simulator")
plan = simulate.plan_day(date(2026, 9, 29))
again = simulate.plan_day(date(2026, 9, 29))
check("a day's plan is the same on every run", plan["events"] == again["events"] and plan["alerts"] == again["alerts"])
errors = [contract.check_event(d) for _, d in plan["events"]] + [contract.check_alert(d) for _, d in plan["alerts"]]
errors += [contract.check_sale(d) for _, d in plan["sales"]]
check("every simulated document follows the contract", not any(errors), [e for e in errors if e][:3])
ids = [d["event_id"] for _, d in plan["events"]]
check("event ids are unique and in time order", len(set(ids)) == len(ids) and ids == sorted(ids))

sys.exit(1 if failed else 0)
