"""Run the Level 2 flow: webhook listener + CarFlow.

  python run_flow.py          DRY RUN: reads spots, only LOGS commands
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


# ----------------------------------------------------------------------
# DRY RUN CLIENT
# ----------------------------------------------------------------------

class DryRunClient:
    """Real read-only calls; commands are logged instead of sent."""

    def __init__(self, real):
        self._real = real

    def list_parking_spots(self):
        return self._real.list_parking_spots()

    def move_car(self, name, destination):
        logger.info(
            "[DRY RUN] would move_car(%r, %r)",
            name,
            destination
        )

    def charge_car(self, name, parking_cost, charging_cost):
        logger.info(
            "[DRY RUN] would charge_car(%r, %r, %r)",
            name,
            parking_cost,
            charging_cost
        )

    def list_barriers(self):
        return self._real.list_barriers()

    def open_gate(self, name):
        logger.info(
            "[DRY RUN] would open_gate(%r)",
            name
        )

    def close_gate(self, name):
        logger.info(
            "[DRY RUN] would close_gate(%r)",
            name
        )


# ----------------------------------------------------------------------
# EVENT LOGGER
# ----------------------------------------------------------------------

def log_event(event):
    logger.info(
        "EVENT %s | server time %s | local time %s",
        event,
        event.get("ServerDateTime"),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )


# ----------------------------------------------------------------------
# MAIN
# ----------------------------------------------------------------------

if __name__ == "__main__":

    # ------------------------------------------------------------------
    # COMMAND LINE ARGUMENTS
    # ------------------------------------------------------------------

    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0]
    )

    parser.add_argument(
        "--live",
        action="store_true",
        help="really send commands"
    )

    parser.add_argument(
        "--port",
        type=int,
        default=5000
    )

    parser.add_argument(
        "--db",
        action="store_true",
        help="record sessions, payments and events in the SQLite database"
    )

    args = parser.parse_args()


    # ------------------------------------------------------------------
    # LOGGING
    # ------------------------------------------------------------------

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s"
    )


    # ------------------------------------------------------------------
    # SIMULATOR CLIENT
    # ------------------------------------------------------------------

    # Every simulator and database call is timed.
    # Timing information can be viewed from /api/health.
    stats = CallStats()

    real_client = Timed(
        SimulatorClient(),
        "simulator",
        stats
    )

    client = real_client


    # ------------------------------------------------------------------
    # LIVE / DRY RUN MODE
    # ------------------------------------------------------------------

    if args.live:
        logger.warning(
            "LIVE MODE: commands will be sent to the simulator"
        )

    else:
        logger.info(
            "DRY RUN: commands are only logged "
            "(use --live to send)"
        )

        client = DryRunClient(real_client)


    # ------------------------------------------------------------------
    # DATABASE
    # ------------------------------------------------------------------

    db = None

    if args.db:

        from database_adapter import DatabaseAdapter

        db = Timed(
            DatabaseAdapter(),
            "db",
            stats
        )

        db.initialize()

        try:
            # Initial read-only synchronization
            db.sync(client)

        except Exception:
            logger.exception(
                "Initial database sync failed; continuing"
            )


    # ------------------------------------------------------------------
    # CAR FLOW
    # ------------------------------------------------------------------

    flow = CarFlow(
        client,
        db=db,
        spots_cache_s=2.0
    )


    # ------------------------------------------------------------------
    # INITIALIZE GATES
    # ------------------------------------------------------------------

    # Level 2 has multiple gates.
    #
    # When the backend starts, put all known parking gates
    # into their normal CLOSED state.
    #
    # CarFlow.initialize_gates() handles:
    #
    # gate1
    # gate2
    # gate3
    # gate4
    # gate5
    # gate6
    #
    # gate7 is currently left alone until its purpose is confirmed.

    flow.initialize_gates()


    # ------------------------------------------------------------------
    # DASHBOARD
    # ------------------------------------------------------------------

    # Dashboard serves:
    #
    # /api/snapshot
    # /api/history
    # /api/control
    #
    # on the same Flask server.

    dashboard = dashboard_api.DashboardState(
        flow,
        real_client,
        use_db=args.db,
        allow_commands=args.live
    )


    # ------------------------------------------------------------------
    # DASHBOARD HEALTH INFORMATION
    # ------------------------------------------------------------------

    dashboard.extra_health = lambda: {

        # Events waiting to be processed
        "backlog": webhook.backlog(),

        # Number of events processed by CarFlow
        "flow_processed": flow.processed,

        # Simulator / database call timings
        "timing": stats.summary(),
    }


    # ------------------------------------------------------------------
    # REGISTER DASHBOARD EVENT HANDLER
    # ------------------------------------------------------------------

    webhook.register_handler(
        dashboard.on_event
    )


    # ------------------------------------------------------------------
    # REGISTER DASHBOARD API
    # ------------------------------------------------------------------

    webhook.app.register_blueprint(
        dashboard_api.build_blueprint(
            dashboard
        )
    )

    logger.info(
        "Dashboard API on http://localhost:%s/api/snapshot",
        args.port
    )


    # ------------------------------------------------------------------
    # REGISTER EVENT LOGGER
    # ------------------------------------------------------------------

    webhook.register_handler(
        log_event
    )


    # ------------------------------------------------------------------
    # REGISTER CAR FLOW
    # ------------------------------------------------------------------

    # CarFlow runs using the ordered background worker.
    #
    # This means the webhook can respond immediately while
    # the parking logic processes events in order.

    webhook.register_handler(
        flow.handle_event,
        background=True
    )


    # ------------------------------------------------------------------
    # START WEBHOOK SERVER
    # ------------------------------------------------------------------

    webhook.run(
        port=args.port
    )