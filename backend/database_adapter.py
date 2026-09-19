"""Bridge between CarFlow and Person 3's SQLite service layer.

All database access for the parking flow goes through this class, which only
calls functions from database/database_service.py. CarFlow never touches SQL.
"""
import logging
import sys
from datetime import datetime
from pathlib import Path

# `database` is a package at the repo root; backend scripts run from backend/.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)

from database import database_service as service  # noqa: E402
from database.database import init_database  # noqa: E402


logger = logging.getLogger(__name__)

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# Spots we create ourselves are parking spots: the flow only ever assigns and
# occupies those. Storing this (instead of NULL) is what keeps them on the
# dashboard's map, which only shows rows whose purpose is "Park".
DEFAULT_PURPOSE = "Park"


def _iso(when):
    """Simulator times are stored as ISO strings; None lets the service default apply."""
    return when.isoformat(timespec="seconds") if when else None


def _event_time(event):
    """ServerDateTime as an ISO string, so events share the clock the sessions use."""
    try:
        return datetime.strptime(event["ServerDateTime"], TIME_FORMAT).isoformat(timespec="seconds")
    except (KeyError, TypeError, ValueError):
        return None


def _spot_fields(spot):
    """A list_parking_spots() entry -> upsert_parking_spot() arguments."""
    return {
        "name": spot["name"],
        "zone": spot.get("zoneParent"),
        "purpose": spot.get("purpose"),
        "parking_for_car_type": spot.get("parkingForCarType"),
        # detectedCars is a count; the plate itself is not known here.
        "status": "occupied" if spot.get("detectedCars") else "available",
        "current_car": None,
        "broken": spot.get("broken", False),
        "under_maintenance": spot.get("isUnderMaintenance", False),
    }


def _as_list(payload):
    """list_barriers() may return a bare list or an object wrapping one."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("barriers", "barrierGates", "gates", "items", "data"):
            if isinstance(payload.get(key), list):
                return payload[key]
    return []


def _first(mapping, *keys):
    """The value of the first key that is present and not None."""
    for key in keys:
        if mapping.get(key) is not None:
            return mapping[key]
    return None


class DatabaseAdapter:
    def initialize(self):
        init_database()

    # ---- simulator sync ---------------------------------------------------

    def sync_spots(self, spots):
        """Store the simulator's parking spots (list_parking_spots() shape)."""
        for spot in spots:
            service.upsert_parking_spot(**_spot_fields(spot))

    def sync_new_spots(self, spots):
        """Store only the spots the database has not seen yet.

        Unlike sync_spots this never overwrites a row we already hold, so it is
        safe to call while cars are parked: live status and current_car survive.
        The flow calls it on every allocation, so a failed startup sync repairs
        itself as soon as the simulator answers again.
        """
        known = {row["name"] for row in service.get_parking_spots()}
        for spot in spots:
            if spot.get("name") not in known:
                service.upsert_parking_spot(**_spot_fields(spot))

    def sync_gates(self, barriers):
        """Store the simulator's barrier gates (list_barriers() shape).

        The response shape is still unconfirmed, so field names are read
        defensively and anything unrecognisable is skipped and logged rather
        than guessed into the database.
        """
        for gate in _as_list(barriers):
            if not isinstance(gate, dict):
                continue
            name = _first(gate, "name", "Name", "id", "Id")
            if name is None:
                logger.warning("Barrier without a recognisable name skipped: %s", gate)
                continue
            service.upsert_gate(
                name=str(name),
                state=str(_first(gate, "state", "State", "status", "Status") or "Closed"),
                broken=bool(_first(gate, "broken", "isBroken", "Broken")),
                under_maintenance=bool(
                    _first(gate, "isUnderMaintenance", "underMaintenance", "IsUnderMaintenance")
                ),
            )

    def sync(self, client):
        """Read-only: fetch spots and gates from the simulator and store them."""
        self.sync_spots(client.list_parking_spots())
        try:
            self.sync_gates(client.list_barriers())
        except Exception:
            # An unconfirmed list_barriers() shape (or a client without it) must
            # never cost us the spot sync, which is what the map needs.
            logger.exception("Gate sync failed; the dashboard's gate panel stays empty")

    # ---- events -----------------------------------------------------------

    def log_event(self, event):
        service.log_event(
            event_type=event.get("EventClass") or "unknown",
            payload=event,
            car_name=event.get("CarPlateNumber"),
            component_name=event.get("SpotName"),
            created_at=_event_time(event),
        )

    # ---- sessions ---------------------------------------------------------

    def start_session(self, plate, car_type, arrival_time):
        return service.start_parking_session(
            car_name=plate,
            car_type=car_type,
            arrival_time=_iso(arrival_time),
        )

    def assign_spot(self, session_id, spot_name):
        self._ensure_spot(spot_name)  # sessions.spot_name is a foreign key
        service.assign_session_spot(session_id, spot_name)

    def mark_parked(self, session_id, when):
        service.mark_car_parked(session_id, _iso(when))

    def complete_session(self, session_id, when):
        service.end_parking_session(session_id, _iso(when))

    # ---- spot occupancy ---------------------------------------------------

    def set_spot_occupied(self, spot_name, plate):
        self._set_spot(spot_name, "occupied", plate)

    def set_spot_available(self, spot_name):
        self._set_spot(spot_name, "available", None)

    def _get_spot(self, spot_name):
        for row in service.get_parking_spots():
            if row["name"] == spot_name:
                return row
        return None

    def _ensure_spot(self, spot_name):
        if self._get_spot(spot_name) is None:
            service.upsert_parking_spot(name=spot_name, purpose=DEFAULT_PURPOSE)

    def _set_spot(self, spot_name, status, current_car):
        # upsert_parking_spot overwrites every column, so carry the rest over.
        row = self._get_spot(spot_name) or {}
        service.upsert_parking_spot(
            name=spot_name,
            zone=row.get("zone"),
            purpose=row.get("purpose") or DEFAULT_PURPOSE,
            parking_for_car_type=row.get("parking_for_car_type"),
            status=status,
            current_car=current_car,
            broken=bool(row.get("broken")),
            under_maintenance=bool(row.get("under_maintenance")),
        )

    # ---- payments ---------------------------------------------------------

    def record_pending_charge(self, session_id, parking_cost, charging_cost):
        return service.record_payment(
            session_id, parking_cost, charging_cost, status="pending"
        )

    def mark_payment_paid(self, payment_id, when=None):
        service.mark_payment_paid(payment_id, _iso(when))
