"""Level 1 orchestration: webhook events -> Person 2 logic -> SimulatorClient.

This module only wires things together. Parking selection lives in
parking_algorithm.py, cost rules in payment_logic.py, HTTP in simulator_api.py.
Per-car state is kept in memory; nothing here talks to a database.

Documented car lifecycle:
  ENTRY1 CarIn -> ENTRY1 CarOut -> Park CarIn -> Park CarOut
  -> ExitSpot CarIn -> ExitSpot CarOut
"""
import logging
import threading
import time
from collections import deque
from datetime import datetime

from parking_algorithm import select_parking_spot
from payment_logic import get_post_payment_action, process_payment
from simulator_api import SimulatorError

logger = logging.getLogger(__name__)

WAITING = "WAITING"      # at the entry, no spot assigned yet
MOVING = "MOVING"        # move_car() to a spot has been sent
PARKED = "PARKED"        # reached its spot
LEAVING = "LEAVING"      # move_car(plate, "exit") has been sent
AT_EXIT = "AT_EXIT"      # reached the exit spot, charge not sent yet
CHARGING = "CHARGING"    # charge_car() sent, waiting for a valid payment_made
DONE = "DONE"            # valid payment received and leavepark sent

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# How many EventIds to remember for duplicate detection. Old ids are forgotten
# oldest-first so a long run cannot grow this without bound.
MAX_REMEMBERED_EVENTS = 10_000

# Do not re-read the barriers more often than this (real seconds).
GATE_CHECK_INTERVAL_S = 5.0

# Simulator seconds a car may take to reach the spot we sent it to. After this
# the reservation is given up: see _expire_reservations.
RESERVATION_TIMEOUT_S = 120


def verify_signature(event):
    """True if the event may be processed.

    The docs describe a signature (exclude Signature, sort field names, join
    the values with "|", hash, compare), but the hash algorithm is not
    confirmed, so it is not implemented. A missing/None Signature (which the
    live simulator currently sends) is never a reason to reject an event.
    """
    if event.get("Signature") is not None:
        logger.warning("Signature present but not verified (algorithm unconfirmed): %s",
                       event.get("EventId"))
    return True


def amount_is_valid(event, car):
    """Is payment_made.Amount the amount we expect for this car?

    What Amount represents is still not confirmed, so this deliberately does
    NOT compare it: any payment_made for a car that is waiting to pay counts.
    validate_payment still enforces the checks that hold either way (the car
    is known, it is in CHARGING and has not already paid), so a stray or
    repeated event cannot release a car twice.

    The amounts we asked for stay on the car as car["parking_cost"] and
    car["charging_cost"]; pass a stricter amount_check to CarFlow once the
    meaning of Amount is confirmed.
    """
    return True


def validate_payment(event, car, amount_check=amount_is_valid):
    """Checks that hold regardless of how Amount is defined, then the Amount check."""
    if car is None:
        logger.warning("payment_made for unknown car %s", event.get("CarPlateNumber"))
        return False
    if car["stage"] != CHARGING or car["paid"]:
        logger.warning("payment_made for %s rejected: stage=%s paid=%s",
                       car["plate"], car["stage"], car["paid"])
        return False
    if not verify_signature(event):
        return False
    return amount_check(event, car)


def _parse_time(event):
    try:
        return datetime.strptime(event["ServerDateTime"], TIME_FORMAT)
    except (KeyError, TypeError, ValueError):
        logger.warning("Bad or missing ServerDateTime in event %s", event.get("EventId"))
        return None


class CarFlow:
    def __init__(self, client, amount_check=amount_is_valid, db=None):
        self.client = client
        self.amount_check = amount_check
        self.db = db  # optional DatabaseAdapter; None = no persistence
        self.cars = {}  # plate -> state dict
        self.gates = {}  # name -> last known barrier dict
        self._gates_checked_at = None  # time.monotonic() of the last list_barriers()
        self._sim_now = None  # newest ServerDateTime seen, our clock
        self._seen_event_ids = set()
        self._seen_event_order = deque()  # same ids, oldest first, for eviction
        self._last_sequence_id = None
        self._lock = threading.RLock()

    def _db(self, method, *args):
        """Call a database adapter method; a database failure never breaks the flow."""
        if self.db is None:
            return None
        try:
            return getattr(self.db, method)(*args)
        except Exception:
            logger.exception("Database %s failed", method)
            return None

    # ---- public API -------------------------------------------------------

    def retry_pending(self):
        """Retry failed commands even when no new webhook arrives.

        Keep the simulator clock event-driven: wall time must not expire
        reservations while the simulator is paused.
        """
        with self._lock:
            self._retry_pending()

    def handle_event(self, event):
        """Webhook handler: register with webhook.register_handler()."""
        with self._lock:
            if not self._accept(event):
                return
            when = _parse_time(event)
            if when and (self._sim_now is None or when > self._sim_now):
                self._sim_now = when
            self._db("log_event", event)
            event_class = event.get("EventClass")
            if event_class == "car_spot_action":
                self._on_car_spot_action(event)
            elif event_class == "payment_made":
                self._on_payment_made(event)
            elif event_class == "gate_action":
                self._on_gate_action(event)
            self._retry_pending(skip=event.get("CarPlateNumber"))

    def request_exit(self, plate):
        """Send a parked car to the exit. Only called explicitly: nothing
        triggers this automatically until we know how departures start."""
        with self._lock:
            car = self.cars.get(plate)
            if car is None or car["stage"] != PARKED:
                logger.warning("request_exit(%s) ignored: not parked", plate)
                return False
            self._ensure_gates_open()
            try:
                self.client.move_car(plate, "exit")
            except SimulatorError as e:
                logger.error("move_car(%s, exit) failed: %s", plate, e)
                return False
            car["stage"] = LEAVING
            return True

    # ---- event gate -------------------------------------------------------

    def _accept(self, event):
        event_id = event.get("EventId")
        if event_id is not None:
            if event_id in self._seen_event_ids:
                logger.info("Duplicate event %s ignored", event_id)
                return False
            self._seen_event_ids.add(event_id)
            self._seen_event_order.append(event_id)
            if len(self._seen_event_order) > MAX_REMEMBERED_EVENTS:
                self._seen_event_ids.discard(self._seen_event_order.popleft())

        sequence_id = event.get("SequenceId")
        if isinstance(sequence_id, int):
            last = self._last_sequence_id
            if last is not None and sequence_id != last + 1:
                logger.warning("SequenceId %s after %s (missing or out-of-order event)",
                               sequence_id, last)
            if last is None or sequence_id > last:
                self._last_sequence_id = sequence_id

        return verify_signature(event)

    # ---- barrier gates ----------------------------------------------------

    def _ensure_gates_open(self, force=False):
        """Open every healthy barrier that is not open yet.

        A closed barrier silently blocks a car: move_car is still accepted (201)
        and no error comes back, the car simply never arrives and no further
        event is ever sent for it. Nothing else in the system opens barriers, so
        the flow has to.

        A broken or under-maintenance gate is never operated: the organisers
        charge a penalty for that. Failures here are logged, never raised.
        """
        now = time.monotonic()
        if (not force and self._gates_checked_at is not None
                and now - self._gates_checked_at < GATE_CHECK_INTERVAL_S):
            return
        self._gates_checked_at = now
        try:
            barriers = self.client.list_barriers()
        except SimulatorError as e:
            logger.error("list_barriers failed: %s", e)
            return
        self._db("sync_gates", barriers)
        for gate in barriers or []:
            if not isinstance(gate, dict) or not gate.get("name"):
                continue
            name = gate["name"]
            self.gates[name] = gate
            if gate.get("broken") or gate.get("isUnderMaintenance"):
                logger.warning("Gate %s is %s; not operating it", name,
                               "broken" if gate.get("broken") else "under maintenance")
                continue
            if gate.get("state") == "Open":
                continue
            try:
                self.client.open_gate(name)
            except SimulatorError as e:
                logger.error("open_gate(%s) failed: %s", name, e)
            else:
                logger.info("Opened gate %s (was %s)", name, gate.get("state"))

    def _on_gate_action(self, event):
        """Keep our view of the barriers current. The payload shape is unconfirmed,
        so the field names are read defensively and a fresh read is forced next."""
        name = event.get("ComponentName") or event.get("SpotName") or event.get("Name")
        if name:
            gate = dict(self.gates.get(name) or {"name": name})
            state = event.get("State") or event.get("Direction")
            if state:
                gate["state"] = state
            self.gates[name] = gate
            self._db("sync_gates", [gate])
        self._gates_checked_at = None  # re-read the real state at the next chance

    # ---- car_spot_action --------------------------------------------------

    def _on_car_spot_action(self, event):
        plate = event.get("CarPlateNumber")
        spot_type = event.get("SpotType")
        spot_name = event.get("SpotName")
        direction = event.get("Direction")
        when = _parse_time(event)

        if spot_type == "EntrySpot":
            if direction == "CarIn":
                self._on_entry_in(plate, event, when)
            return

        car = self.cars.get(plate)
        if car is None:
            logger.warning("%s event for unknown car %s", spot_name, plate)
            return

        if spot_type == "ExitSpot":
            if direction == "CarIn":
                self._on_exit_in(car, when)
            elif direction == "CarOut":
                logger.info("%s left through %s", plate, spot_name)
                self._db("complete_session", car["session_id"], when)
                del self.cars[plate]
            return

        # Anything else is a parking spot: only our reserved spot matters.
        if spot_name != car["spot"]:
            logger.warning("%s event for %s, but %s was assigned %s",
                           direction, spot_name, plate, car["spot"])
            return
        if direction == "CarIn":
            car["parked_in_time"] = when
            if car["stage"] in (MOVING, WAITING):
                car["stage"] = PARKED
            self._db("mark_parked", car["session_id"], when)
            self._db("set_spot_occupied", spot_name, plate)
        elif direction == "CarOut":
            car["parked_out_time"] = when  # also releases the reservation
            self._db("set_spot_available", spot_name)

    def _on_entry_in(self, plate, event, when):
        if plate in self.cars:
            logger.warning("%s arrived at the entry but is already tracked", plate)
            return
        car = {
            "plate": plate,
            "car_type": event.get("CarType"),
            "planned_minutes": event.get("PlannedParkingDurationInMinutes"),
            "stage": WAITING,
            "spot": None,
            "entry_in_time": when,
            "parked_in_time": None,
            "parked_out_time": None,
            "exit_in_time": None,
            "parking_cost": None,   # requested amounts, stored separately
            "charging_cost": None,
            "paid": False,
            "leavepark_sent": False,
            "move_sent_time": None,  # when we told it to go to its spot
            "session_id": None,     # database session
            "payment_id": None,     # database payment row
        }
        self.cars[plate] = car
        car["session_id"] = self._db("start_session", plate, car["car_type"], when)
        self._allocate(car)

    def _allocate(self, car):
        try:
            spots = self.client.list_parking_spots()
        except SimulatorError as e:
            logger.error("list_parking_spots failed: %s", e)
            return
        # Store any spot the database has not seen yet. This is what makes the
        # dashboard survive a failed startup sync (the simulator being briefly
        # unreachable): the spots arrive with the first car instead of never.
        self._db("sync_new_spots", spots)
        # detectedCars is empty until a car arrives, so skip spots we already assigned.
        reserved = {c["spot"] for c in self.cars.values()
                    if c["spot"] and c["parked_out_time"] is None}
        free = [s for s in spots if s.get("name") not in reserved]
        spot = select_parking_spot({"CarType": car["car_type"]}, free)
        if spot is None:
            logger.info("No free spot for %s; waiting", car["plate"])
            return
        # Clear the way first: a closed barrier makes move_car a silent no-op.
        self._ensure_gates_open()
        try:
            self.client.move_car(car["plate"], spot["name"])
        except SimulatorError as e:
            logger.error("move_car(%s, %s) failed: %s", car["plate"], spot["name"], e)
            return
        car["spot"] = spot["name"]
        car["stage"] = MOVING
        car["move_sent_time"] = self._sim_now
        self._db("assign_spot", car["session_id"], spot["name"])

    def _on_exit_in(self, car, when):
        car["exit_in_time"] = when
        if car["stage"] in (CHARGING, DONE):
            return  # duplicate; never charge twice
        # A car at the exit is not sitting in a parking spot any more, even if we
        # never saw it park (a drive-through, or a missed Park CarOut event).
        # Releasing the reservation here stops it holding a spot it never used.
        if car["parked_out_time"] is None:
            car["parked_out_time"] = when
            if car["spot"]:
                self._db("set_spot_available", car["spot"])
        car["stage"] = AT_EXIT
        self._charge(car)

    def _billing_period(self, car):
        # PROVISIONAL (open question 1): which timestamps define the charged
        # minutes is not confirmed. All event times are stored on the car, so
        # changing this is a one-line switch.
        # A car that never parked (drove straight through) has no parked_in_time,
        # so fall back to the time it entered: without this it can never be
        # charged, stays AT_EXIT and is retried on every later event.
        start = car["parked_in_time"] or car["entry_in_time"]
        end = car["parked_out_time"] or car["exit_in_time"]
        return start, end

    def _charge(self, car):
        start, end = self._billing_period(car)
        if start is None or end is None:
            logger.warning("Cannot charge %s: missing timestamps", car["plate"])
            return
        charges = process_payment(
            car["car_type"], start, end, at_exit=True, already_paid=car["paid"]
        )
        if charges is None:
            return
        try:
            self.client.charge_car(
                car["plate"], charges["parkingCost"], charges["chargingCost"]
            )
        except SimulatorError as e:
            logger.error("charge_car(%s) failed: %s", car["plate"], e)
            return  # stays AT_EXIT, retried on the next event
        car["parking_cost"] = charges["parkingCost"]
        car["charging_cost"] = charges["chargingCost"]
        car["stage"] = CHARGING
        car["payment_id"] = self._db(
            "record_pending_charge",
            car["session_id"], charges["parkingCost"], charges["chargingCost"],
        )

    # ---- payment_made -----------------------------------------------------

    def _on_payment_made(self, event):
        car = self.cars.get(event.get("CarPlateNumber"))
        if not validate_payment(event, car, self.amount_check):
            logger.warning("Payment rejected (%s): %s",
                           get_post_payment_action(False), event)
            return
        car["paid"] = True
        if car["payment_id"] is not None:
            # Simulator time, so paid_at matches the session timestamps.
            self._db("mark_payment_paid", car["payment_id"], _parse_time(event))
        if get_post_payment_action(True) == "ALLOW_DEPARTURE":
            self._leavepark(car)

    def _leavepark(self, car):
        if car["leavepark_sent"]:
            return
        self._ensure_gates_open()
        try:
            self.client.move_car(car["plate"], "leavepark")
        except SimulatorError as e:
            logger.error("move_car(%s, leavepark) failed: %s", car["plate"], e)
            return  # paid but not sent, retried on the next event
        car["leavepark_sent"] = True
        car["stage"] = DONE

    # ---- retries ----------------------------------------------------------

    def _expire_reservations(self):
        """Give up a spot a car was sent to but never reached.

        A car that never arrives used to hold its spot for ever. Once enough of
        them pile up every spot looks taken, select_parking_spot returns None
        and no new car is ever given one: the flow wedges with an empty car park.
        Releasing the spot puts the car back in WAITING, so the next retry sends
        move_car again (by then the barriers have been opened).
        """
        now = self._sim_now
        if now is None:
            return
        for car in self.cars.values():
            if car["stage"] != MOVING or not car["spot"]:
                continue
            sent = car["move_sent_time"]
            if sent is None or (now - sent).total_seconds() < RESERVATION_TIMEOUT_S:
                continue
            logger.warning("%s never reached %s after %.0fs; releasing it and retrying",
                           car["plate"], car["spot"], (now - sent).total_seconds())
            self._db("set_spot_available", car["spot"])
            car["spot"] = None
            car["stage"] = WAITING
            car["move_sent_time"] = None

    def _retry_pending(self, skip=None):
        self._expire_reservations()
        if any(c["stage"] in (MOVING, LEAVING) for c in self.cars.values()):
            # Someone is in transit: make sure a barrier has not closed on them.
            self._ensure_gates_open()
        for car in list(self.cars.values()):
            if car["plate"] == skip:
                continue
            if car["stage"] == WAITING:
                self._allocate(car)
            elif car["stage"] == AT_EXIT:
                self._charge(car)
            elif car["paid"] and not car["leavepark_sent"]:
                self._leavepark(car)
