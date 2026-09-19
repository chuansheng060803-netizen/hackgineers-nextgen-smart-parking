from parking_algorithm import select_parking_spot

car = {
    "CarPlateNumber": "ZGM 824",
    "CarType": "Normal"
}

parking_spots = [
    {
        "name": "S1",
        "purpose": "Park",
        "parkingForCarType": "Any",
        "zoneParent": "ZONE1",
        "detectedCars": 1,
        "broken": False,
        "isUnderMaintenance": False
    },

    {
        "name": "S2",
        "purpose": "Park",
        "parkingForCarType": "Any",
        "zoneParent": "ZONE1",
        "detectedCars": 0,
        "broken": False,
        "isUnderMaintenance": False
    },

    {
        "name": "S3",
        "purpose": "Park",
        "parkingForCarType": "Electric",
        "zoneParent": "ZONE1",
        "detectedCars": 0,
        "broken": False,
        "isUnderMaintenance": False
    },

    {
        "name": "S4",
        "purpose": "Park",
        "parkingForCarType": "Any",
        "zoneParent": "ZONE1",
        "detectedCars": 0,
        "broken": True,
        "isUnderMaintenance": False
    },

    {
        "name": "ENTRY1",
        "purpose": "EntrySpot",
        "parkingForCarType": "Any",
        "zoneParent": "ZONE1",
        "detectedCars": 0,
        "broken": False,
        "isUnderMaintenance": False
    }
]

selected_spot = select_parking_spot(car, parking_spots)

if selected_spot is None:
    print("Parking is full!")
else:
    print("Selected parking spot:", selected_spot["name"])