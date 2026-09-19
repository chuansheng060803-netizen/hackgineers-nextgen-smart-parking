"""Car movement test.

  python test_move_car.py <car-name> <destination>

destination: a parking spot name (e.g. S1), "exit" or "leavepark".
Nothing is sent until you type 'yes'.
"""
import sys

from simulator_api import SimulatorClient, SimulatorError

if len(sys.argv) != 3:
    raise SystemExit(__doc__)

name, destination = sys.argv[1], sys.argv[2]

print(f"This will send the REAL car '{name}' to '{destination}'.")
if destination not in ("exit", "leavepark"):
    print("Warning: if that spot is occupied, the simulator may issue a penalty.")
if input("Type 'yes' to continue: ") != "yes":
    raise SystemExit("Aborted.")

try:
    SimulatorClient().move_car(name, destination)
except SimulatorError as e:
    raise SystemExit(f"Simulator error: {e}")

print(f"Move request for '{name}' to '{destination}' accepted.")
