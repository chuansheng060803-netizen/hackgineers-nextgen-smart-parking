from simulator_api import SimulatorClient

client = SimulatorClient()

print("\n=== BARRIERS ===")
barriers = client.list_barriers()
print(barriers)

print("\n=== PARKING SPOTS ===")
spots = client.list_parking_spots()
print(spots)