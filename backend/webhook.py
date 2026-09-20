"""Receiver for simulator webhook events.

Events are passed as raw JSON dicts to registered handlers; no event-specific
parsing happens here. Documented EventClass values: car_spot_action,
gate_action, payment_made, component_broken, component_fixed, penalty,
carbon_monoxide_event.

Two kinds of handler:

  register_handler(fn)                   runs at once, inside the request. Use it
                                         for quick logging/dashboard work.
  register_handler(fn, background=True)  runs on ONE worker thread, in arrival
                                         order. Use it for car logic.

Important for the simulator exit flow: the webhook must return HTTP 200 promptly.
The simulator emits ExitSpot/CarIn before it has fully changed the car's internal
state to WaitingAtExit. car_flow.py therefore performs the actual charge shortly
AFTER this acknowledgement instead of blocking the webhook response.
"""
import logging
import queue
import threading

from flask import Flask, request

logger = logging.getLogger(__name__)

app = Flask(__name__)
_handlers = []
_background = []
_queue = queue.Queue()
_worker = None
_worker_lock = threading.Lock()


def register_handler(fn=None, background=False):
    """Register fn(event: dict) to be called for every webhook event."""
    def add(f):
        (_background if background else _handlers).append(f)
        return f

    return add(fn) if fn is not None else add


def backlog():
    """How many events are waiting for the background handlers."""
    return _queue.qsize()


def _run(handlers, event):
    for handler in list(handlers):
        try:
            handler(event)
        except Exception:
            logger.exception("Webhook handler %r failed", handler)


def _work():
    while True:
        event = _queue.get()
        try:
            _run(_background, event)
        finally:
            _queue.task_done()


def _ensure_worker():
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(
                target=_work, name="webhook-worker", daemon=True
            )
            _worker.start()


@app.route("/webhook", methods=["POST"])
def webhook():
    event = request.get_json(silent=True)
    if event is None:
        return {"status": "invalid json"}, 400

    _run(_handlers, event)

    if _background:
        _ensure_worker()
        _queue.put(event)

    # Always acknowledge immediately. Do not wait here for charge/open-gate API
    # calls; the simulator needs this response before it finishes the car state
    # transition that makes /charge valid at EXIT_EXIT.
    return {"status": "received"}, 200


def run(host="0.0.0.0", port=5000):
    app.run(host=host, port=port)
