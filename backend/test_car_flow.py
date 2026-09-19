"""Offline tests for car_flow.py. No network: a fake client records commands.

  python test_car_flow.py
"""
import unittest
from unittest import mock

import car_flow
from car_flow import CarFlow
from simulator_api import SimulatorError

PLATE = "AAA 111"
T_IN = "2026-09-19 14:00:00"
T_OUT = "2026-09-19 14:10:00"


def spot(name, car_type="Any", detected_cars=0):
    return {
        "name": name,
        "purpose": "Park",
        "parkingForCarType": car_type,
        "detectedCars": detected_cars,
        "broken": False,
        "isUnderMaintenance": False,
    }


class FakeClient:
    """Records commands; can be told to fail like the real client would."""

    def __init__(self, spots):
        self.spots = spots
        self.calls = []
        self.fail = set()  # method names that raise SimulatorError

    def list_parking_spots(self):
        return self.spots

    def move_car(self, name, destination):
        if "move_car" in self.fail:
            raise SimulatorError("move_car failed")
        self.calls.append(("move_car", name, destination))

    def charge_car(self, name, parking_cost, charging_cost):
        if "charge_car" in self.fail:
            raise SimulatorError("charge_car failed")
        self.calls.append(("charge_car", name, parking_cost, charging_cost))

    def of(self, kind):
        return [c for c in self.calls if c[0] == kind]


class FlowTestCase(unittest.TestCase):
    def setUp(self):
        # Guarantee that no test can reach a real simulator.
        patcher = mock.patch("requests.request", side_effect=AssertionError("network used"))
        patcher.start()
        self.addCleanup(patcher.stop)

        self.client = FakeClient([spot("S1")])
        self.flow = CarFlow(self.client)
        self._n = 0

    # ---- event helpers ----

    def event(self, **fields):
        self._n += 1
        base = {
            "EventClass": "car_spot_action",
            "EventId": f"event-{self._n}",
            "SequenceId": self._n,
            "Signature": None,
            "ServerDateTime": T_IN,
        }
        base.update(fields)
        return base

    def send(self, spot_name, spot_type, direction, plate=PLATE, car_type="Normal", time=T_IN):
        self.flow.handle_event(self.event(
            SpotName=spot_name,
            SpotType=spot_type,
            Direction=direction,
            CarPlateNumber=plate,
            CarType=car_type,
            PlannedParkingDurationInMinutes="4",
            ServerDateTime=time,
        ))

    def enter(self, plate=PLATE, car_type="Normal"):
        self.send("ENTRY1", "EntrySpot", "CarIn", plate, car_type)

    def park_in(self, spot_name="S1", plate=PLATE, car_type="Normal"):
        self.send("ENTRY1", "EntrySpot", "CarOut", plate, car_type)
        self.send(spot_name, "Park", "CarIn", plate, car_type, T_IN)

    def park_out(self, spot_name="S1", plate=PLATE, car_type="Normal"):
        self.send(spot_name, "Park", "CarOut", plate, car_type, T_OUT)

    def reach_exit(self, plate=PLATE, car_type="Normal"):
        self.send("EXIT1", "ExitSpot", "CarIn", plate, car_type, T_OUT)

    def leave_exit(self, plate=PLATE, car_type="Normal"):
        self.send("EXIT1", "ExitSpot", "CarOut", plate, car_type, T_OUT)

    def payment(self, plate=PLATE, amount=10.0):
        self.flow.handle_event(self.event(
            EventClass="payment_made", CarPlateNumber=plate, Amount=amount, Reason="test",
        ))

    def unrelated_event(self):
        self.flow.handle_event(self.event(EventClass="penalty"))

    def to_charging(self, plate=PLATE, car_type="Normal"):
        """Entry -> parked -> left spot -> reached exit (charge sent)."""
        self.enter(plate, car_type)
        self.park_in("S1", plate, car_type)
        self.park_out("S1", plate, car_type)
        self.reach_exit(plate, car_type)

    def car(self, plate=PLATE):
        return self.flow.cars[plate]


class ParkingTests(FlowTestCase):
    def test_entry_moves_car_to_free_spot(self):
        self.enter()
        self.assertEqual(self.client.calls, [("move_car", PLATE, "S1")])
        self.assertEqual(self.car()["stage"], car_flow.MOVING)

    def test_second_car_gets_different_spot_before_first_parks(self):
        self.client.spots = [spot("S1"), spot("S2")]
        self.enter("AAA 111")
        self.enter("BBB 222")
        self.assertEqual(self.client.of("move_car"), [
            ("move_car", "AAA 111", "S1"),
            ("move_car", "BBB 222", "S2"),
        ])

    def test_full_lot_waits_then_moves_when_spot_freed(self):
        self.enter("AAA 111")
        self.enter("BBB 222")
        self.assertEqual(len(self.client.of("move_car")), 1)
        self.assertEqual(self.car("BBB 222")["stage"], car_flow.WAITING)

        self.park_in("S1", "AAA 111")
        self.park_out("S1", "AAA 111")
        self.assertEqual(self.client.of("move_car")[-1], ("move_car", "BBB 222", "S1"))

    def test_occupied_spots_with_nonzero_count_are_skipped(self):
        self.client.spots = [
            spot("S1", detected_cars=1),
            spot("S2", detected_cars=2),
            spot("S3"),
        ]
        self.enter()
        self.assertEqual(self.client.calls, [("move_car", PLATE, "S3")])

    def test_all_spots_occupied_car_waits(self):
        self.client.spots = [spot("S1", detected_cars=1), spot("S2", detected_cars=3)]
        self.enter()
        self.assertEqual(self.client.calls, [])
        self.assertEqual(self.car()["stage"], car_flow.WAITING)

    def test_car_type_compatibility(self):
        self.client.spots = [spot("S1", "Normal"), spot("S2", "Electric")]
        self.enter("EV 123", "Electric")
        self.enter("AAA 111", "Normal")
        self.assertEqual(self.client.of("move_car"), [
            ("move_car", "EV 123", "S2"),
            ("move_car", "AAA 111", "S1"),
        ])


class EventGateTests(FlowTestCase):
    def test_duplicate_event_id_processed_once(self):
        e = self.event(EventClass="car_spot_action", SpotName="ENTRY1", SpotType="EntrySpot",
                       Direction="CarIn", CarPlateNumber=PLATE, CarType="Normal")
        self.flow.handle_event(e)
        self.flow.handle_event(dict(e))
        self.assertEqual(len(self.client.of("move_car")), 1)

    def test_sequence_gap_logs_warning_and_still_processes(self):
        self.flow.handle_event(self.event(SequenceId=1, EventClass="penalty"))
        with self.assertLogs("car_flow", "WARNING"):
            self.flow.handle_event(self.event(
                SequenceId=5, SpotName="ENTRY1", SpotType="EntrySpot", Direction="CarIn",
                CarPlateNumber=PLATE, CarType="Normal"))
        self.assertEqual(len(self.client.of("move_car")), 1)

    def test_missing_signature_is_not_rejected(self):
        e = self.event(SpotName="ENTRY1", SpotType="EntrySpot", Direction="CarIn",
                       CarPlateNumber=PLATE, CarType="Normal")
        del e["Signature"]
        self.flow.handle_event(e)
        self.assertEqual(len(self.client.of("move_car")), 1)

    def test_non_null_signature_is_processed_with_warning(self):
        with self.assertLogs("car_flow", "WARNING"):
            self.flow.handle_event(self.event(
                Signature="abc", SpotName="ENTRY1", SpotType="EntrySpot", Direction="CarIn",
                CarPlateNumber=PLATE, CarType="Normal"))
        self.assertEqual(len(self.client.of("move_car")), 1)


class ExitAndChargeTests(FlowTestCase):
    def test_parking_stores_data_and_sends_no_exit_command(self):
        self.enter()
        self.park_in()
        self.assertEqual(self.car()["stage"], car_flow.PARKED)
        self.assertEqual(self.car()["planned_minutes"], "4")
        self.assertIsNotNone(self.car()["parked_in_time"])
        self.assertEqual(self.client.of("move_car"), [("move_car", PLATE, "S1")])

    def test_request_exit_sends_exit_once(self):
        self.enter()
        self.park_in()
        self.assertTrue(self.flow.request_exit(PLATE))
        self.assertFalse(self.flow.request_exit(PLATE))
        self.assertEqual(self.client.of("move_car")[1:], [("move_car", PLATE, "exit")])

    def test_request_exit_ignored_when_not_parked_or_unknown(self):
        self.enter()  # still MOVING
        self.assertFalse(self.flow.request_exit(PLATE))
        self.assertFalse(self.flow.request_exit("ZZZ 999"))
        self.assertEqual(len(self.client.of("move_car")), 1)

    def test_no_charge_before_exit_spot(self):
        self.enter()
        self.park_in()
        self.park_out()
        self.assertEqual(self.client.of("charge_car"), [])

    def test_charge_at_exit_normal_car(self):
        self.to_charging()
        self.assertEqual(self.client.of("charge_car"), [("charge_car", PLATE, 10.0, 0)])
        self.assertEqual(self.car()["stage"], car_flow.CHARGING)

    def test_charge_at_exit_electric_car(self):
        self.to_charging("EV 123", "Electric")
        self.assertEqual(self.client.of("charge_car"), [("charge_car", "EV 123", 10.0, 10.0)])

    def test_requested_costs_stored_separately(self):
        self.to_charging("EV 123", "Electric")
        self.assertEqual(self.car("EV 123")["parking_cost"], 10.0)
        self.assertEqual(self.car("EV 123")["charging_cost"], 10.0)

    def test_repeated_exit_event_does_not_charge_twice(self):
        self.to_charging()
        self.reach_exit()
        self.assertEqual(len(self.client.of("charge_car")), 1)


class PaymentTests(FlowTestCase):
    def test_successful_payment_lifecycle(self):
        # ExitSpot CarIn -> charge_car -> payment_made -> leavepark, with the default checks
        self.enter()
        self.park_in()
        self.park_out()
        self.reach_exit()
        self.assertEqual(self.car()["stage"], car_flow.CHARGING)
        self.assertFalse(self.car()["paid"])

        self.payment()
        self.assertTrue(self.car()["paid"])
        self.assertEqual(self.car()["stage"], car_flow.DONE)
        self.assertEqual(self.client.calls, [
            ("move_car", PLATE, "S1"),
            ("charge_car", PLATE, 10.0, 0),
            ("move_car", PLATE, "leavepark"),
        ])

        self.leave_exit()
        self.assertNotIn(PLATE, self.flow.cars)

    def test_payment_is_accepted_whatever_the_amount_is(self):
        # Amount semantics are undocumented, so no Amount rule is applied.
        for amount in (0, 10.0, 999.5):
            with self.subTest(amount=amount):
                self.client.calls.clear()
                plate = f"AMT {int(amount)}"
                self.to_charging(plate)
                self.payment(plate, amount=amount)
                self.assertIn(("move_car", plate, "leavepark"), self.client.calls)

    def test_repeated_payment_sends_leavepark_once(self):
        self.to_charging()
        self.payment()
        self.payment()  # repeated payment_made
        self.payment()
        self.assertEqual(self.client.calls.count(("move_car", PLATE, "leavepark")), 1)
        self.assertEqual(len(self.client.of("charge_car")), 1)
        self.assertEqual(self.car()["stage"], car_flow.DONE)

    def test_payment_rejected_when_the_amount_check_rejects_it(self):
        # The hook is kept: a real Amount rule can still be injected later.
        self.flow = CarFlow(self.client, amount_check=lambda event, car: False)
        self.to_charging()
        self.payment()
        self.assertFalse(self.car()["paid"])
        self.assertEqual(self.car()["stage"], car_flow.CHARGING)
        self.assertNotIn(("move_car", PLATE, "leavepark"), self.client.calls)

    def test_payment_for_unknown_car_rejected(self):
        self.payment("ZZZ 999")
        self.assertEqual(self.client.calls, [])

    def test_payment_for_another_car_does_not_release_this_one(self):
        self.to_charging()
        self.payment("ZZZ 999")
        self.assertFalse(self.car()["paid"])
        self.assertNotIn(("move_car", PLATE, "leavepark"), self.client.calls)

    def test_payment_before_charge_rejected(self):
        self.enter()
        self.park_in()
        self.payment()
        self.assertFalse(self.car()["paid"])
        self.assertNotIn(("move_car", PLATE, "leavepark"), self.client.calls)


class ErrorHandlingTests(FlowTestCase):
    def test_move_car_failure_is_retried_on_next_event(self):
        self.client.fail.add("move_car")
        self.enter()  # must not raise
        self.assertEqual(self.car()["stage"], car_flow.WAITING)
        self.assertEqual(self.client.calls, [])

        self.client.fail.clear()
        self.unrelated_event()
        self.assertEqual(self.client.calls, [("move_car", PLATE, "S1")])

    def test_charge_failure_is_retried_on_next_event(self):
        self.client.fail.add("charge_car")
        self.to_charging()  # must not raise
        self.assertEqual(self.car()["stage"], car_flow.AT_EXIT)
        self.assertEqual(self.client.of("charge_car"), [])

        self.client.fail.clear()
        self.unrelated_event()
        self.assertEqual(self.client.of("charge_car"), [("charge_car", PLATE, 10.0, 0)])


if __name__ == "__main__":
    unittest.main()
