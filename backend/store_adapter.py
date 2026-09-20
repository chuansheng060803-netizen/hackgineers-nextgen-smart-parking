"""The car logic's database hook, backed by the new single-writer Store.

CarFlow reports what it decides through `db` (start_session, record_pending_charge,
mark_payment_paid, ...). The older DatabaseAdapter wrote those into the old tables.
This one writes the part that matters for the dashboard into the NEW database, through
the same single writer as all simulator data:

  * every bill sent to the simulator (charge_car)          -> charges, status 'billed'
  * the payment the car logic checked and accepted         -> the same row, status 'paid'

Everything else CarFlow reports is already in the database as simulator data (the
webhook events and the polled lists), so those calls are accepted and ignored here.
A payment that CarFlow REJECTS (a fake one) is never reported, so its bill stays
'billed' and never counts as income.

Nothing here can break the car logic: CarFlow already survives a failing hook, and
Store.submit_record only queues.
"""
import itertools
import logging
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"     # same text format as the simulator's ServerDateTime


class StoreAdapter:
    def __init__(self, store, clock=datetime.now):
        self.store = store
        self._clock = clock
        self._lock = threading.Lock()
        self._sessions = {}                               # session_id -> {"plate", "car_type"}
        self._charges = {}                                # charge_id  -> the row as last written
        self._session_ids = itertools.count(1)
        # milliseconds since 1970 as the start: ids stay unique across restarts
        self._charge_ids = itertools.count(int(time.time() * 1000))

    def _now(self):
        return self._clock().strftime(TIME_FORMAT)

    # ---- what we keep ----------------------------------------------------

    def start_session(self, plate, car_type, *_ignored):
        # CarFlow calls start_session(plate, car_type, entry_spot, when); only plate and type are kept here.
        with self._lock:
            session_id = next(self._session_ids)
            self._sessions[session_id] = {"plate": plate, "car_type": car_type}
        return session_id

    def record_pending_charge(self, session_id, parking_cost, charging_cost):
        with self._lock:
            session = self._sessions.get(session_id, {})
            charge_id = next(self._charge_ids)
            parking = round(float(parking_cost or 0), 4)
            charging = round(float(charging_cost or 0), 4)
            row = {
                "charge_id": charge_id,
                "CarPlateNumber": session.get("plate") or "unknown",
                "CarType": session.get("car_type"),
                "billed_parking": parking,
                "billed_charging": charging,
                "billed_total": round(parking + charging, 4),
                "billed_at": self._now(),
                "status": "billed",
                "paid_at": None,
            }
            self._charges[charge_id] = row
        self.store.submit_record("charges", row)
        return charge_id

    def mark_payment_paid(self, charge_id, *_when):
        with self._lock:
            row = self._charges.get(charge_id)
            if row is None:
                logger.warning("mark_payment_paid: unknown charge %r", charge_id)
                return
            row = dict(row, status="paid", paid_at=self._now())
            self._charges[charge_id] = row
        self.store.submit_record("charges", row)

    def complete_session(self, session_id, *_ignored):
        with self._lock:
            self._sessions.pop(session_id, None)          # the visit is over; forget it

    # ---- already in the database as simulator data: nothing to do --------
    # CarFlow reports these too (its own, older database keeps them in its tables). The
    # signatures differ between car_flow versions, so they accept anything.

    def log_event(self, *_args):
        pass

    def start_parking(self, *_args):
        pass

    def end_parking(self, *_args):
        pass

    def complete_payment(self, *_args):
        pass

    def update_gate_status(self, *_args):
        pass

    def assign_spot(self, *_args):
        pass

    def mark_parked(self, *_args):
        pass

    def set_spot_occupied(self, *_args):
        pass

    def set_spot_available(self, *_args):
        pass
