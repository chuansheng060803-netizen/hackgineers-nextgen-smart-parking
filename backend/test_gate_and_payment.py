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
        self.moved = []
        self.charged = []

    def list_barriers(self):
        return self.gates

    def list_parking_spots(self):
        return self.spots

    def open_gate(self, name):
        self.opened.append(name)

    def close_gate(self, name):
        pass

    def move_car(self, plate, destination):
        self.moved.append((plate, destination))

    def charge_car(self, plate, parking_cost, charging_cost):
        self.charged.append((plate, parking_cost, charging_cost))


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

    def test_a_failing_open_does_not_break_the_flow(self):
        def boom(name):
            raise SimulatorError("refused")
        self.client.open_gate = boom
        self.flow.handle_event(entry_event())          # must not raise
        self.assertEqual(self.client.moved[0][0], "RFB 098")

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
        self.assertIn("gateB", self.client.opened)

    def test_gate_names_are_configurable(self):
        self.assertEqual(car_flow.ENTRY_GATES, ["gateA"])
        self.assertEqual(car_flow.EXIT_GATES, ["gateB"])


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
