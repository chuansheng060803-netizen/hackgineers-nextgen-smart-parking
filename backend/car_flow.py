"""Level 1 & Level 2 orchestration: webhook events -> logic -> SimulatorClient."""
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

ZONE_ENTRY_GATE_MAP = {
    "ZONE1": ["gate1"],
    "ZONE2": ["gate3"],
    "ZONE3": ["gate5"],
}

ZONE_EXIT_GATE_MAP = {
    "ZONE1": ["gate2"],
    "ZONE2": ["gate4"],
    "ZONE3": ["gate6"],
}


def verify_signature(event):
    if event.get("Signature") is not None:
        logger.warning("Signature present but not verified: %s", event.get("EventId"))
    return True


def amount_is_valid(event, car):
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
        self.spots_cache_s = spots_cache_s
        self._spots = None
        self._spots_at = None
        self.processed = 0
        self.amount_check = amount_check
        self.db = db
        self.cars = {}
        self._seen_event_ids = set()
        self._last_sequence_id = None
        self._lock = threading.RLock()

    def _db(self, method, *args):
        if self.db is None:
            return None
        try:
            return getattr(self.db, method)(*args)
        except Exception:
            logger.exception("Database %s failed", method)
            return None

    def handle_event(self, event):
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

    def initialize_gates(self):
        """Force all zone gates closed on startup."""
        all_gates = ["gate1", "gate2", "gate3", "gate4", "gate5", "gate6"]
        self._close_gates(all_gates, "initializing car park - closing all gates")

    def _open_gates(self, names, why):
        for name in names:
            try:
                self.client.open_gate(name)
                logger.info("Opened %s (%s)", name, why)
            except Exception as e:
                logger.error("open_gate(%s) failed: %s", name, e)

    def _close_gates(self, names, why):
        for name in names:
            try:
                self.client.close_gate(name)
                logger.info("Closed %s (%s)", name, why)
            except Exception as e:
                logger.error("close_gate(%s) failed: %s", name, e)

    def _accept(self, event):
        event_id = event.get("EventId")
        if event_id is not None:
            if event_id in self._seen_event_ids:
                return False
            self._seen_event_ids.add(event_id)
        return verify_signature(event)

    def _on_car_spot_action(self, event):
        plate = event.get("CarPlateNumber")
        spot_type = event.get("SpotType")
        spot_name = event.get("SpotName")
        direction = event.get("Direction")
        when = _parse_time(event) or datetime.now()

        if spot_type == "EntrySpot":
            if direction == "CarIn":
                self._on_entry_in(plate, event, when)
            return

        car = self.cars.get(plate)
        if car is None:
            return

        if spot_type == "ExitSpot":
            if direction in ("CarIn", "CarOut"):
                self._on_exit_in(car, when)

            if direction == "CarOut" and car["paid"]:
                zone = car.get("zone")
                exit_gates = ZONE_EXIT_GATE_MAP.get(zone, [])
                self._close_gates(exit_gates, f"{plate} has left {zone}")
                self._db("complete_session", car["session_id"], when)
                del self.cars[plate]
            return

        if spot_name != car["spot"]:
            return

        if direction == "CarIn":
            car["parked_in_time"] = when
            if car["stage"] in (MOVING, WAITING):
                car["stage"] = PARKED
            self._db("mark_parked", car["session_id"], when)
            self._db("set_spot_occupied", spot_name, plate)

        elif direction == "CarOut":
            car["parked_out_time"] = when
            car["stage"] = LEAVING
            self._db("set_spot_available", spot_name)

    def _on_entry_in(self, plate, event, when):
        if plate in self.cars:
            car = self.cars[plate]
            if car["stage"] == WAITING:
                self._allocate(car)
            return

        car = {
            "plate": plate,
            "car_type": event.get("CarType"),
            "entry": event.get("SpotName"),
            "planned_minutes": event.get("PlannedParkingDurationInMinutes"),
            "stage": WAITING,
            "spot": None,
            "entry_in_time": when,
            "parked_in_time": None,
            "parked_out_time": None,
            "exit_in_time": None,
            "parking_cost": None,
            "charging_cost": None,
            "paid": False,
            "leavepark_sent": False,
            "session_id": None,
            "payment_id": None,
        }
        self.cars[plate] = car
        car["session_id"] = self._db("start_session", plate, car["car_type"], when)
        self._allocate(car)

    def _allocate(self, car):
        try:
            spots = self.client.list_parking_spots()
        except Exception as e:
            logger.error("list_parking_spots failed: %s", e)
            return

        reserved = {
            c["spot"] for c in self.cars.values()
            if c["spot"] and c["parked_out_time"] is None
        }

        free = [s for s in spots if s.get("name") not in reserved]
        spot = select_parking_spot({"CarType": car["car_type"]}, free)

        if spot is None:
            logger.warning("No spot available for %s", car["plate"])
            return

        # Extract target spot string cleanly
        if isinstance(spot, dict):
            target_spot_name = spot.get("name") or spot.get("SpotName")
            destination_zone = spot.get("zoneParent", "ZONE1")
        else:
            target_spot_name = str(spot)
            destination_zone = "ZONE1"

        entry_gates = ZONE_ENTRY_GATE_MAP.get(destination_zone, [])

        # 1. Open entry gate FIRST
        self._open_gates(entry_gates, f"letting {car['plate']} into {destination_zone}")

        car["spot"] = target_spot_name
        car["zone"] = destination_zone
        car["stage"] = MOVING
        self._db("assign_spot", car["session_id"], target_spot_name)

        # 2. Brief pause for physical gate barrier arm to register as open
        time.sleep(0.5)

        # 3. Dispatch movement command
        try:
            self.client.move_car(car["plate"], target_spot_name)
            logger.info("Sent move_car(%s, %s)", car["plate"], target_spot_name)
        except Exception as e:
            logger.error("move_car(%s, %s) failed: %s", car["plate"], target_spot_name, e)

        # 4. Close entry gate 4 seconds later after car passes
        def safe_gate_close():
            time.sleep(4.0)
            with self._lock:
                self._close_gates(entry_gates, f"timed close for {car['plate']}")

        threading.Thread(target=safe_gate_close, daemon=True).start()

    def _on_exit_in(self, car, when):
        car["exit_in_time"] = when
        if car["paid"]:
            return
        car["stage"] = AT_EXIT
        self._charge(car)

    def _charge(self, car):
        if car["paid"]:
            return

        start_time = car.get("parked_in_time") or car.get("entry_in_time") or datetime.now()
        end_time = car.get("exit_in_time") or datetime.now()

        charges = process_payment(
            car.get("car_type"), start_time, end_time, at_exit=True, already_paid=car["paid"]
        )

        try:
            self.client.charge_car(
                car["plate"],
                charges["parkingCost"],
                charges["chargingCost"]
            )
            logger.info("FEE SENT SUCCESSFULLY: %s -> %s", car["plate"], charges)
            car["stage"] = CHARGING
            car["parking_cost"] = charges["parkingCost"]
            car["charging_cost"] = charges["chargingCost"]
            car["payment_id"] = self._db(
                "record_pending_charge",
                car["session_id"],
                charges["parkingCost"],
                charges["chargingCost"],
            )
        except Exception as e:
            logger.error("CHARGE API FAILED for %s: %s", car["plate"], e)

    def _on_payment_made(self, event):
        car = self.cars.get(event.get("CarPlateNumber"))
        if not validate_payment(event, car, self.amount_check):
            return
        car["paid"] = True
        if car["payment_id"] is not None:
            self._db("mark_payment_paid", car["payment_id"])
        if get_post_payment_action(True) == "ALLOW_DEPARTURE":
            self._leavepark(car)

    def _leavepark(self, car):
        if car["leavepark_sent"]:
            return
        zone = car.get("zone")
        exit_gates = ZONE_EXIT_GATE_MAP.get(zone, [])

        self._open_gates(exit_gates, f"{car['plate']} paid and leaving {zone}")
        try:
            self.client.move_car(car["plate"], "leavepark")
            car["leavepark_sent"] = True
        except Exception as e:
            logger.error("leavepark failed: %s", e)

    def _retry_pending(self, skip=None):
        for car in list(self.cars.values()):
            if car["plate"] == skip:
                continue
            if car["stage"] == WAITING:
                self._allocate(car)
            elif not car["paid"] and car["stage"] in (LEAVING, AT_EXIT, CHARGING):
                self._charge(car)
            elif car["paid"] and not car["leavepark_sent"]:
                self._leavepark(car)