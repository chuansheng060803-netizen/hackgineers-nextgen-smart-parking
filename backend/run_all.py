"""Run the whole Level 1 backend in ONE process: car logic + the new database ingest.

    python run_all.py            DRY RUN: cars are NOT moved, gates NOT touched, only logged
    python run_all.py --live     really send commands (open gates, move cars, charge them)

What runs together (webhook port 5000, so stop run_flow.py / ingest.py first):

    webhook events ─▶ Store (saved to the database, in the simulator's own names) ─┐
                  └─▶ CarFlow (on its own ordered worker: parks, charges, releases cars)
    REST poller  ───▶ Store (spots, gates, zones, lights, alarms, status)
    CarFlow's bills and accepted payments ─▶ Store (charges table, via StoreAdapter)
    Dashboard operator buttons ─▶ POST /api/control/... ─▶ GateController: a gate held open or
                  closed by an operator ignores the car logic (see gate_control.py, control_api.py)

The store handler runs first and only queues, so an event is saved before the car
logic sees it, and a slow simulator command can never delay the data being saved.
This file does not change car_flow.py.
"""
import argparse
import logging

import control_api
import ingest
import webhook
from car_flow import ENTRY_GATES, EXIT_GATES, CarFlow
from dry_run import DryRunClient
from gate_control import GateController
from poller import intervals_from_env
from simulator_api import SimulatorClient, SimulatorError
from store_adapter import StoreAdapter

logger = logging.getLogger("run_all")

_active = {"flow": None, "live": False, "controller": None}     # what /api/flow/health reports on
webhook.app.register_blueprint(control_api.bp)                     # /api/control/... (see control_api.py)


@webhook.app.route("/api/flow/health")
def flow_health():
    flow = _active["flow"]
    if flow is None:
        return {"status": "not started"}, 503
    return {"backlog": webhook.backlog(), "processed": flow.processed,
            "cars_tracked": len(flow.cars), "live": _active["live"]}


def build(live=False, db_path=None, flow_client=None, poll_client=None, poll=True,
          intervals=None, control_client=None):
    """Wire everything. Returns (store, poller, flow). Nothing is started yet."""
    store, poller = ingest.build(db_path, client=poll_client or SimulatorClient(), poll=poll,
                                 intervals=intervals)
    real = flow_client or SimulatorClient()          # its own client: the car logic and the poller
    client = real if live else DryRunClient(real)    # never share one login/token between threads

    adapter = StoreAdapter(store)                    # bills and accepted payments -> charges table

    # Manual gate control has the last word: every gate command the car logic sends passes
    # through the controller, which drops it while an operator holds that gate open/closed.
    # The controller has its own simulator client for the operator's commands.
    real_control = control_client or SimulatorClient()
    controller = GateController(real_control if live else DryRunClient(real_control), store=store,
                                gates=ENTRY_GATES + EXIT_GATES)
    control_api.configure(controller, store.db_path, control_api.load_token(store.db_path, create=True))

    flow = CarFlow(controller.wrap(client), db=adapter, spots_cache_s=2.0)
    webhook.register_handler(flow.handle_event, background=True)    # after the store handler
    _active.update(flow=flow, live=live, controller=controller)
    return store, poller, flow


def start_gates(flow):
    """Same startup as run_flow.py: exit gate closed, entry gate closed (the entry
    controller then opens gateA only for admitted cars). In a dry run these are only
    logged. Older car_flow versions without initialize_entry_gate are skipped safely."""
    for gate in EXIT_GATES:
        try:
            flow.client.close_gate(gate)
        except SimulatorError as e:
            logger.error("Could not close %s at startup: %s", gate, e)
    init_entry = getattr(flow, "initialize_entry_gate", None)
    if init_entry:
        init_entry()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true", help="really send commands")
    parser.add_argument("--no-poll", action="store_true", help="do not poll the REST lists")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.live:
        logger.warning("LIVE MODE: commands will be sent to the simulator")
    else:
        logger.info("DRY RUN: commands are only logged (use --live to send)")

    store, poller, flow = build(live=args.live, poll=not args.no_poll,
                                intervals=intervals_from_env())
    store.start()
    controller = _active["controller"]
    controller.load(store.db_path)           # gate orders an operator gave before a restart still hold
    if args.live:
        controller.start()                   # keeps a held gate the way the operator left it
    if poller:
        poller.start()
    start_gates(flow)
    logger.info("Database: %s", store.db_path)
    logger.info("Health: /api/ingest/health (data) and /api/flow/health (cars)")
    try:
        webhook.run(port=args.port)
    finally:
        controller.stop()
        if poller:
            poller.stop()
        store.stop()


if __name__ == "__main__":
    main()
