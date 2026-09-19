def is_parking_space(spot):
    return spot.get("purpose") == "Park"


def is_operational(spot):
    return (
        spot.get("broken") == False
        and spot.get("isUnderMaintenance") == False
    )


def is_available(spot):
    return spot.get("detectedCars", 0) == 0


def is_compatible(car, spot):
    car_type = car.get("CarType")
    spot_type = spot.get("parkingForCarType")

    if spot_type == "Any":
        return True

    if spot_type == car_type:
        return True

    return False


def get_available_spots(car, parking_spots):
    available_spots = []

    for spot in parking_spots:

        if not is_parking_space(spot):
            continue

        if not is_operational(spot):
            continue

        if not is_available(spot):
            continue

        if not is_compatible(car, spot):
            continue

        available_spots.append(spot)

    return available_spots


def select_parking_spot(car, parking_spots):
    available_spots = get_available_spots(car, parking_spots)

    if len(available_spots) == 0:
        return None

    return available_spots[0]