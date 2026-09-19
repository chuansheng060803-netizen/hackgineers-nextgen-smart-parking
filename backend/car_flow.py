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

    Amount is deliberately NOT checked: the simulator's Amount semantics are
    undocumented and no conversion is invented. A payment is therefore accepted
    on the conditions in validate_payment() alone (known car, CHARGING stage,
    not already paid). The requested amounts are stored as car["parking_cost"]
    and car["charging_cost"] so a real Amount rule can be added here later, or
    injected through CarFlow(amount_check=...).
    """
    logger.info("payment_made Amount %s for %s is not validated (semantics undocumented)",
                event.get("Amount"), car["plate"])
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
        self._seen_event_ids = set()
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

    def handle_event(self, event):
        """Webhook handler: register with webhook.register_handler()."""
        with self._lock:
            if not self._accept(event):
                return
            self._db("log_event", event)
            event_class = event.get("EventClass")
            if event_class == "car_spot_action":
                self._on_car_spot_action(event)
            elif event_class == "payment_made":
                self._on_payment_made(event)
            self._retry_pending(skip=event.get("CarPlateNumber"))

    def request_exit(self, plate):
        """Send a parked car to the exit. Only called explicitly: nothing
        triggers this automatically until we know how departures start."""
        with self._lock:
            car = self.cars.get(plate)
            if car is None or car["stage"] != PARKED:
                logger.warning("request_exit(%s) ignored: not parked", plate)
                return False
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

        sequence_id = event.get("SequenceId")
        if isinstance(sequence_id, int):
            last = self._last_sequence_id
            if last is not None and sequence_id != last + 1:
                logger.warning("SequenceId %s after %s (missing or out-of-order event)",
                               sequence_id, last)
            if last is None or sequence_id > last:
                self._last_sequence_id = sequence_id

        return verify_signature(event)

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
        # detectedCars is empty until a car arrives, so skip spots we already assigned.
        reserved = {c["spot"] for c in self.cars.values()
                    if c["spot"] and c["parked_out_time"] is None}
        free = [s for s in spots if s.get("name") not in reserved]
        spot = select_parking_spot({"CarType": car["car_type"]}, free)
        if spot is None:
            logger.info("No free spot for %s; waiting", car["plate"])
            return
        try:
            self.client.move_car(car["plate"], spot["name"])
        except SimulatorError as e:
            logger.error("move_car(%s, %s) failed: %s", car["plate"], spot["name"], e)
            return
        car["spot"] = spot["name"]
        car["stage"] = MOVING
        self._db("assign_spot", car["session_id"], spot["name"])

    def _on_exit_in(self, car, when):
        car["exit_in_time"] = when
        if car["stage"] in (CHARGING, DONE):
            return  # duplicate; never charge twice
        car["stage"] = AT_EXIT
        self._charge(car)

    def _billing_period(self, car):
        # PROVISIONAL (open question 1): which timestamps define the charged
        # minutes is not confirmed. All event times are stored on the car, so
        # changing this is a one-line switch.
        return car["parked_in_time"], car["parked_out_time"]

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
            self._db("mark_payment_paid", car["payment_id"])
        if get_post_payment_action(True) == "ALLOW_DEPARTURE":
            self._leavepark(car)

    def _leavepark(self, car):
        if car["leavepark_sent"]:
            return
        try:
            self.client.move_car(car["plate"], "leavepark")
        except SimulatorError as e:
            logger.error("move_car(%s, leavepark) failed: %s", car["plate"], e)
            return  # paid but not sent, retried on the next event
        car["leavepark_sent"] = True
        car["stage"] = DONE

    # ---- retries ----------------------------------------------------------

    def _retry_pending(self, skip=None):
        for car in list(self.cars.values()):
            if car["plate"] == skip:
                continue
            if car["stage"] == WAITING:
                self._allocate(car)
            elif car["stage"] == AT_EXIT:
                self._charge(car)
            elif car["paid"] and not car["leavepark_sent"]:
                self._leavepark(car)
