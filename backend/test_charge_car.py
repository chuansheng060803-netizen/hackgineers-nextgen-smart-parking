"""Car charge test.

  python test_charge_car.py <car-name> <parking-cost> <charging-cost>

Nothing is sent until you type 'yes'.
"""
import sys

from simulator_api import SimulatorClient, SimulatorError

if len(sys.argv) != 4:
    raise SystemExit(__doc__)

name = sys.argv[1]
try:
    parking_cost = float(sys.argv[2])
    charging_cost = float(sys.argv[3])
except ValueError:
    raise SystemExit("parking-cost and charging-cost must be numbers.")

print(f"This will CHARGE the REAL car '{name}':")
print(f"  parkingCost:  {parking_cost}")
print(f"  chargingCost: {charging_cost}")
if input("Type 'yes' to continue: ") != "yes":
    raise SystemExit("Aborted.")

try:
    SimulatorClient().charge_car(name, parking_cost, charging_cost)
except SimulatorError as e:
    raise SystemExit(f"Simulator error: {e}")

print(f"Charge request for '{name}' accepted.")
