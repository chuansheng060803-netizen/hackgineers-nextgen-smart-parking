"""Manual smoke test for the service layer. Writes to its own scratch database.

Run it from the repo root:  python -B -m database.test_database
"""
import os
from pathlib import Path

# Never write test rows into the real parking.db: they end up on the dashboard
# as cars that never existed. Set PARKING_DB_PATH yourself to override.
os.environ.setdefault(
    "PARKING_DB_PATH",
    str(Path(__file__).resolve().parent / "test_scratch.db"),
)

from database.database import init_database  # noqa: E402

from database.database_service import (
    upsert_parking_spot,
    upsert_gate,
    start_parking_session,
    mark_car_parked,
    end_parking_session,
    get_parking_session,
    record_payment,
    log_event,
    get_parking_spots,
    get_gates,
    get_active_sessions,
    get_recent_events,
)


init_database()


# ---------------------------------------------
# Create parking spot
# ---------------------------------------------

upsert_parking_spot(
    name="S1",
    zone="ZONE1",
    purpose="Park",
    parking_for_car_type="Any",
)


# ---------------------------------------------
# Create gate
# ---------------------------------------------

upsert_gate(
    name="gate0",
    state="Closed",
)


# ---------------------------------------------
# Start parking session
# ---------------------------------------------

session_id = start_parking_session(
    car_name="CAR_TEST_001",
    car_type="Normal",
    spot_name="S1",
)

print("Created session:", session_id)


# ---------------------------------------------
# Car parked
# ---------------------------------------------

mark_car_parked(session_id)


# ---------------------------------------------
# Store payment
# ---------------------------------------------

payment_id = record_payment(
    session_id=session_id,
    parking_cost=10,
    charging_cost=0,
)

print("Created payment:", payment_id)

# ---------------------------------------------
# Car leaves the car park
# ---------------------------------------------

end_parking_session(session_id)

completed_session = get_parking_session(session_id)

assert completed_session is not None
assert completed_session["departure_time"] is not None
assert completed_session["status"] == "completed"

print("✅ Departure recorded")
print("✅ Parking session completed")

# ---------------------------------------------
# Store event
# ---------------------------------------------

event_id = log_event(
    event_type="car_spot_action",
    car_name="CAR_TEST_001",
    payload={
        "action": "parked",
        "spot": "S1"
    },
)

print("Created event:", event_id)


print("\nParking spots:")
print(get_parking_spots())

print("\nGates:")
print(get_gates())

print("\nActive sessions:")
print(get_active_sessions())

print("\nRecent events:")
print(get_recent_events())