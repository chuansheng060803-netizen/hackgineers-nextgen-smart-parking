"""Gate control test.

  python test_gates.py               list barriers (read-only)
  python test_gates.py open <name>   open a gate (asks for confirmation)
  python test_gates.py close <name>  close a gate (asks for confirmation)
"""
import json
import sys

from simulator_api import SimulatorClient, SimulatorError

client = SimulatorClient()

try:
    if len(sys.argv) == 1:
        print(json.dumps(client.list_barriers(), indent=2))
    elif len(sys.argv) == 3 and sys.argv[1] in ("open", "close"):
        action, name = sys.argv[1], sys.argv[2]
        if input(f"This will {action.upper()} the REAL gate '{name}'. Type 'yes' to continue: ") != "yes":
            raise SystemExit("Aborted.")
        getattr(client, f"{action}_gate")(name)
        print(f"Gate '{name}' {action} request accepted.")
    else:
        raise SystemExit(__doc__)
except SimulatorError as e:
    raise SystemExit(f"Simulator error: {e}")
