"""Tests for the two fixes that made the car park work end to end:

  1. the entrance barrier is opened, so cars can actually get in
  2. payment_made.Amount is checked, so departures are authorised

Both use the exact payloads the live simulator sent (captured from the backend
log on 19 Sep 2026).
"""
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import car_flow
from car_flow import AT_EXIT, CHARGING, DONE, CarFlow, amount_is_valid
from simulator_api import SimulatorError

NOW = "2026-09-19 17:58:43"


class FakeClient:
    def __init__(self):
        self.gates = [
            {"name": "gateA", "zoneParent": "ZONE1", "broken": False,
             "isUnderMaintenance": False, "state": "Closed"},
            {"name": "gateB", "zoneParent": "ZONE1", "broken": False,
             "isUnderMaintenance": False, "state": "Open"},
        ]
        self.spots = [{"name": f"S{i}", "purpose": "Park", "parkingForCarType": "Any",
                       "zoneParent": "ZONE1", "detectedCars": 0, "broken": False,
                       "isUnderMaintenance": False} for i in range(1, 31)]
        self.opened = []
        self.closed = []
        self.moved = []
        self.charged = []
        self.actions = []

    def list_barriers(self):
        return self.gates

    def list_parking_spots(self):
        return self.spots

    def open_gate(self, name):
        self.opened.append(name)
        self.actions.append(("open", name))
        for gate in self.gates:
            if gate.get("name") == name:
                gate["state"] = "Open"

    def close_gate(self, name):
        self.closed.append(name)
        self.actions.append(("close", name))
        for gate in self.gates:
            if gate.get("name") == name:
                gate["state"] = "Closed"

    def move_car(self, plate, destination):
        self.moved.append((plate, destination))
        self.actions.append(("move", plate, destination))

    def charge_car(self, plate, parking_cost, charging_cost):
        self.charged.append((plate, parking_cost, charging_cost))
        self.actions.append(("charge", plate, parking_cost, charging_cost))


def entry_event(plate="RFB 098", minutes="1"):
    return {"EventClass": "car_spot_action", "CarPlateNumber": plate, "CarType": "Normal",
            "SpotName": "ENTRY1", "SpotType": "EntrySpot", "Direction": "CarIn",
            "PlannedParkingDurationInMinutes": minutes, "EventId": f"e-{plate}",
            "SequenceId": 1, "Signature": None, "ServerDateTime": NOW}


class GateTests(unittest.TestCase):
    """Without this the barrier stays down and every car queues at the entry."""

    def setUp(self):
        self.client = FakeClient()
        self.flow = CarFlow(self.client)

    def test_entrance_gate_is_opened_when_a_car_is_let_in(self):
        self.flow.handle_event(entry_event())
        self.assertIn("gateA", self.client.opened)
        self.assertEqual(self.client.moved[0][0], "RFB 098")

    def test_a_broken_entrance_gate_is_never_operated(self):
        self.client.gates[0]["broken"] = True
        self.flow.handle_event(entry_event())
        self.assertEqual(self.client.opened, [],
                         "must not operate a broken component")

    def test_a_gate_under_maintenance_is_never_operated(self):
        self.client.gates[0]["isUnderMaintenance"] = True
        self.flow.handle_event(entry_event())
        self.assertEqual(self.client.opened, [])

    def test_unknown_gate_health_means_do_not_touch(self):
        def boom():
            raise SimulatorError("simulator down")
        self.client.list_barriers = boom
        self.flow.handle_event(entry_event())
        self.assertEqual(self.client.opened, [])

    def test_a_failing_open_does_not_move_the_car(self):
        def boom(name):
            raise SimulatorError("refused")
        self.client.open_gate = boom
        self.flow.handle_event(entry_event())          # must not raise
        self.assertEqual(self.client.moved, [])
        self.assertEqual(self.flow.cars["RFB 098"]["stage"], car_flow.WAITING)

    def test_charge_is_sent_before_exit_gate_close_command(self):
        self.flow.handle_event(entry_event())
        plate = "RFB 098"
        spot = self.flow.cars[plate]["spot"]
        for direction in ("CarIn", "CarOut"):
            self.flow.handle_event({"EventClass": "car_spot_action", "CarPlateNumber": plate,
                                    "SpotName": spot, "SpotType": "Park", "Direction": direction,
                                    "EventId": f"order-{direction}", "ServerDateTime": NOW})
        self.client.actions.clear()
        self.flow.handle_event({"EventClass": "car_spot_action", "CarPlateNumber": plate,
                                "SpotName": "EXIT_EXIT", "SpotType": "ExitSpot",
                                "Direction": "CarIn", "EventId": "order-exit",
                                "ServerDateTime": NOW})
        kinds = [action[0] for action in self.client.actions]
        self.assertIn("charge", kinds)
        self.assertIn("close", kinds)
        self.assertLess(kinds.index("charge"), kinds.index("close"))

    def test_exit_gate_is_opened_when_a_car_reaches_the_exit(self):
        self.flow.handle_event(entry_event())
        plate = "RFB 098"
        spot = self.flow.cars[plate]["spot"]
        self.client.opened.clear()
        for direction in ("CarIn", "CarOut"):
            self.flow.handle_event({"EventClass": "car_spot_action", "CarPlateNumber": plate,
                                    "SpotName": spot, "SpotType": "Park", "Direction": direction,
                                    "EventId": f"p{direction}", "ServerDateTime": NOW})
        self.flow.handle_event({"EventClass": "car_spot_action", "CarPlateNumber": plate,
                                "SpotName": "EXIT_EXIT", "SpotType": "ExitSpot",
                                "Direction": "CarIn", "EventId": "x1", "ServerDateTime": NOW})
        self.assertNotIn("gateB", self.client.opened)
        self.assertIn("gateB", self.client.closed)

    def test_gate_names_are_configurable(self):
        self.assertEqual(car_flow.ENTRY_GATES, ["gateA"])
        self.assertEqual(car_flow.EXIT_GATES, ["gateB"])


class EntryLifecycleTests(unittest.TestCase):
    """Entry gateA is normally closed and closes after each admitted batch clears."""

    def setUp(self):
        self.client = FakeClient()
        self.flow = CarFlow(self.client)
        self.seq = 0

    def _entry(self, plate, direction="CarIn", car_type="Normal"):
        self.seq += 1
        self.flow.handle_event({
            "EventClass": "car_spot_action",
            "CarPlateNumber": plate,
            "CarType": car_type,
            "SpotName": "ENTRY1",
            "SpotType": "EntrySpot",
            "Direction": direction,
            "PlannedParkingDurationInMinutes": "1",
            "EventId": f"entry-{self.seq}",
            "SequenceId": self.seq,
            "Signature": None,
            "ServerDateTime": NOW,
        })

    def test_startup_initialization_closes_gateA_only(self):
        self.flow.initialize_entry_gate()
        self.assertEqual(self.client.closed, ["gateA"])
        self.assertNotIn("gateB", self.client.closed)

    def test_one_car_opens_then_closes_gateA(self):
        self._entry("AAA 111", "CarIn")
        self.assertEqual(self.client.moved, [("AAA 111", "S1")])
        self.assertIn("gateA", self.client.opened)
        self.assertTrue(self.flow.cars["AAA 111"]["entering"])

        self._entry("AAA 111", "CarOut")
        self.assertEqual(self.client.closed, ["gateA"])
        self.assertFalse(self.flow.cars["AAA 111"]["entering"])

    def test_gate_opens_before_move_command(self):
        self._entry("AAA 111", "CarIn")
        self.assertGreaterEqual(len(self.client.actions), 2)
        self.assertEqual(self.client.actions[0], ("open", "gateA"))
        self.assertEqual(self.client.actions[1], ("move", "AAA 111", "S1"))

    def test_move_waits_until_gate_reports_open(self):
        # Simulate a barrier that needs a few status reads before it is Open.
        reads = {"count": 0}
        real_list = self.client.list_barriers

        def delayed_list():
            reads["count"] += 1
            gates = real_list()
            if reads["count"] < 3:
                gates[0]["state"] = "Opening"
            else:
                gates[0]["state"] = "Open"
            return gates

        self.client.list_barriers = delayed_list
        self._entry("AAA 111", "CarIn")
        self.assertGreaterEqual(reads["count"], 3)
        self.assertEqual(self.client.moved, [("AAA 111", "S1")])

    def test_multiple_arrivals_are_all_allocated_while_gate_is_open(self):
        self.client.spots = [
            {**self.client.spots[0], "name": "S1"},
            {**self.client.spots[0], "name": "S2"},
            {**self.client.spots[0], "name": "S3"},
        ]
        self._entry("AAA 111")
        self._entry("BBB 222")
        self._entry("CCC 333")
        self.assertEqual([p for p, _ in self.client.moved],
                         ["AAA 111", "BBB 222", "CCC 333"])
        self.assertTrue(all(self.flow.cars[p]["entering"]
                            for p in ("AAA 111", "BBB 222", "CCC 333")))

    def test_gate_stays_open_until_all_admitted_cars_have_carout(self):
        self.client.spots = [
            {**self.client.spots[0], "name": "S1"},
            {**self.client.spots[0], "name": "S2"},
        ]
        self._entry("AAA 111")
        self._entry("BBB 222")

        self._entry("AAA 111", "CarOut")
        self.assertEqual(self.client.closed, [])
        self.assertFalse(self.flow.cars["AAA 111"]["entering"])
        self.assertTrue(self.flow.cars["BBB 222"]["entering"])

        self._entry("BBB 222", "CarOut")
        self.assertEqual(self.client.closed, ["gateA"])

    def test_out_of_order_carout_does_not_leave_a_car_stuck_entering(self):
        # Reproduces the live simulator behaviour observed on 2026-09-19:
        # the second queued car can report CarOut before the first one.
        self.client.spots = [
            {**self.client.spots[0], "name": "S1"},
            {**self.client.spots[0], "name": "S2"},
        ]
        self._entry("HJL 293")
        self._entry("VBS 796")
        self._entry("VBS 796", "CarOut")
        self.assertFalse(self.flow.cars["VBS 796"]["entering"])
        self.assertEqual(self.client.closed, [])

        self._entry("HJL 293", "CarOut")
        self.assertFalse(self.flow.cars["HJL 293"]["entering"])
        self.assertEqual(self.client.closed, ["gateA"])

    def test_stuck_entry_retries_move_once(self):
        self._entry("AAA 111")
        self.assertEqual(self.client.moved, [("AAA 111", "S1")])
        self.flow._retry_entry_move("AAA 111", "S1")
        self.assertEqual(self.client.moved,
                         [("AAA 111", "S1"), ("AAA 111", "S1")])
        self.assertEqual(self.flow.cars["AAA 111"]["entry_retry_count"], 1)
        # The configured maximum is one retry, so another recovery pass is a no-op.
        self.flow._retry_entry_move("AAA 111", "S1")
        self.assertEqual(len(self.client.moved), 2)

    def test_entry_retry_is_cancelled_logically_after_carout(self):
        self._entry("AAA 111")
        self._entry("AAA 111", "CarOut")
        self.flow._retry_entry_move("AAA 111", "S1")
        self.assertEqual(self.client.moved, [("AAA 111", "S1")])

    def test_move_failure_recloses_gate_when_no_other_car_is_entering(self):
        def boom(plate, destination):
            raise SimulatorError("move failed")
        self.client.move_car = boom
        self._entry("AAA 111")
        self.assertIn("gateA", self.client.opened)
        self.assertIn("gateA", self.client.closed)
        self.assertEqual(self.flow.cars["AAA 111"]["stage"], car_flow.WAITING)
        self.assertFalse(self.flow.cars["AAA 111"]["entering"])

    def test_full_lot_never_opens_gate(self):
        for s in self.client.spots:
            s["detectedCars"] = 1
        self._entry("AAA 111")
        self.assertEqual(self.client.moved, [])
        self.assertEqual(self.client.opened, [])
        self.assertEqual(self.flow.cars["AAA 111"]["stage"], car_flow.WAITING)

    def test_unknown_or_duplicate_carout_does_not_close_twice(self):
        self._entry("ZZZ 999", "CarOut")
        self.assertEqual(self.client.closed, [])
        self._entry("AAA 111")
        self._entry("AAA 111", "CarOut")
        self._entry("AAA 111", "CarOut")
        self.assertEqual(self.client.closed, ["gateA"])

    def test_entry_lifecycle_never_operates_gateB(self):
        self.flow.initialize_entry_gate()
        self._entry("AAA 111")
        self._entry("AAA 111", "CarOut")
        self.assertNotIn("gateB", self.client.opened)
        self.assertNotIn("gateB", self.client.closed)


    def test_parking_carin_clears_entry_hold_if_entry_carout_is_missing(self):
        self._entry("AAA 111")
        spot = self.flow.cars["AAA 111"]["spot"]
        self.assertTrue(self.flow.cars["AAA 111"]["entering"])
        self.flow.handle_event({
            "EventClass": "car_spot_action", "CarPlateNumber": "AAA 111",
            "SpotName": spot, "SpotType": "Park", "Direction": "CarIn",
            "EventId": "park-proof", "SequenceId": 99,
            "ServerDateTime": NOW,
        })
        self.assertFalse(self.flow.cars["AAA 111"]["entering"])




class PaymentAmountTests(unittest.TestCase):
    """The simulator sends Amount as a string of the total it charged."""

    @staticmethod
    def car(parking, charging=0):
        return {"plate": "RFB 098", "parking_cost": parking, "charging_cost": charging}

    def test_the_real_payment_from_the_simulator_is_accepted(self):
        # captured live: we billed 1.0233, the simulator paid back '1.02'
        event = {"EventClass": "payment_made", "CarPlateNumber": "RFB 098",
                 "Amount": "1.02", "Reason": "Car Payment", "ServerDateTime": NOW}
        self.assertTrue(amount_is_valid(event, self.car(1.0233)))

    def test_an_exact_amount_is_accepted(self):
        self.assertTrue(amount_is_valid({"Amount": "4.00"}, self.car(4)))

    def test_an_electric_car_pays_both_costs(self):
        self.assertTrue(amount_is_valid({"Amount": "6.00"}, self.car(3, 3)))

    def test_underpayment_is_refused(self):
        self.assertFalse(amount_is_valid({"Amount": "0.50"}, self.car(4)))

    def test_overpayment_is_refused(self):
        self.assertFalse(amount_is_valid({"Amount": "40.00"}, self.car(4)))

    def test_a_missing_or_junk_amount_is_refused(self):
        self.assertFalse(amount_is_valid({}, self.car(4)))
        self.assertFalse(amount_is_valid({"Amount": None}, self.car(4)))
        self.assertFalse(amount_is_valid({"Amount": "free"}, self.car(4)))


class DepartureTests(unittest.TestCase):
    """The whole point of fixing the amount check: the car is let out."""

    def setUp(self):
        self.client = FakeClient()
        self.flow = CarFlow(self.client)

    def _drive_to_exit(self, plate="RFB 098"):
        self.flow.handle_event(entry_event(plate))
        spot = self.flow.cars[plate]["spot"]
        for direction in ("CarIn", "CarOut"):
            self.flow.handle_event({"EventClass": "car_spot_action", "CarPlateNumber": plate,
                                    "SpotName": spot, "SpotType": "Park", "Direction": direction,
                                    "EventId": f"{plate}{direction}", "ServerDateTime": NOW})
        self.flow.handle_event({"EventClass": "car_spot_action", "CarPlateNumber": plate,
                                "SpotName": "EXIT_EXIT", "SpotType": "ExitSpot",
                                "Direction": "CarIn", "EventId": f"{plate}x",
                                "ServerDateTime": NOW})
        return plate

    def test_a_correct_payment_authorises_departure(self):
        plate = self._drive_to_exit()
        car = self.flow.cars[plate]
        self.assertEqual(car["stage"], CHARGING)
        billed = round(car["parking_cost"] + car["charging_cost"], 2)

        self.flow.handle_event({"EventClass": "payment_made", "CarPlateNumber": plate,
                                "Amount": f"{billed:.2f}", "Reason": "Car Payment",
                                "EventId": "pay1", "ServerDateTime": NOW})
        self.assertTrue(car["paid"])
        self.assertEqual(car["stage"], DONE)
        self.assertIn("gateB", self.client.opened)
        self.assertIn((plate, "leavepark"), self.client.moved)

    def test_a_wrong_payment_does_not_authorise_departure(self):
        plate = self._drive_to_exit()
        # nowhere near what this car was billed
        self.flow.handle_event({"EventClass": "payment_made", "CarPlateNumber": plate,
                                "Amount": "99.99", "EventId": "pay2", "ServerDateTime": NOW})
        car = self.flow.cars[plate]
        self.assertFalse(car["paid"])
        self.assertNotIn((plate, "leavepark"), self.client.moved)


if __name__ == "__main__":
    unittest.main(verbosity=2)
