"""Build the dashboard snapshot directly from the V2 SQLite database."""

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path


_REPO_ROOT = str(Path(__file__).resolve().parent.parent)

if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)


import database.database as database_module
from database import database_service as service


TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class DatabaseNotFound(Exception):
    """Raised when the SQLite database has not been created yet."""


def _parse(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=None)
    except ValueError:
        return None


def _fmt(value):
    return value.strftime(TIME_FORMAT) if value else ""


def _natural(name):
    return int(
        "".join(ch for ch in str(name) if ch.isdigit()) or 0
    )


def _payment_summary(payments):
    """Return payment information grouped by session id."""

    by_session = {}

    for payment in payments:
        session_id = payment["session_id"]

        entry = by_session.setdefault(
            session_id,
            {
                "total": 0.0,
                "paid": True,
            },
        )

        entry["total"] += (
            float(payment["parking_cost"] or 0)
            + float(payment["charging_cost"] or 0)
        )

        status = str(payment["status"] or "").upper()

        if status not in ("PAID", "COMPLETED"):
            entry["paid"] = False

    return {
        session_id: {
            "total": entry["total"],
            "status": "paid" if entry["paid"] else "pending",
        }
        for session_id, entry in by_session.items()
    }


def _event_payload(event):
    raw = event.get("raw_data")

    if not raw:
        return {}

    try:
        data = json.loads(raw)

        if isinstance(data, dict):
            return data

    except (TypeError, ValueError):
        pass

    return {}


def _event_row(event):
    payload = _event_payload(event)

    event_class = event.get("event_class")

    plate = (
        event.get("plate")
        or payload.get("CarPlateNumber")
    )

    place = (
        payload.get("SpotName")
        or payload.get("GateName")
        or payload.get("ComponentName")
    )

    when = (
        _parse(event.get("event_time"))
        or _parse(payload.get("ServerDateTime"))
    )

    if event_class == "car_spot_action":
        spot_type = payload.get("SpotType")
        direction = payload.get("Direction")

        if spot_type == "EntrySpot":
            kind = "entry"
        elif spot_type == "ExitSpot":
            kind = "exit"
        else:
            kind = "park"

        if direction == "CarIn":
            verb = "arrived at"
        elif direction == "CarOut":
            verb = "left"
        else:
            verb = "moved at"

        text = f"{plate or 'Car'} {verb} {place or ''}".strip()

    elif event_class == "payment_made":
        kind = "payment"

        amount = payload.get("Amount")

        text = f"Payment reported for {plate or 'car'}"

        if amount is not None:
            text += f": {amount}"

    elif event_class == "penalty":
        kind = "penalty"
        text = "Penalty event reported"

        if plate:
            text += f" for {plate}"

    elif event_class == "gate_action":
        kind = "gate"
        text = "Gate event"

        if place:
            text += f": {place}"

    elif event_class in (
        "component_broken",
        "component_fixed",
    ):
        kind = "component"

        if event_class == "component_broken":
            text = "Component broken"
        else:
            text = "Component fixed"

        if place:
            text += f": {place}"

    elif event_class == "carbon_monoxide_event":
        kind = "co"
        text = "Carbon monoxide event"

    else:
        kind = str(event_class or "event")
        text = str(event_class or "event")

    return {
        "t": when.strftime("%H:%M:%S") if when else "",
        "kind": kind,
        "text": text,
        "plate": plate,
    }


def _arrivals(sessions, now):
    """Cars arriving per 5-minute interval over the last hour."""

    if now is None:
        return []

    start = now.replace(
        second=0,
        microsecond=0,
        minute=(now.minute // 5) * 5,
    ) - timedelta(minutes=55)

    counts = {}

    for session in sessions:
        arrived = _parse(session.get("arrival_time"))

        if arrived and arrived >= start:
            bucket = arrived.replace(
                second=0,
                microsecond=0,
                minute=(arrived.minute // 5) * 5,
            )

            counts[bucket] = counts.get(bucket, 0) + 1

    return [
        {
            "t": (
                start + timedelta(minutes=5 * i)
            ).isoformat(timespec="seconds"),
            "count": counts.get(
                start + timedelta(minutes=5 * i),
                0,
            ),
        }
        for i in range(12)
    ]


def fetch_db():
    path = Path(database_module.DATABASE_PATH)

    if not path.exists():
        raise DatabaseNotFound(
            f"Database file not found: {path}. "
            "Start the backend with --db first."
        )

    spot_rows = service.get_parking_spots()
    gate_rows = service.get_gates()

    active = service.get_active_sessions()
    finished = service.get_sessions("COMPLETED")

    payments = _payment_summary(
        service.get_payments()
    )

    events = service.get_recent_events(
        limit=1000
    )

    # Find newest timestamp stored in the database.
    timestamps = []

    for event in events:
        value = _parse(event.get("event_time"))

        if value:
            timestamps.append(value)

    for session in active + finished:
        for field in (
            "arrival_time",
            "start_park",
            "end_park",
            "exit_time",
        ):
            value = _parse(session.get(field))

            if value:
                timestamps.append(value)

    for spot in spot_rows:
        value = _parse(spot.get("updated_at"))

        if value:
            timestamps.append(value)

    for gate in gate_rows:
        value = _parse(gate.get("updated_at"))

        if value:
            timestamps.append(value)

    now = (
        max(timestamps)
        if timestamps
        else datetime.now()
    )

    # Cars assigned a spot but not parked yet.
    assigned = {
        session["spot_name"]: session["plate"]
        for session in active
        if session.get("spot_name")
        and not session.get("start_park")
    }

    spots = []

    for row in sorted(
        spot_rows,
        key=lambda item: _natural(item["name"]),
    ):
        status = str(
            row.get("status") or "FREE"
        ).upper()

        if status == "OCCUPIED":
            state = "occupied"

        elif status in (
            "BROKEN",
            "FAULT",
            "FAILED",
        ):
            state = "broken"

        elif status in (
            "MAINTENANCE",
            "UNDER_MAINTENANCE",
        ):
            state = "maintenance"

        elif row["name"] in assigned:
            state = "reserved"

        else:
            state = "available"

        if state == "broken":
            health = "broken"

        elif state == "maintenance":
            health = "maintenance"

        else:
            health = "ok"

        spots.append({
            "name": row["name"],
            "zone": row.get("zone") or "",
            "type": "Any",
            "state": state,
            "car": (
                row.get("current_plate")
                or assigned.get(row["name"])
            ),
            "health": health,
        })

    cars = []

    for session in active:
        arrived = _parse(
            session.get("arrival_time")
        )

        if arrived:
            minutes = max(
                0.0,
                (now - arrived).total_seconds()
                / 60.0,
            )
        else:
            minutes = 0.0

        payment = payments.get(
            session["id"]
        )

        if session.get("end_park"):
            status = "Heading to exit"

        elif session.get("start_park"):
            status = "Parked"

        elif session.get("spot_name"):
            status = "Heading to spot"

        else:
            status = "Waiting for a spot"

        cars.append({
            "plate": session["plate"],
            "car_type": (
                session.get("car_type")
                or "Normal"
            ),
            "status": status,
            "spot": (
                session.get("spot_name")
                or "-"
            ),
            "entered_at": (
                arrived.strftime("%H:%M:%S")
                if arrived
                else ""
            ),
            "minutes_inside": round(
                minutes,
                1,
            ),
            "estimated_charge": (
                round(payment["total"], 2)
                if payment
                else 0.0
            ),
            "flag": None,
            "assigned_spot": None,
        })

    cars.sort(
        key=lambda car: car["entered_at"],
        reverse=True,
    )

    sessions = []

    for session in finished:
        arrived = _parse(
            session.get("arrival_time")
        )

        left = _parse(
            session.get("exit_time")
        )

        payment = payments.get(
            session["id"]
        )

        if payment is None:
            display_status = "Completed"

        elif payment["status"] == "paid":
            display_status = "Completed - paid"

        else:
            display_status = (
                "Completed - payment pending"
            )

        sessions.append({
            "plate": session["plate"],
            "car_type": (
                session.get("car_type")
                or "Normal"
            ),
            "spot": (
                session.get("spot_name")
                or "-"
            ),
            "entered_at": _fmt(arrived),
            "left_at": _fmt(left),
            "minutes": (
                round(
                    (
                        left - arrived
                    ).total_seconds()
                    / 60.0,
                    1,
                )
                if arrived and left
                else 0.0
            ),
            "charge": (
                round(payment["total"], 2)
                if payment
                else 0.0
            ),
            "status": display_status,
        })

    revenue = sum(
        payment["total"]
        for payment in payments.values()
        if payment["status"] == "paid"
    )

    gates = []

    for gate in gate_rows:
        gates.append({
            "name": gate["name"],
            "state": str(
                gate.get("status") or ""
            ).lower(),
            "role": "",
            "zone": "",
            "health": "ok",
        })

    return {
        "generated_at": _fmt(now),
        "source": "database",

        "spots": spots,
        "cars": cars,

        "events": [
            _event_row(event)
            for event in events[:200]
        ],

        "sessions": sessions,

        "gates": gates,

        "history": {
            "arrivals": _arrivals(
                active + finished,
                now,
            )
        },

        "stats": {
            "revenue": round(
                revenue,
                2,
            ),
            "cars_served": len(
                finished
            ),
            "penalty_count": sum(
                1
                for event in events
                if event.get("event_class")
                == "penalty"
            ),
        },
    }
