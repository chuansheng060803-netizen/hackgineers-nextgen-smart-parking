"""Run the simulator ingest: webhook receiver + REST poller -> database.

    python ingest.py                 listens on port 5000, polls the simulator, writes the database
    python ingest.py --no-poll       webhooks only (no REST reads)
    python ingest.py --port 5001

Point the simulator's settings.json at  "WebhookUrl": "http://localhost:5000/webhook".
The database is PARKING_DB_PATH, or database/parking.db.

This program only READS from the simulator. It sends no commands (no gates, no
cars), so it is safe to run next to anything else that does. Look at
http://localhost:5000/api/ingest/health to see that data is flowing.
"""
import argparse
import logging
import signal

import webhook
from poller import Poller, intervals_from_env
from simulator_api import SimulatorClient
from store import Store

logger = logging.getLogger("ingest")

_active = {"store": None, "poller": None}     # what /api/ingest/health reports on


@webhook.app.route("/api/ingest/health")
def ingest_health():
    store, poller = _active["store"], _active["poller"]
    if store is None:
        return {"status": "not started"}, 503
    return {
        "writer_alive": store.is_alive(),   # False = nothing is being saved
        "backlog": store.backlog(),
        "stats": store.stats,
        "poller_reads": poller.reads if poller else None,
        "db": store.db_path,
    }


def build(db_path=None, client=None, poll=True, intervals=None):
    """Wire up store, poller and the webhook handler. Returns (store, poller or None)."""
    store = Store(db_path)
    poller = Poller(client or SimulatorClient(), store, intervals) if poll else None
    webhook.register_handler(store.submit_event)     # only queues: the request answers at once
    _active.update(store=store, poller=poller)
    return store, poller


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--no-poll", action="store_true", help="webhooks only")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    store, poller = build(poll=not args.no_poll, intervals=intervals_from_env())
    store.start()
    # A plain kill (SIGTERM) must also save what is still queued, like Ctrl+C does.
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(SystemExit(0)))
    logger.info("Writing to %s", store.db_path)
    if poller:
        poller.start()
        logger.info("Polling the simulator: %s", poller.intervals)
    logger.info("Webhooks on http://localhost:%s/webhook | health on /api/ingest/health", args.port)
    try:
        webhook.run(port=args.port)
    finally:
        if poller:
            poller.stop()
        store.stop()


if __name__ == "__main__":
    main()
