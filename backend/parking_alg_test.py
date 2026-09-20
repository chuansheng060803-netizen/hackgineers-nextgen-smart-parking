from parking_algorithm import select_parking_spot


car = {
    "CarPlateNumber": "ABC123",
    "CarType": "Normal"
}


parking_spots = [
    {
        "name": "S1",
        "purpose": "Park",
        "parkingForCarType": "Any",
        "zoneParent": "ZONE1",
        "detectedCars": ["CAR111"],
        "broken": False,
        "isUnderMaintenance": False
    },

    {
        "name": "S2",
        "purpose": "Park",
        "parkingForCarType": "Any",
        "zoneParent": "ZONE2",
        "detectedCars": [],
        "broken": False,
        "isUnderMaintenance": False
    },

    {
        "name": "S3",
        "purpose": "Park",
        "parkingForCarType": "Any",
        "zoneParent": "ZONE3",
        "detectedCars": [],
        "broken": False,
        "isUnderMaintenance": False
    }
]


selected = select_parking_spot(car, parking_spots)

print(selected)