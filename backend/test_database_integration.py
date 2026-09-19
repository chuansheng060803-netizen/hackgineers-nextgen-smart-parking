"""Offline tests for the CarFlow <-> SQLite integration.

Uses the real database_service against a temporary SQLite file. No network,
no simulator, and the real database/parking.db is never touched.

  python -B -m unittest test_database_integration
"""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import car_flow
from car_flow import CarFlow
from database_adapter import DatabaseAdapter
import database.database as database_module
from database import database_service as service
from test_car_flow import PLATE, T_OUT, FlowTestCase, spot

REAL_DB_PATH = Path(database_module.DATABASE_PATH).resolve()


def payments():
    with closing(database_module.get_connection()) as connection:
        return [dict(r) for r in connection.execute("SELECT * FROM payments ORDER BY id")]


def spot_row(name):
    return next((s for s in service.get_parking_spots() if s["name"] == name), None)


def sim_spot(name, detected_cars=0, broken=False, zone="ZONE1"):
    """A spot in the real list_parking_spots() shape."""
    return {
        "name": name,
        "purpose": "Park",
        "parkingForCarType": "Any",
        "zoneParent": zone,
        "detectedCars": detected_cars,
        "broken": broken,
        "isUnderMaintenance": False,
    }


class DbTestCase(FlowTestCase):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "test.db"
        self.assertNotEqual(path.resolve(), REAL_DB_PATH)

        patcher = mock.patch.object(database_module, "DATABASE_PATH", path)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.adapter = DatabaseAdapter()
        self.adapter.initialize()
        self.client.spots = [spot("S1"), spot("S2")]
        self.flow = CarFlow(self.client, db=self.adapter)

    def use_valid_payments(self):
        self.flow = CarFlow(self.client, amount_check=lambda event, car: True, db=self.adapter)

    def session(self):
        return service.get_parking_session(self.car()["session_id"])


class SyncTests(DbTestCase):
    def test_sync_spots_stores_real_shape(self):
        self.adapter.sync_spots([sim_spot("S1"), sim_spot("S2", detected_cars=2, broken=True)])
        s1, s2 = spot_row("S1"), spot_row("S2")
        self.assertEqual((s1["status"], s1["zone"], s1["broken"]), ("available", "ZONE1", 0))
        self.assertEqual((s2["status"], s2["broken"]), ("occupied", 1))

    def test_second_sync_updates_instead_of_duplicating(self):
        self.adapter.sync_spots([sim_spot("S1"), sim_spot("S2", detected_cars=2)])
        self.adapter.sync_spots([sim_spot("S1"), sim_spot("S2")])
        self.assertEqual(len(service.get_parking_spots()), 2)
        self.assertEqual(spot_row("S2")["status"], "available")

    def test_sync_from_client_uses_list_parking_spots(self):
        self.adapter.sync(self.client)
        self.assertEqual([s["name"] for s in service.get_parking_spots()], ["S1", "S2"])
        self.assertEqual(self.client.calls, [])  # read-only, no commands


class SessionTests(DbTestCase):
    def test_arrival_creates_active_session_and_remembers_id(self):
        self.enter()
        sessions = service.get_active_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["id"], self.car()["session_id"])
        self.assertEqual(sessions[0]["car_name"], PLATE)
        self.assertEqual(sessions[0]["car_type"], "Normal")
        self.assertEqual(sessions[0]["arrival_time"], "2026-09-19T14:00:00")
        self.assertEqual(sessions[0]["status"], "active")

    def test_selected_spot_is_assigned_to_session(self):
        self.enter()
        self.assertEqual(self.session()["spot_name"], "S1")

    def test_park_in_marks_parked_and_spot_occupied_then_available(self):
        self.adapter.sync_spots([sim_spot("S1"), sim_spot("S2")])
        self.enter()
        self.park_in()
        self.assertEqual(self.session()["parked_time"], "2026-09-19T14:00:00")
        s1 = spot_row("S1")
        self.assertEqual((s1["status"], s1["current_car"], s1["zone"]), ("occupied", PLATE, "ZONE1"))

        self.park_out()
        s1 = spot_row("S1")
        self.assertEqual((s1["status"], s1["current_car"], s1["zone"]), ("available", None, "ZONE1"))

    def test_exit_completes_session(self):
        self.use_valid_payments()
        self.to_charging()
        self.payment()
        self.assertEqual(self.session()["status"], "active")  # leavepark sent, not gone yet
        session_id = self.car()["session_id"]

        self.leave_exit()
        session = service.get_parking_session(session_id)
        self.assertEqual(session["status"], "completed")
        self.assertEqual(session["departure_time"], "2026-09-19T14:10:00")


class PaymentTests(DbTestCase):
    def test_charge_is_recorded_as_pending(self):
        self.to_charging()
        rows = payments()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["session_id"], self.car()["session_id"])
        self.assertEqual(rows[0]["id"], self.car()["payment_id"])
        self.assertEqual((rows[0]["parking_cost"], rows[0]["charging_cost"]), (10.0, 0))
        self.assertEqual((rows[0]["status"], rows[0]["paid_at"]), ("pending", None))

    def test_payment_stays_pending_when_the_amount_is_wrong(self):
        # the default check compares Amount with what we billed (10.0)
        self.to_charging()
        self.payment(amount=3.0)
        self.assertEqual(payments()[0]["status"], "pending")

    def test_valid_payment_marks_paid_once(self):
        self.use_valid_payments()
        self.to_charging()
        self.payment()
        self.payment()  # repeated payment_made
        rows = payments()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "paid")
        self.assertIsNotNone(rows[0]["paid_at"])

    def test_payment_for_unknown_car_changes_nothing(self):
        self.use_valid_payments()
        self.payment("ZZZ 999")
        self.assertEqual(payments(), [])

    def test_payment_before_charge_changes_nothing(self):
        self.use_valid_payments()
        self.enter()
        self.park_in()
        self.payment()
        self.assertEqual(payments(), [])

    def test_electric_charge_amounts_stored(self):
        self.to_charging("EV 123", "Electric")
        row = payments()[0]
        self.assertEqual((row["parking_cost"], row["charging_cost"]), (10.0, 10.0))


class EventLogTests(DbTestCase):
    def test_accepted_events_are_logged_with_type_car_and_component(self):
        self.enter()
        events = service.get_recent_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "car_spot_action")
        self.assertEqual(events[0]["car_name"], PLATE)
        self.assertEqual(events[0]["component_name"], "ENTRY1")
        self.assertEqual(events[0]["payload"]["Direction"], "CarIn")

    def test_duplicate_event_id_is_logged_once(self):
        e = self.event(SpotName="ENTRY1", SpotType="EntrySpot", Direction="CarIn",
                       CarPlateNumber=PLATE, CarType="Normal")
        self.flow.handle_event(e)
        self.flow.handle_event(dict(e))
        self.assertEqual(len(service.get_recent_events()), 1)

    def test_payment_made_is_logged(self):
        self.to_charging()
        self.payment()
        self.assertEqual(service.get_recent_events()[0]["event_type"], "payment_made")


class FailureIsolationTests(DbTestCase):
    def test_database_errors_never_break_the_flow(self):
        class BrokenAdapter:
            def __getattr__(self, name):
                def fail(*args, **kwargs):
                    raise sqlite3.Error(f"{name} failed")
                return fail

        self.flow = CarFlow(self.client, amount_check=lambda e, c: True, db=BrokenAdapter())
        with self.assertLogs("car_flow", "ERROR"):
            self.to_charging()   # must not raise
            self.payment()
        self.assertEqual(self.client.of("move_car")[0], ("move_car", PLATE, "S1"))
        self.assertEqual(self.client.of("charge_car"), [("charge_car", PLATE, 10.0, 0)])
        self.assertIn(("move_car", PLATE, "leavepark"), self.client.calls)

    def test_without_db_nothing_is_written(self):
        self.flow = CarFlow(self.client)
        self.assertIsNone(self.flow.db)
        self.to_charging()
        self.assertEqual(service.get_active_sessions(), [])
        self.assertEqual(payments(), [])


if __name__ == "__main__":
    unittest.main()
