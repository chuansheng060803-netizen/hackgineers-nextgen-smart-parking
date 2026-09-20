def is_parking_space(spot):
    return spot.get("purpose") == "Park"


def is_operational(spot):
    return (
        spot.get("broken") == False
        and spot.get("isUnderMaintenance") == False
    )


def is_available(spot):
    detected_cars = spot.get("detectedCars", [])

    if isinstance(detected_cars, list):
        return len(detected_cars) == 0

    return detected_cars == 0


def get_zone(spot):
    return spot.get("zoneParent")



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
    zones = ["ZONE1", "ZONE2", "ZONE3"]

    for zone in zones:
        zone_spots = get_available_spots_in_zone(
            car,
            parking_spots,
            zone
        )

        if len(zone_spots) > 0:
            return zone_spots[0]

    return None


def get_available_spots_in_zone(car, parking_spots, zone):
    available_spots = get_available_spots(car, parking_spots)

    zone_spots = []

    for spot in available_spots:
        if get_zone(spot) == zone:
            zone_spots.append(spot)

    return zone_spots