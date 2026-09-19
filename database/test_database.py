import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import database.database as database_module
from database import database_service as service


class DatabaseServiceTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        test_db = Path(self.temp_dir.name) / "test_database.db"

        self.db_patch = mock.patch.object(
            database_module,
            "DATABASE_PATH",
            test_db,
        )

        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)

        database_module.init_database()

    def test_spot_lifecycle(self):
        service.add_or_update_spot(
            name="S1",
            zone="ZONE1",
            status="FREE",
            updated_at="2026-09-20T10:00:00",
        )

        spots = service.get_parking_spots()

        self.assertEqual(len(spots), 1)
        self.assertEqual(spots[0]["name"], "S1")
        self.assertEqual(spots[0]["status"], "FREE")

        service.set_spot_occupied(
            spot_name="S1",
            plate="ABC 123",
            updated_at="2026-09-20T10:01:00",
        )

        spot = service.get_parking_spots()[0]

        self.assertEqual(
            spot["status"],
            "OCCUPIED",
        )

        self.assertEqual(
            spot["current_plate"],
            "ABC 123",
        )

        service.set_spot_available(
            spot_name="S1",
            updated_at="2026-09-20T10:02:00",
        )

        spot = service.get_parking_spots()[0]

        self.assertEqual(
            spot["status"],
            "FREE",
        )

        self.assertIsNone(
            spot["current_plate"]
        )

    def test_gate_state(self):
        service.update_gate_status(
            gate_name="gateA",
            status="CLOSED",
            updated_at="2026-09-20T10:00:00",
        )

        gates = service.get_gates()

        self.assertEqual(len(gates), 1)
        self.assertEqual(
            gates[0]["name"],
            "gateA",
        )
        self.assertEqual(
            gates[0]["status"],
            "CLOSED",
        )

        service.update_gate_status(
            gate_name="gateA",
            status="OPEN",
            updated_at="2026-09-20T10:01:00",
        )

        gates = service.get_gates()

        self.assertEqual(len(gates), 1)
        self.assertEqual(
            gates[0]["status"],
            "OPEN",
        )

    def test_session_lifecycle(self):
        session_id = service.start_session(
            plate="ABC 123",
            car_type="Normal",
            entry_gate_name="ENTRY1",
            arrival_time="2026-09-20T10:00:00",
        )

        session = service.get_parking_session(
            session_id
        )

        self.assertEqual(
            session["plate"],
            "ABC 123",
        )

        self.assertEqual(
            session["status"],
            "ACTIVE",
        )

        self.assertEqual(
            len(service.get_active_sessions()),
            1,
        )

        service.start_parking(
            session_id=session_id,
            spot_name="S1",
            start_park="2026-09-20T10:01:00",
        )

        session = service.get_parking_session(
            session_id
        )

        self.assertEqual(
            session["spot_name"],
            "S1",
        )

        self.assertEqual(
            session["start_park"],
            "2026-09-20T10:01:00",
        )

        service.end_parking(
            session_id=session_id,
            end_park="2026-09-20T10:05:00",
        )

        session = service.get_parking_session(
            session_id
        )

        self.assertEqual(
            session["end_park"],
            "2026-09-20T10:05:00",
        )

        service.complete_session(
            session_id=session_id,
            exit_gate_name="EXIT_EXIT",
            exit_time="2026-09-20T10:06:00",
        )

        session = service.get_parking_session(
            session_id
        )

        self.assertEqual(
            session["status"],
            "COMPLETED",
        )

        self.assertEqual(
            session["exit_gate_name"],
            "EXIT_EXIT",
        )

        self.assertEqual(
            session["exit_time"],
            "2026-09-20T10:06:00",
        )

        self.assertEqual(
            len(service.get_active_sessions()),
            0,
        )

        self.assertEqual(
            len(service.get_sessions("COMPLETED")),
            1,
        )

    def test_payment_lifecycle(self):
        session_id = service.start_session(
            plate="PAY 001",
            car_type="Normal",
            entry_gate_name="ENTRY1",
            arrival_time="2026-09-20T10:00:00",
        )

        payment_id = service.create_payment(
            session_id=session_id,
            parking_cost=5.50,
            charging_cost=2.00,
        )

        payment = service.get_payments()[0]

        self.assertEqual(
            payment["id"],
            payment_id,
        )

        self.assertEqual(
            payment["status"],
            "PENDING",
        )

        self.assertEqual(
            payment["parking_cost"],
            5.50,
        )

        self.assertEqual(
            payment["charging_cost"],
            2.00,
        )

        service.mark_payment_paid(
            payment_id=payment_id,
            paid_time="2026-09-20T10:05:00",
        )

        payment = service.get_payments()[0]

        self.assertEqual(
            payment["status"],
            "PAID",
        )

        self.assertEqual(
            payment["paid_time"],
            "2026-09-20T10:05:00",
        )

        service.complete_payment(
            payment_id=payment_id,
            completed_time="2026-09-20T10:06:00",
        )

        payment = service.get_payments()[0]

        self.assertEqual(
            payment["status"],
            "COMPLETED",
        )

        self.assertEqual(
            payment["completed_time"],
            "2026-09-20T10:06:00",
        )

    def test_event_logging(self):
        event = {
            "EventId": "event-001",
            "EventClass": "car_spot_action",
            "CarPlateNumber": "ABC 123",
            "ServerDateTime": "2026-09-20T10:00:00",
            "SpotName": "S1",
            "Direction": "CarIn",
        }

        service.log_event(event)

        events = service.get_recent_events()

        self.assertEqual(len(events), 1)

        stored = events[0]

        self.assertEqual(
            stored["event_id"],
            "event-001",
        )

        self.assertEqual(
            stored["event_class"],
            "car_spot_action",
        )

        self.assertEqual(
            stored["plate"],
            "ABC 123",
        )

        raw = json.loads(
            stored["raw_data"]
        )

        self.assertEqual(
            raw["SpotName"],
            "S1",
        )

        self.assertEqual(
            raw["Direction"],
            "CarIn",
        )

    def test_get_sessions_returns_all_sessions(self):
        service.start_session(
            "CAR 1",
            "Normal",
            "ENTRY1",
            "2026-09-20T10:00:00",
        )

        service.start_session(
            "CAR 2",
            "Normal",
            "ENTRY1",
            "2026-09-20T10:01:00",
        )

        sessions = service.get_sessions()

        self.assertEqual(
            len(sessions),
            2,
        )


if __name__ == "__main__":
    unittest.main()
