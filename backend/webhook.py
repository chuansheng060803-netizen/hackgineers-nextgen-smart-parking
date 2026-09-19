"""Receiver for simulator webhook events.

Events are passed as raw JSON dicts to registered handlers; no event-specific
parsing happens here. Documented EventClass values: car_spot_action,
gate_action, payment_made, component_broken, component_fixed, penalty,
carbon_monoxide_event.
"""
import logging

from flask import Flask, request

logger = logging.getLogger(__name__)

app = Flask(__name__)
_handlers = []


def register_handler(fn):
    """Register fn(event: dict) to be called for every webhook event.

    Can be used as a decorator. Handler exceptions are logged, never raised.
    """
    _handlers.append(fn)
    return fn


@app.route("/webhook", methods=["POST"])
def webhook():
    event = request.get_json(silent=True)
    if event is None:
        return {"status": "invalid json"}, 400

    for handler in _handlers:
        try:
            handler(event)
        except Exception:
            logger.exception("Webhook handler %r failed", handler)

    return {"status": "received"}, 200


def run(host="0.0.0.0", port=5000):
    app.run(host=host, port=port)
