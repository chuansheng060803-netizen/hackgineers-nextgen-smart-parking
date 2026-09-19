"""Level 1 orchestration: webhook events -> Person 2 logic -> SimulatorClient.

This module only wires things together. Parking selection lives in
parking_algorithm.py, cost rules in payment_logic.py, HTTP in simulator_api.py.
Per-car state is kept in memory; nothing here talks to a database.

Documented car lifecycle:
  ENTRY1 CarIn -> ENTRY1 CarOut -> Park CarIn -> Park CarOut
  -> ExitSpot CarIn -> ExitSpot CarOut
"""
import logging
import os
import threading
import time
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

# Barrier gates a car has to pass. The simulator gives no coordinates, so which
# gate guards which way is configuration, not something we can work out at run
# time. Level 1: gateA sits just past ENTRY1, gateB just before EXIT_EXIT.
ENTRY_GATES = [g.strip() for g in os.getenv("ENTRY_GATES", "gateA").split(",") if g.strip()]
EXIT_GATES = [g.strip() for g in os.getenv("EXIT_GATES", "gateB").split(",") if g.strip()]
GATE_HEALTH_CACHE_S = 3.0    # don't re-read barrier health for every single car
ENTRY_MOVE_RETRY_S = 2.5       # one recovery retry if ENTRY1 CarOut never arrives
ENTRY_MOVE_MAX_RETRIES = 1


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

    Confirmed against the live simulator: Amount is a STRING holding the total
    it charged, which is what charge_car() asked for. For example a car billed
    parkingCost=1.0233, chargingCost=0 pays back Amount='1.02', so the amount is
    compared to the sum of the two requested costs, allowing for the rounding to
    two decimals. Anything else is refused, and leavepark is not sent.
    """
    expected = (car.get("parking_cost") or 0) + (car.get("charging_cost") or 0)
    raw = event.get("Amount")
    try:
        paid = float(raw)
    except (TypeError, ValueError):
        logger.warning("payment_made for %s has no usable Amount: %r", car["plate"], raw)
        return False
    if abs(paid - round(expected, 2)) <= 0.011:
        return True
    logger.warning("payment_made for %s: paid %.2f but we billed %.2f",
                   car["plate"], paid, expected)
    return False


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
    def __init__(self, client, amount_check=amount_is_valid, db=None, spots_cache_s=0.0):
        self.client = client
        self.spots_cache_s = spots_cache_s   # 0 = ask the simulator every time
        self._spots = None
        self._spots_at = None
        self.processed = 0                   # events handled so far (for the backlog gauge)
        self.amount_check = amount_check
        self.db = db  # optional DatabaseAdapter; None = no persistence
        self.cars = {}  # plate -> state dict
        self._seen_event_ids = set()
        self._last_sequence_id = None
        self._lock = threading.RLock()
        self._barriers = []          # cached barrier health, see _gate_is_ok
        self._barriers_at = None

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
            self.processed += 1
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

    # ---- barrier gates ----------------------------------------------------

    def _gate_is_ok(self, name):
        """True only if this barrier is known to be healthy.

        The organisers' rule is never to operate a broken or under-maintenance
        component, so an unknown health counts as "do not touch".
        """
        now = time.monotonic()
        if self._barriers_at is None or now - self._barriers_at > GATE_HEALTH_CACHE_S:
            try:
                self._barriers = self.client.list_barriers() or []
                self._barriers_at = now
            except (SimulatorError, AttributeError) as e:
                logger.warning("list_barriers failed, not touching gates: %s", e)
                return False
        for barrier in self._barriers:
            if isinstance(barrier, dict) and barrier.get("name") == name:
                return not barrier.get("broken") and not barrier.get("isUnderMaintenance")
        logger.warning("Gate %r is not in the simulator's barrier list", name)
        return False

    def _open_gates(self, names, why):
        """Raise healthy barriers.

        Returns True when at least one requested gate was opened and none failed,
        False when an actual open command failed, and None when health was unknown
        so no gate command was attempted.  The None case preserves the existing
        dry-run/test behaviour while live Level 1 has a real barrier list.
        """
        attempted = False
        failed = False
        for name in names:
            if not self._gate_is_ok(name):
                continue
            attempted = True
            try:
                self.client.open_gate(name)
                logger.info("Opened %s (%s)", name, why)
            except (SimulatorError, AttributeError) as e:
                failed = True
                logger.error("open_gate(%s) failed: %s", name, e)
        if failed:
            return False
        if attempted:
            return True
        return None

    def _close_gates(self, names, why):
        """Close healthy barriers without letting a gate failure break car flow."""
        for name in names:
            if not self._gate_is_ok(name):
                continue
            try:
                self.client.close_gate(name)
                logger.info("Closed %s (%s)", name, why)
            except (SimulatorError, AttributeError) as e:
                logger.error("close_gate(%s) failed: %s", name, e)

    def _wait_for_gate_state(self, name, expected, timeout=2.0, poll=0.05):
        """Wait briefly for the simulator to report a barrier state.

        The live simulator acknowledges open_gate before the physical barrier has
        finished opening. Sending goto in that small window can leave the first
        car idle at ENTRY1. Polling the read-only barrier list avoids guessing a
        fixed sleep and only delays while the barrier is actually moving.
        """
        deadline = time.monotonic() + timeout
        expected = expected.lower()
        while time.monotonic() < deadline:
            try:
                barriers = self.client.list_barriers() or []
            except (SimulatorError, AttributeError) as e:
                logger.warning("Cannot confirm %s state for %s: %s", name, expected, e)
                return False
            now = time.monotonic()
            self._barriers = barriers
            self._barriers_at = now
            for barrier in barriers:
                if not isinstance(barrier, dict) or barrier.get("name") != name:
                    continue
                state = str(barrier.get("state", "")).lower()
                if state == expected:
                    return True
                break
            time.sleep(poll)
        logger.warning("Timed out waiting for %s to become %s", name, expected)
        return False

    def initialize_entry_gate(self):
        """Put the Level 1 entry barrier in its normal CLOSED state at startup."""
        self._close_gates(ENTRY_GATES, "entry controller startup")


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
            elif direction == "CarOut":
                self._on_entry_out(plate)
            return

        # Any parking-bay occupancy change makes the cached simulator snapshot
        # stale immediately.  Invalidate even for an unknown car so a restart or
        # reordered event cannot leave us assigning from an outdated spot list.
        if spot_type == "Park":
            self._invalidate_spot_cache()

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
            # Reaching a parking bay proves the car has cleared the entrance,
            # even if the simulator omitted or reordered ENTRY1/CarOut.
            car["entering"] = False
            car["entry_admitted_at"] = None
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
            "entering": False,       # gateA is open for this car until ENTRY1 CarOut
            "entry_admitted_at": None,
            "entry_retry_count": 0,
        }
        self.cars[plate] = car
        car["session_id"] = self._db("start_session", plate, car["car_type"], when)
        self._allocate(car)

    def _on_entry_out(self, plate):
        """Mark one admitted car as through and close gateA when the batch is clear.

        The simulator can let several queued cars cross ENTRY1 while gateA is
        physically open.  Every accepted car therefore gets its destination
        immediately; we only close the barrier once all accepted cars that are
        still waiting for their own ENTRY1/CarOut have passed.
        """
        car = self.cars.get(plate)
        if car is None or not car.get("entering"):
            return
        car["entering"] = False
        car["entry_admitted_at"] = None
        if any(c.get("entering") for c in self.cars.values()):
            logger.info("%s passed entry; gateA stays open for other admitted cars", plate)
            return
        self._close_gates(ENTRY_GATES, f"{plate} cleared the admitted entry batch")

    def _parking_spots(self):
        """The spot list, optionally reused for spots_cache_s seconds. Our own
        reservations are tracked locally, so a few seconds old is safe and it
        saves one simulator call per waiting car per event."""
        now = time.monotonic()
        if (self.spots_cache_s and self._spots is not None
                and now - self._spots_at < self.spots_cache_s):
            return self._spots
        self._spots = self.client.list_parking_spots()
        self._spots_at = now
        return self._spots

    def _invalidate_spot_cache(self):
        """Force the next allocation/recovery to read fresh spot occupancy."""
        self._spots = None
        self._spots_at = None

    def _allocate(self, car):
        try:
            spots = self._parking_spots()
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
        # Live Level 1 behaviour is order-sensitive: cars can sit at ENTRY1 if
        # goto is sent while gateA is still closed.  Open the entry barrier first,
        # then issue the movement command.
        gate_opened = self._open_gates(ENTRY_GATES, f"letting {car['plate']} in")
        if gate_opened is False:
            logger.warning("%s waits: entry gate open command failed", car["plate"])
            return
        # In live mode, wait until the simulator reports the barrier as Open
        # before issuing goto.  Otherwise the first car can receive goto while
        # gateA is still physically moving and remain idle at ENTRY1.
        if gate_opened is True:
            for gate_name in ENTRY_GATES:
                if not self._wait_for_gate_state(gate_name, "Open"):
                    logger.warning("%s waits: %s did not become Open",
                                   car["plate"], gate_name)
                    self._close_gates(ENTRY_GATES,
                                      f"entry gate not ready for {car['plate']}")
                    return
        try:
            self.client.move_car(car["plate"], spot["name"])
        except SimulatorError as e:
            logger.error("move_car(%s, %s) failed: %s", car["plate"], spot["name"], e)
            # If nobody else is currently crossing, restore the normal closed
            # state after a failed movement command.
            if not any(c.get("entering") for c in self.cars.values()):
                self._close_gates(ENTRY_GATES, f"move failed for {car['plate']}")
            return
        car["spot"] = spot["name"]
        car["stage"] = MOVING
        self._db("assign_spot", car["session_id"], spot["name"])
        # The simulator may let several queued cars cross ENTRY1 during one
        # physical gate opening. Track all cars that were actually commanded
        # through the open barrier and close it when the batch clears.
        car["entering"] = True
        car["entry_admitted_at"] = time.monotonic()
        car["entry_retry_count"] = 0
        logger.info("Sent %s to %s after entry gate confirmed Open",
                    car["plate"], spot["name"])
        self._schedule_entry_retry(car["plate"], spot["name"])

    def _schedule_entry_retry(self, plate, destination):
        """Schedule one non-blocking recovery attempt for a rare stuck entry car."""
        timer = threading.Timer(ENTRY_MOVE_RETRY_S, self._retry_entry_move,
                                args=(plate, destination))
        timer.daemon = True
        timer.start()

    def _retry_entry_move(self, plate, destination):
        """Retry goto once if a commanded car never clears ENTRY1.

        Refresh the simulator spot list first.  If the originally assigned bay
        became occupied in the meantime, choose a different compatible free bay
        instead of repeating the same invalid destination.  Recovery remains
        finite: each car gets at most ENTRY_MOVE_MAX_RETRIES attempts.
        """
        with self._lock:
            car = self.cars.get(plate)
            if car is None:
                return
            if car.get("stage") != MOVING or not car.get("entering"):
                return
            if car.get("spot") != destination:
                return
            if car.get("entry_retry_count", 0) >= ENTRY_MOVE_MAX_RETRIES:
                return

            # Occupancy may have changed after the original allocation.  Always
            # refresh before retrying so we do not resend a car to a bay that is
            # now occupied.
            self._invalidate_spot_cache()
            try:
                spots = self._parking_spots()
            except SimulatorError as e:
                logger.error("Entry retry could not refresh parking spots for %s: %s",
                             plate, e)
                return

            reserved = {
                c["spot"]
                for c in self.cars.values()
                if c.get("plate") != plate
                and c.get("spot")
                and c.get("parked_out_time") is None
            }
            free = [s for s in spots if s.get("name") not in reserved]
            spot = select_parking_spot({"CarType": car["car_type"]}, free)
            if spot is None:
                logger.warning("Entry retry for %s: no free compatible spot", plate)
                return
            new_destination = spot["name"]

            # Make sure the barrier is still physically open before retrying goto.
            gate_opened = self._open_gates(ENTRY_GATES,
                                           f"recovering stuck entry car {plate}")
            if gate_opened is False:
                logger.warning("Entry retry for %s skipped: gate open failed", plate)
                return
            if gate_opened is True:
                for gate_name in ENTRY_GATES:
                    if not self._wait_for_gate_state(gate_name, "Open"):
                        logger.warning("Entry retry for %s skipped: %s not Open",
                                       plate, gate_name)
                        return

            try:
                self.client.move_car(plate, new_destination)
            except SimulatorError as e:
                logger.error("entry retry move_car(%s, %s) failed: %s",
                             plate, new_destination, e)
                return

            if new_destination != destination:
                logger.warning("Entry retry reassigned %s from %s to %s",
                               plate, destination, new_destination)
                car["spot"] = new_destination
                self._db("assign_spot", car["session_id"], new_destination)

            car["entry_retry_count"] = car.get("entry_retry_count", 0) + 1
            logger.warning("Retried entry move for %s -> %s (%s/%s)",
                           plate, new_destination, car["entry_retry_count"],
                           ENTRY_MOVE_MAX_RETRIES)

    def _on_exit_in(self, car, when):
        car["exit_in_time"] = when
        if car["stage"] in (CHARGING, DONE):
            return  # duplicate; never charge twice
        car["stage"] = AT_EXIT
        self._open_gates(EXIT_GATES, f"letting {car['plate']} out")
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
        self._open_gates(EXIT_GATES, f"{car['plate']} is leaving")

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
