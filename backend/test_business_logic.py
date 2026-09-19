from datetime import datetime

from parking_algorithm import select_parking_spot
from payment_logic import (
    calculate_charges,
    can_charge,
    calculate_parking_minutes,
    process_payment,
    get_post_payment_action
)


def check(test_name, actual, expected):
    if actual == expected:
        print(f"PASS: {test_name}")
    else:
        print(f"FAIL: {test_name}")
        print(f"  Expected: {expected}")
        print(f"  Actual:   {actual}")


print("\n===== PARKING TESTS =====")

normal_car = {
    "CarPlateNumber": "ABC 123",
    "CarType": "Normal"
}

electric_car = {
    "CarPlateNumber": "EV 123",
    "CarType": "Electric"
}


# TEST 1: Normal available parking
spots = [
    {
        "name": "S1",
        "purpose": "Park",
        "parkingForCarType": "Any",
        "detectedCars": 0,
        "broken": False,
        "isUnderMaintenance": False
    }
]

result = select_parking_spot(normal_car, spots)

check(
    "Normal car gets available spot",
    result["name"] if result else None,
    "S1"
)


# TEST 2: Occupied parking spot
spots[0]["detectedCars"] = 1

result = select_parking_spot(normal_car, spots)

check(
    "Occupied spot is rejected",
    result,
    None
)


# TEST 3: Broken parking spot
spots[0]["detectedCars"] = 0
spots[0]["broken"] = True

result = select_parking_spot(normal_car, spots)

check(
    "Broken spot is rejected",
    result,
    None
)


# TEST 4: Spot under maintenance
spots[0]["broken"] = False
spots[0]["isUnderMaintenance"] = True

result = select_parking_spot(normal_car, spots)

check(
    "Maintenance spot is rejected",
    result,
    None
)


# TEST 5: Entry spot must not be used for parking
entry_spot = [
    {
        "name": "ENTRY1",
        "purpose": "EntrySpot",
        "parkingForCarType": "Any",
        "detectedCars": 0,
        "broken": False,
        "isUnderMaintenance": False
    }
]

result = select_parking_spot(normal_car, entry_spot)

check(
    "Entry spot is rejected",
    result,
    None
)


# TEST 6: Electric car can use Electric spot
electric_spot = [
    {
        "name": "EV1",
        "purpose": "Park",
        "parkingForCarType": "Electric",
        "detectedCars": 0,
        "broken": False,
        "isUnderMaintenance": False
    }
]

result = select_parking_spot(electric_car, electric_spot)

check(
    "Electric car can use Electric spot",
    result["name"] if result else None,
    "EV1"
)


print("\n===== PAYMENT TESTS =====")

entry_time = datetime(2026, 9, 19, 14, 0)
exit_time = datetime(2026, 9, 19, 14, 10)


# TEST 7: Parking duration
minutes = calculate_parking_minutes(entry_time, exit_time)

check(
    "Parking duration is calculated",
    minutes,
    10.0
)


# TEST 8: Normal car payment
charges = calculate_charges("Normal", 10)

check(
    "Normal car charges",
    charges,
    {
        "parkingCost": 10,
        "chargingCost": 0
    }
)


# TEST 9: Electric car payment
charges = calculate_charges("Electric", 10)

check(
    "Electric car charges",
    charges,
    {
        "parkingCost": 10,
        "chargingCost": 20
    }
)


# TEST 10: Cannot charge before exit
result = can_charge(
    at_exit=False,
    already_paid=False
)

check(
    "Cannot charge before reaching exit",
    result,
    False
)


# TEST 11: Cannot charge twice
result = can_charge(
    at_exit=True,
    already_paid=True
)

check(
    "Cannot charge twice",
    result,
    False
)


# TEST 12: Complete valid payment
payment = process_payment(
    car_type="Normal",
    entry_time=entry_time,
    exit_time=exit_time,
    at_exit=True,
    already_paid=False
)

check(
    "Valid payment is processed",
    payment,
    {
        "parkingCost": 10.0,
        "chargingCost": 0
    }
)


# TEST 13: Successful payment allows departure
action = get_post_payment_action(True)

check(
    "Successful payment allows departure",
    action,
    "ALLOW_DEPARTURE"
)


# TEST 14: Failed payment does not allow departure
action = get_post_payment_action(False)

check(
    "Failed payment blocks departure",
    action,
    "PAYMENT_FAILED"
)


print("\n===== TESTING COMPLETE =====")