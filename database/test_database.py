from database.database import init_database

from database.database_service import (
    upsert_parking_spot,
    upsert_gate,
    start_parking_session,
    mark_car_parked,
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