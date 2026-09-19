def calculate_charges(car_type, minutes_parked):
    parking_cost = minutes_parked

    if car_type == "Electric":
        charging_cost = minutes_parked * 2
    else:
        charging_cost = 0

    return {
        "parkingCost": parking_cost,
        "chargingCost": charging_cost
    }


def can_charge(at_exit, already_paid):
    if not at_exit:
        return False

    if already_paid:
        return False

    return True


def calculate_parking_minutes(entry_time, exit_time):
    duration = exit_time - entry_time

    total_seconds = duration.total_seconds()

    minutes = total_seconds / 60

    return minutes


def process_payment(car_type, entry_time, exit_time, at_exit, already_paid):

    # Step 1: Check whether this car should be charged
    if not can_charge(at_exit, already_paid):
        return None

    # Step 2: Calculate how long the car parked
    minutes_parked = calculate_parking_minutes(
        entry_time,
        exit_time
    )

    # Step 3: Calculate the charges
    charges = calculate_charges(
        car_type,
        minutes_parked
    )

    # Step 4: Return the result
    return charges


def get_post_payment_action(payment_successful):
    if payment_successful:
        return "ALLOW_DEPARTURE"

    return "PAYMENT_FAILED"