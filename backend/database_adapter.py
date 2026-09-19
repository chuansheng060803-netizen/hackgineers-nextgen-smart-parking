"""Bridge between CarFlow and the SQLite database service."""

import sys
from pathlib import Path
from datetime import datetime, timezone


# Allow backend scripts to import the database package
_REPO_ROOT = str(Path(__file__).resolve().parent.parent)

if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)


from database import database_service as service
from database.database import init_database


def _iso(when):
    """Convert datetime to ISO string for SQLite."""
    if when is None:
        return None

    if isinstance(when, str):
        return when

    return when.isoformat(timespec="seconds")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DatabaseAdapter:

    # =========================================================
    # INITIALIZATION
    # =========================================================

    def initialize(self):
        init_database()

    # =========================================================
    # INITIAL SIMULATOR STATE
    # =========================================================

    def sync_spots(self, spots):
        """
        Create/update parking spot state using the simulator's
        list_parking_spots response.
        """

        now = _now()

        for spot in spots:

            status = (
                "OCCUPIED"
                if spot.get("detectedCars")
                else "FREE"
            )

            service.add_or_update_spot(
                name=spot["name"],
                zone=spot.get("zoneParent"),
                status=status,
                updated_at=now,
            )

            # If simulator confirms it is free,
            # remove any old stored plate.
            if status == "FREE":
                service.set_spot_available(
                    spot["name"],
                    now,
                )

    def sync_gates(self, gates):
        """
        Create/update gate state from list_barriers.
        """

        now = _now()

        for gate in gates:

            service.update_gate_status(
                gate_name=gate["name"],
                status=str(
                    gate.get("state", "CLOSED")
                ).upper(),
                updated_at=now,
            )

    def sync(self, client):
        """
        Initial database sync when backend starts.
        """

        self.sync_spots(
            client.list_parking_spots()
        )

        self.sync_gates(
            client.list_barriers()
        )

    # =========================================================
    # EVENTS
    # =========================================================

    def log_event(self, event):
        service.log_event(event)

    # =========================================================
    # PARKING SESSION
    # =========================================================

    def start_session(
        self,
        plate,
        car_type,
        entry_gate_name,
        arrival_time,
    ):
        return service.start_session(
            plate=plate,
            car_type=car_type,
            entry_gate_name=entry_gate_name,
            arrival_time=_iso(arrival_time),
        )

    def start_parking(
        self,
        session_id,
        spot_name,
        when,
    ):
        service.start_parking(
            session_id=session_id,
            spot_name=spot_name,
            start_park=_iso(when),
        )

    def end_parking(
        self,
        session_id,
        when,
    ):
        service.end_parking(
            session_id=session_id,
            end_park=_iso(when),
        )

    def complete_session(
        self,
        session_id,
        exit_gate_name,
        when,
    ):
        service.complete_session(
            session_id=session_id,
            exit_gate_name=exit_gate_name,
            exit_time=_iso(when),
        )

    # =========================================================
    # PARKING SPOTS
    # =========================================================

    def set_spot_occupied(
        self,
        spot_name,
        plate,
        when,
    ):
        service.set_spot_occupied(
            spot_name=spot_name,
            plate=plate,
            updated_at=_iso(when),
        )

    def set_spot_available(
        self,
        spot_name,
        when,
    ):
        service.set_spot_available(
            spot_name=spot_name,
            updated_at=_iso(when),
        )

    # =========================================================
    # PAYMENTS
    # =========================================================

    def record_pending_charge(
        self,
        session_id,
        parking_cost,
        charging_cost,
    ):
        return service.create_payment(
            session_id=session_id,
            parking_cost=parking_cost,
            charging_cost=charging_cost,
        )

    def mark_payment_paid(
        self,
        payment_id,
        when,
    ):
        service.mark_payment_paid(
            payment_id=payment_id,
            paid_time=_iso(when),
        )

    def complete_payment(
        self,
        payment_id,
        when,
    ):
        service.complete_payment(
            payment_id=payment_id,
            completed_time=_iso(when),
        )

    # =========================================================
    # GATES
    # =========================================================

    def update_gate_status(
        self,
        gate_name,
        status,
        when,
    ):
        service.update_gate_status(
            gate_name=gate_name,
            status=status.upper(),
            updated_at=_iso(when),
        )