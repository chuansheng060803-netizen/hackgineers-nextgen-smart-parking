import tempfile
import unittest
from pathlib import Path
from unittest import mock

import database.database as database_module
from database import database_service as service

from dashboard import auth
from dashboard import db_source


class DashboardDatabaseTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        test_db = Path(
            self.temp_dir.name
        ) / "dashboard_test.db"

        self.db_patch = mock.patch.object(
            database_module,
            "DATABASE_PATH",
            test_db,
        )

        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)

        database_module.init_database()

    def test_empty_database_snapshot(self):
        snap = db_source.fetch_db()

        self.assertEqual(
            snap["source"],
            "database",
        )

        self.assertEqual(
            snap["spots"],
            [],
        )

        self.assertEqual(
            snap["cars"],
            [],
        )

        self.assertEqual(
            snap["sessions"],
            [],
        )

        self.assertEqual(
            snap["gates"],
            [],
        )

        self.assertEqual(
            snap["events"],
            [],
        )

        self.assertEqual(
            snap["stats"]["revenue"],
            0,
        )

        self.assertEqual(
            snap["stats"]["cars_served"],
            0,
        )

    def test_active_car_appears_in_dashboard(self):
        service.add_or_update_spot(
            name="S1",
            zone="ZONE1",
            status="FREE",
            updated_at="2026-09-20T10:00:00",
        )

        session_id = service.start_session(
            plate="ABC 123",
            car_type="Normal",
            entry_gate_name="ENTRY1",
            arrival_time="2026-09-20T10:00:00",
        )

        service.start_parking(
            session_id=session_id,
            spot_name="S1",
            start_park="2026-09-20T10:01:00",
        )

        service.set_spot_occupied(
            spot_name="S1",
            plate="ABC 123",
            updated_at="2026-09-20T10:01:00",
        )

        snap = db_source.fetch_db()

        self.assertEqual(
            len(snap["cars"]),
            1,
        )

        car = snap["cars"][0]

        self.assertEqual(
            car["plate"],
            "ABC 123",
        )

        self.assertEqual(
            car["spot"],
            "S1",
        )

        self.assertEqual(
            car["status"],
            "Parked",
        )

        self.assertEqual(
            snap["spots"][0]["state"],
            "occupied",
        )

        self.assertEqual(
            snap["spots"][0]["car"],
            "ABC 123",
        )

    def test_completed_session_and_revenue(self):
        session_id = service.start_session(
            plate="DONE 001",
            car_type="Normal",
            entry_gate_name="ENTRY1",
            arrival_time="2026-09-20T10:00:00",
        )

        service.start_parking(
            session_id=session_id,
            spot_name="S1",
            start_park="2026-09-20T10:01:00",
        )

        service.end_parking(
            session_id=session_id,
            end_park="2026-09-20T10:05:00",
        )

        payment_id = service.create_payment(
            session_id=session_id,
            parking_cost=5.00,
            charging_cost=0.00,
        )

        service.mark_payment_paid(
            payment_id=payment_id,
            paid_time="2026-09-20T10:05:30",
        )

        service.complete_payment(
            payment_id=payment_id,
            completed_time="2026-09-20T10:06:00",
        )

        service.complete_session(
            session_id=session_id,
            exit_gate_name="EXIT_EXIT",
            exit_time="2026-09-20T10:06:00",
        )

        snap = db_source.fetch_db()

        self.assertEqual(
            len(snap["sessions"]),
            1,
        )

        session = snap["sessions"][0]

        self.assertEqual(
            session["plate"],
            "DONE 001",
        )

        self.assertEqual(
            session["spot"],
            "S1",
        )

        self.assertEqual(
            session["charge"],
            5.0,
        )

        self.assertEqual(
            session["status"],
            "Completed - paid",
        )

        self.assertEqual(
            snap["stats"]["revenue"],
            5.0,
        )

        self.assertEqual(
            snap["stats"]["cars_served"],
            1,
        )

    def test_pending_payment_not_counted_as_revenue(self):
        session_id = service.start_session(
            plate="PAY 001",
            car_type="Normal",
            entry_gate_name="ENTRY1",
            arrival_time="2026-09-20T10:00:00",
        )

        service.create_payment(
            session_id=session_id,
            parking_cost=10.0,
            charging_cost=0.0,
        )

        snap = db_source.fetch_db()

        self.assertEqual(
            snap["stats"]["revenue"],
            0,
        )

        self.assertEqual(
            snap["cars"][0]["estimated_charge"],
            10.0,
        )

    def test_gate_state_is_exposed(self):
        service.update_gate_status(
            gate_name="gateA",
            status="OPEN",
            updated_at="2026-09-20T10:00:00",
        )

        snap = db_source.fetch_db()

        self.assertEqual(
            len(snap["gates"]),
            1,
        )

        gate = snap["gates"][0]

        self.assertEqual(
            gate["name"],
            "gateA",
        )

        self.assertEqual(
            gate["state"],
            "open",
        )

        self.assertEqual(
            gate["health"],
            "ok",
        )

    def test_events_are_converted_for_dashboard(self):
        service.log_event({
            "EventId": "event-001",
            "EventClass": "car_spot_action",
            "CarPlateNumber": "ABC 123",
            "ServerDateTime": "2026-09-20T10:00:00",
            "SpotName": "ENTRY1",
            "SpotType": "EntrySpot",
            "Direction": "CarIn",
        })

        snap = db_source.fetch_db()

        self.assertEqual(
            len(snap["events"]),
            1,
        )

        event = snap["events"][0]

        self.assertEqual(
            event["kind"],
            "entry",
        )

        self.assertEqual(
            event["plate"],
            "ABC 123",
        )

        self.assertIn(
            "ABC 123",
            event["text"],
        )

    def test_dashboard_authentication(self):
        service.create_user(
            "admin_test",
            "Admin123!",
            "Admin",
        )

        service.create_user(
            "operator_test",
            "Operator123!",
            "Operator",
        )

        self.assertEqual(
            auth.authenticate(
                "admin_test",
                "Admin123!",
            ),
            "Admin",
        )

        self.assertEqual(
            auth.authenticate(
                "operator_test",
                "Operator123!",
            ),
            "Operator",
        )

        self.assertIsNone(
            auth.authenticate(
                "admin_test",
                "wrong",
            )
        )

        self.assertIsNone(
            auth.authenticate(
                "unknown",
                "password",
            )
        )


class MissingDatabaseTests(unittest.TestCase):

    def test_missing_database_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.db"

            with mock.patch.object(
                database_module,
                "DATABASE_PATH",
                missing,
            ):
                with self.assertRaises(
                    db_source.DatabaseNotFound
                ):
                    db_source.fetch_db()


if __name__ == "__main__":
    unittest.main()
