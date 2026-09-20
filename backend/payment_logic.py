"""Payment logic for calculating parking and charging costs."""
from datetime import datetime

PARKING_RATE_PER_HOUR = 2.0
CHARGING_RATE_PER_HOUR = 5.0

def process_payment(car_type, start_time, end_time, at_exit=True, already_paid=False):
    if already_paid:
        return {"parkingCost": 0.0, "chargingCost": 0.0}

    if not isinstance(start_time, datetime):
        start_time = datetime.now()
    if not isinstance(end_time, datetime):
        end_time = datetime.now()

    duration_seconds = max(0, (end_time - start_time).total_seconds())
    duration_hours = max(1.0 / 60.0, duration_seconds / 3600.0)

    parking_cost = round(duration_hours * PARKING_RATE_PER_HOUR, 2)
    charging_cost = 0.0

    if str(car_type).upper() in ("EV", "ELECTRIC"):
        charging_cost = round(duration_hours * CHARGING_RATE_PER_HOUR, 2)

    return {
        "parkingCost": max(0.50, parking_cost),
        "chargingCost": charging_cost
    }

def get_post_payment_action(payment_valid):
    return "ALLOW_DEPARTURE" if payment_valid else "DENY_DEPARTURE"