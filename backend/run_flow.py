"""Run the Level 1 flow: webhook listener + CarFlow.

  python run_flow.py          DRY RUN: reads spots, only LOGS move_car/charge_car
  python run_flow.py --live   really sends commands to the simulator
"""
import argparse
import logging
from datetime import datetime

import dashboard_api
import webhook
from car_flow import CarFlow
from simulator_api import SimulatorClient
from timing import CallStats, Timed

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

    def list_barriers(self):
        return self._real.list_barriers()

    def open_gate(self, name):
        logger.info("[DRY RUN] would open_gate(%r)", name)

    def close_gate(self, name):
        logger.info("[DRY RUN] would close_gate(%r)", name)


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

    # Every simulator and database call is timed; see /api/health ("timing").
    stats = CallStats()
    real_client = Timed(SimulatorClient(), "simulator", stats)
    client = real_client
    if args.live:
        logger.warning("LIVE MODE: commands will be sent to the simulator")
    else:
        logger.info("DRY RUN: commands are only logged (use --live to send)")
        client = DryRunClient(real_client)

    db = None
    if args.db:
        from database_adapter import DatabaseAdapter

        db = Timed(DatabaseAdapter(), "db", stats)
        db.initialize()
        try:
            db.sync(client)  # read-only: list_parking_spots()
        except Exception:
            logger.exception("Initial database sync failed; continuing")

    flow = CarFlow(client, db=db, spots_cache_s=2.0)

    # Dashboard: serves /api/snapshot, /api/history and /api/control on this same
    # port, so `streamlit run dashboard/app.py` with DASHBOARD_SOURCE=api shows
    # the live car park. Reads go straight to the simulator; the only commands it
    # can send are the operator's gate buttons, and only with --live.
    dashboard = dashboard_api.DashboardState(
        flow, real_client, use_db=args.db, allow_commands=args.live
    )
    dashboard.extra_health = lambda: {
        "backlog": webhook.backlog(),          # events waiting for the car logic
        "flow_processed": flow.processed,
        "timing": stats.summary(),             # recent simulator / database call times
    }
    webhook.register_handler(dashboard.on_event)
    webhook.app.register_blueprint(dashboard_api.build_blueprint(dashboard))
    logger.info("Dashboard API on http://localhost:%s/api/snapshot", args.port)

    webhook.register_handler(log_event)
    # The car logic runs on the ordered worker, so the webhook answers at once.
    webhook.register_handler(flow.handle_event, background=True)
    webhook.run(port=args.port)
