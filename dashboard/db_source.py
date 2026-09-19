"""Builds the dashboard snapshot from the SQLite database (read-only).

The backend (run_flow.py --db) is the only writer. This module only calls the
read functions of database/database_service.py: no SQL, no simulator, no network.
Anything the database does not store (fans, CO, penalty amounts, occupancy history,
the 30-day archive) is left empty, so those panels show their normal empty state.
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

# `database` is a package at the repo root; the dashboard runs from dashboard/.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)

import database.database as database_module  # noqa: E402
from database import database_service as service  # noqa: E402

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_SESSIONS = 5000     # how far back the history and the 30-day archive look
ARCHIVE_DAYS = 30       # the organisers' retention window


class DatabaseNotFound(Exception):
    """The SQLite file does not exist yet (the backend has not created it)."""


def _parse(value):
    """ISO / 'YYYY-MM-DD HH:MM:SS' text -> naive datetime, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except ValueError:
        return None


def _fmt(when):
    return when.strftime(TIME_FORMAT) if when else ""


def _natural(name):
    return int("".join(ch for ch in name if ch.isdigit()) or 0)


def _payment_summary(payments):
    """session_id -> {"total": float, "status": "paid" | "pending"}."""
    by_session = {}
    for p in payments:
        entry = by_session.setdefault(p["session_id"], {"total": 0.0, "paid": True})
        entry["total"] += (p["parking_cost"] or 0) + (p["charging_cost"] or 0)
        entry["paid"] = entry["paid"] and p["status"] == "paid"
    return {sid: {"total": e["total"], "status": "paid" if e["paid"] else "pending"}
            for sid, e in by_session.items()}


def _event_row(ev):
    payload = ev.get("payload") or {}
    event_class = ev.get("event_type")
    plate = ev.get("car_name") or payload.get("CarPlateNumber")
    place = ev.get("component_name") or payload.get("SpotName")
    when = _parse(payload.get("ServerDateTime")) or _parse(ev.get("created_at"))

    if event_class == "car_spot_action":
        spot_type, direction = payload.get("SpotType"), payload.get("Direction")
        kind = "entry" if spot_type == "EntrySpot" else "exit" if spot_type == "ExitSpot" else "park"
        verb = {"CarIn": "arrived at", "CarOut": "left"}.get(direction, "moved at")
        text = f"{plate} {verb} {place}"
    elif event_class == "payment_made":
        kind = "payment"
        reason = payload.get("Reason")
        text = f"Payment reported for {plate}: {payload.get('Amount')}" + (f" ({reason})" if reason else "")
    elif event_class == "penalty":
        kind, text = "penalty", "Penalty event reported" + (f" for {plate}" if plate else "")
    elif event_class == "gate_action":
        kind, text = "gate", "Gate event" + (f": {place}" if place else "")
    elif event_class in ("component_broken", "component_fixed"):
        kind = "component"
        text = ("Component broken" if event_class == "component_broken" else "Component fixed") + (f": {place}" if place else "")
    elif event_class == "carbon_monoxide_event":
        kind, text = "co", "Carbon monoxide event"
    else:
        kind, text = str(event_class), str(event_class)
    return {"t": when.strftime("%H:%M:%S") if when else "", "kind": kind, "text": text, "plate": plate}


def _arrivals(sessions, now):
    """Cars arriving per 5 minutes over the last hour (simulator time)."""
    if now is None:
        return []
    start = now.replace(second=0, microsecond=0, minute=(now.minute // 5) * 5) - timedelta(minutes=55)
    counts = {}
    for s in sessions:
        arrived = _parse(s["arrival_time"])
        if arrived and arrived >= start:
            bucket = arrived.replace(second=0, microsecond=0, minute=(arrived.minute // 5) * 5)
            counts[bucket] = counts.get(bucket, 0) + 1
    return [{"t": (start + timedelta(minutes=5 * i)).isoformat(timespec="seconds"),
             "count": counts.get(start + timedelta(minutes=5 * i), 0)} for i in range(12)]


def _occupancy(sessions, now, total):
    """Cars inside, sampled every 5 minutes over the last hour (simulator time).

    Derived from the session timestamps rather than stored samples: a car counts
    from the moment it arrived until its session was completed.
    """
    if now is None or not total:
        return []
    start = now.replace(second=0, microsecond=0, minute=(now.minute // 5) * 5) - timedelta(minutes=55)
    spans = [(a, _parse(s["departure_time"]))
             for s, a in ((s, _parse(s["arrival_time"])) for s in sessions) if a]
    rows = []
    for i in range(12):
        t = start + timedelta(minutes=5 * i)
        inside = sum(1 for a, d in spans if a <= t and (d is None or d > t))
        rows.append({"t": t.isoformat(timespec="seconds"), "occupied": inside, "total": total})
    return rows


def _visit(session, arrived, payments):
    """One row for the History tables, in the shape the dashboard expects."""
    left = _parse(session["departure_time"])
    payment = payments.get(session["id"])
    if session["status"] == "active":
        status = "Inside"
    elif not payment:
        status = "Completed"
    else:
        status = "Completed - paid" if payment["status"] == "paid" else "Completed - payment pending"
    return {"plate": session["car_name"], "car_type": session["car_type"],
            "spot": session["spot_name"] or "-",
            "entered_at": _fmt(arrived), "left_at": _fmt(left),
            "minutes": round((left - arrived).total_seconds() / 60.0, 1) if arrived and left else 0.0,
            "charge": round(payment["total"], 2) if payment else 0.0,
            "status": status}


def _archive(sessions, payments, events, total):
    """One summary row per day, newest first, for the 30-day history view."""
    by_day = {}
    for session in sessions:
        arrived = _parse(session["arrival_time"])
        if arrived:
            by_day.setdefault(arrived.date(), []).append((session, arrived))

    penalties = {}
    for event in events:
        if event.get("event_type") == "penalty":
            when = _parse((event.get("payload") or {}).get("ServerDateTime")) or _parse(event.get("created_at"))
            if when:
                penalties[when.date()] = penalties.get(when.date(), 0) + 1

    rows = []
    for day in sorted(by_day, reverse=True)[:ARCHIVE_DAYS]:
        items = by_day[day]
        parked = [s for s, _ in items if s["parked_time"]]
        income = sum(payments[s["id"]]["total"] for s, _ in items
                     if payments.get(s["id"], {}).get("status") == "paid")
        edges, minutes = [], []
        for session, arrived in items:
            left = _parse(session["departure_time"])
            edges.append((arrived, 1))
            if left:
                edges.append((left, -1))
                minutes.append((left - arrived).total_seconds() / 60.0)
        level = peak = 0
        for _, delta in sorted(edges):    # a departure at the same instant lands first
            level += delta
            peak = max(peak, level)
        rows.append({"date": day.isoformat(), "visits": len(items), "cars_parked": len(parked),
                     "drive_through": len(items) - len(parked), "income": round(income, 2),
                     "penalties": penalties.get(day, 0),
                     "peak_pct": min(100, round(100 * peak / total)) if total else 0,
                     "avg_minutes": round(sum(minutes) / len(minutes), 1) if minutes else 0.0})
    return rows


def history(date):
    """Every visit of one day, for the dashboard's 30-day view. date: 'YYYY-MM-DD'."""
    payments = _payment_summary(service.get_payments())
    rows = []
    for session in service.get_sessions(limit=MAX_SESSIONS):
        arrived = _parse(session["arrival_time"])
        if arrived and arrived.date().isoformat() == str(date):
            rows.append(_visit(session, arrived, payments))
    return rows


def fetch_db():
    path = Path(database_module.DATABASE_PATH)
    if not path.exists():
        raise DatabaseNotFound(f"Database file not found: {path}. Start the backend with --db first.")

    spot_rows = service.get_parking_spots()
    gate_rows = service.get_gates()
    all_sessions = service.get_sessions(limit=MAX_SESSIONS)
    active = [s for s in all_sessions if s["status"] == "active"]
    finished = [s for s in all_sessions if s["status"] == "completed"]
    payments = _payment_summary(service.get_payments())
    events = service.get_recent_events(limit=1000)

    # "Now" is the newest simulator time we have seen, never the wall clock.
    now = next((t for t in (_parse((e.get("payload") or {}).get("ServerDateTime")) for e in events) if t), None)

    # A spot a car has been sent to but has not reached yet.
    assigned = {s["spot_name"]: s["car_name"] for s in active if s["spot_name"] and not s["parked_time"]}

    spots = []
    for row in sorted((r for r in spot_rows if r["purpose"] == "Park"), key=lambda r: _natural(r["name"])):
        if row["broken"]:
            state = "broken"
        elif row["under_maintenance"]:
            state = "maintenance"
        elif row["status"] == "occupied":
            state = "occupied"
        elif row["name"] in assigned:
            state = "reserved"
        else:
            state = "available"
        spots.append({"name": row["name"], "zone": row["zone"], "type": row["parking_for_car_type"] or "Any",
                      "state": state, "car": row["current_car"] or assigned.get(row["name"]), "health": "ok"})

    cars = []
    for s in active:
        arrived = _parse(s["arrival_time"])
        minutes = max(0.0, (now - arrived).total_seconds() / 60.0) if now and arrived else 0.0
        payment = payments.get(s["id"])
        cars.append({"plate": s["car_name"], "car_type": s["car_type"],
                     "status": "Parked" if s["parked_time"] else "Heading to spot" if s["spot_name"] else "Waiting for a spot",
                     "spot": s["spot_name"] or "-",
                     "entered_at": arrived.strftime("%H:%M:%S") if arrived else "",
                     "minutes_inside": round(minutes, 1),
                     "estimated_charge": round(payment["total"], 2) if payment else 0.0,
                     "flag": None, "assigned_spot": None})
    cars.sort(key=lambda c: c["entered_at"], reverse=True)

    sessions = [_visit(s, _parse(s["arrival_time"]), payments) for s in finished]

    revenue = sum(p["total"] for p in payments.values() if p["status"] == "paid")

    return {
        "generated_at": _fmt(now),
        "source": "database",
        "spots": spots,
        "cars": cars,
        "events": [_event_row(e) for e in events[:200]],
        "sessions": sessions,
        "gates": [{"name": g["name"], "state": g["state"], "role": "", "zone": "",
                   "health": "broken" if g["broken"] else "maintenance" if g["under_maintenance"] else "ok"}
                  for g in gate_rows],
        "archive": _archive(all_sessions, payments, events, len(spots)),
        "history": {"arrivals": _arrivals(all_sessions, now),
                    "occupancy": _occupancy(all_sessions, now, len(spots))},
        "stats": {"revenue": round(revenue, 2), "cars_served": len(finished),
                  "penalty_count": sum(1 for e in events if e.get("event_type") == "penalty")},
    }
