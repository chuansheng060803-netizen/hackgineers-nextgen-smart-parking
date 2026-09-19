"""Run the Level 1 flow: webhook listener + CarFlow.

  python run_flow.py          DRY RUN: reads spots, only LOGS move_car/charge_car
  python run_flow.py --live   really sends commands to the simulator
"""
import argparse
import logging
from datetime import datetime

import webhook
from car_flow import CarFlow
from simulator_api import SimulatorClient

logger = logging.getLogger("run_flow")


class DryRunClient:
    """Real read-only calls; commands are logged instead of sent."""

    def __init__(self, real):
        self._real = real

    def list_parking_spots(self):
        return self._real.list_parking_spots()

    def move_car(self, name, destination):
        logger.info("[DRY RUN] would move_car(%r, %r)", name, destination)

    def charge_car(self, name, parking_cost, charging_cost):
        logger.info("[DRY RUN] would charge_car(%r, %r, %r)", name, parking_cost, charging_cost)


def log_event(event):
    logger.info("EVENT %s | server time %s | local time %s",
                event, event.get("ServerDateTime"), datetime.now().strftime("%Y-%m-%d %H:%M:%S"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true", help="really send commands")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--db", action="store_true",
                        help="record sessions, payments and events in the SQLite database")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    client = SimulatorClient()
    if args.live:
        logger.warning("LIVE MODE: commands will be sent to the simulator")
    else:
        logger.info("DRY RUN: commands are only logged (use --live to send)")
        client = DryRunClient(client)

    db = None
    if args.db:
        from database_adapter import DatabaseAdapter

        db = DatabaseAdapter()
        db.initialize()
        try:
            db.sync(client)  # read-only: list_parking_spots()
        except Exception:
            logger.exception("Initial database sync failed; continuing")

    flow = CarFlow(client, db=db)
    webhook.register_handler(log_event)
    webhook.register_handler(flow.handle_event)
    webhook.run(port=args.port)
