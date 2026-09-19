"""Receiver for simulator webhook events.

Events are passed as raw JSON dicts to registered handlers; no event-specific
parsing happens here. Documented EventClass values: car_spot_action,
gate_action, payment_made, component_broken, component_fixed, penalty,
carbon_monoxide_event.

Two kinds of handler:

  register_handler(fn)                   runs at once, inside the request. Use it
                                         for anything quick (logging, the dashboard).
  register_handler(fn, background=True)  runs on ONE worker thread, in the order
                                         the events arrived. Use it for the car
                                         logic, which talks to the simulator and
                                         database and so can be slow.

Why: run inline, a slow handler makes the simulator wait (and re-send events),
and every request then runs on its own thread, so events get handled out of
order and the car logic falls further and further behind. With the worker, the
webhook always answers straight away and the order is kept.
"""
import logging
import queue
import threading

from flask import Flask, request

logger = logging.getLogger(__name__)

app = Flask(__name__)
_handlers = []              # run inside the request
_background = []            # run on the worker thread, in arrival order
_queue = queue.Queue()
_worker = None
_worker_lock = threading.Lock()


def register_handler(fn=None, background=False):
    """Register fn(event: dict) to be called for every webhook event.

    Can be used as a decorator, with or without background=True.
    Handler exceptions are logged, never raised.
    """
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
            _worker = threading.Thread(target=_work, name="webhook-worker", daemon=True)
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

    return {"status": "received"}, 200


def run(host="0.0.0.0", port=5000):
    app.run(host=host, port=port)
