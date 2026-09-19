from simulator_api import SimulatorClient, SimulatorError

client = SimulatorClient()

try:
    client.login()
    print("TOKEN RECEIVED!")

    spots = client.list_parking_spots()
except SimulatorError as e:
    raise SystemExit(f"Simulator error: {e}")

print("Objects received:", len(spots))

for spot in spots:
    print(
        spot["name"],
        "| purpose:", spot["purpose"],
        "| type:", spot["parkingForCarType"],
        "| zone:", spot["zoneParent"],
        "| cars:", spot["detectedCars"]
    )
