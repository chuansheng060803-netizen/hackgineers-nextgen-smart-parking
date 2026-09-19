"""Bridge between CarFlow and Person 3's SQLite service layer.

All database access for the parking flow goes through this class, which only
calls functions from database/database_service.py. CarFlow never touches SQL.
"""
import sys
from pathlib import Path

# `database` is a package at the repo root; backend scripts run from backend/.
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)

from database import database_service as service  # noqa: E402
from database.database import init_database  # noqa: E402


def _iso(when):
    """Simulator times are stored as ISO strings; None lets the service default apply."""
    return when.isoformat(timespec="seconds") if when else None


class DatabaseAdapter:
    def initialize(self):
        init_database()

    # ---- simulator sync ---------------------------------------------------

    def sync_spots(self, spots):
        """Store the simulator's parking spots (list_parking_spots() shape)."""
        for spot in spots:
            service.upsert_parking_spot(
                name=spot["name"],
                zone=spot.get("zoneParent"),
                purpose=spot.get("purpose"),
                parking_for_car_type=spot.get("parkingForCarType"),
                # detectedCars is a count; the plate itself is not known here.
                status="occupied" if spot.get("detectedCars") else "available",
                current_car=None,
                broken=spot.get("broken", False),
                under_maintenance=spot.get("isUnderMaintenance", False),
            )

    def sync(self, client):
        """Read-only: fetch spots from the simulator and store them.

        Gates are not synced yet: the list_barriers() response shape has not
        been confirmed, so no gate fields are guessed.
        """
        self.sync_spots(client.list_parking_spots())

    # ---- events -----------------------------------------------------------

    def log_event(self, event):
        service.log_event(
            event_type=event.get("EventClass") or "unknown",
            payload=event,
            car_name=event.get("CarPlateNumber"),
            component_name=event.get("SpotName"),
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
            service.upsert_parking_spot(name=spot_name)

    def _set_spot(self, spot_name, status, current_car):
        # upsert_parking_spot overwrites every column, so carry the rest over.
        row = self._get_spot(spot_name) or {}
        service.upsert_parking_spot(
            name=spot_name,
            zone=row.get("zone"),
            purpose=row.get("purpose"),
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

    def mark_payment_paid(self, payment_id):
        service.mark_payment_paid(payment_id)
