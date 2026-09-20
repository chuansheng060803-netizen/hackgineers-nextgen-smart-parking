"""Offline tests for CarFlow <-> V2 SQLite integration."""

import json
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
from test_car_flow import PLATE, FlowTestCase, spot


REAL_DB_PATH = Path(
    database_module.DATABASE_PATH
).resolve()


def payments():
    with closing(
        database_module.get_connection()
    ) as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM payments ORDER BY id"
            )
        ]


def spot_row(name):
    return next(
        (
            row
            for row in service.get_parking_spots()
            if row["name"] == name
        ),
        None,
    )


def sim_spot(
    name,
    detected_cars=0,
    zone="ZONE1",
):
    return {
        "name": name,
        "purpose": "Park",
        "parkingForCarType": "Any",
        "zoneParent": zone,
        "detectedCars": detected_cars,
        "broken": False,
        "isUnderMaintenance": False,
    }


class DbTestCase(FlowTestCase):

    def setUp(self):
        super().setUp()

        tmp = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        self.addCleanup(tmp.cleanup)

        path = Path(tmp.name) / "test.db"

        self.assertNotEqual(
            path.resolve(),
            REAL_DB_PATH,
        )

        patcher = mock.patch.object(
            database_module,
            "DATABASE_PATH",
            path,
        )

        patcher.start()
        self.addCleanup(patcher.stop)

        self.adapter = DatabaseAdapter()
        self.adapter.initialize()

        self.client.spots = [
            spot("S1"),
            spot("S2"),
        ]

        # The normal FlowTestCase FakeClient predates
        # the live gate integration.
        #
        # For database tests we do not need to test
        # physical gate timing, so an empty barrier
        # list is enough.
        self.client.list_barriers = lambda: []

        self.flow = self._make_flow()

    def _make_flow(self, valid_payments=False):
        flow = CarFlow(
            self.client,
            amount_check=(
                (lambda event, car: True)
                if valid_payments
                else car_flow.amount_is_valid
            ),
            db=self.adapter,
        )

        # Database integration tests must be deterministic.
        # Do not leave real background timers running.
        flow._schedule_entry_retry = (
            lambda *args, **kwargs: None
        )

        def immediate_charge(car, delay=None):
            flow._delayed_exit_charge(
                car["plate"]
            )

        flow._schedule_exit_charge = (
            immediate_charge
        )

        def immediate_leave(car):
            if car.get("leavepark_sent"):
                return

            self.client.move_car(
                car["plate"],
                "leavepark",
            )

            car["leavepark_sent"] = True
            car["stage"] = car_flow.DONE
            car["exit_release_scheduled"] = False

        flow._leavepark = immediate_leave

        return flow

    def use_valid_payments(self):
        self.flow = self._make_flow(
            valid_payments=True
        )

    def session(self):
        return service.get_parking_session(
            self.car()["session_id"]
        )


class SyncTests(DbTestCase):

    def test_sync_spots_stores_v2_shape(self):
        self.adapter.sync_spots([
            sim_spot("S1"),
            sim_spot(
                "S2",
                detected_cars=2,
            ),
        ])

        s1 = spot_row("S1")
        s2 = spot_row("S2")

        self.assertEqual(
            (
                s1["status"],
                s1["zone"],
            ),
            (
                "FREE",
                "ZONE1",
            ),
        )

        self.assertEqual(
            s2["status"],
            "OCCUPIED",
        )

    def test_second_sync_updates_instead_of_duplicating(self):
        self.adapter.sync_spots([
            sim_spot("S1"),
            sim_spot(
                "S2",
                detected_cars=2,
            ),
        ])

        self.adapter.sync_spots([
            sim_spot("S1"),
            sim_spot("S2"),
        ])

        self.assertEqual(
            len(service.get_parking_spots()),
            2,
        )

        self.assertEqual(
            spot_row("S2")["status"],
            "FREE",
        )

    def test_sync_from_client(self):
        self.adapter.sync(self.client)

        self.assertEqual(
            [
                row["name"]
                for row in service.get_parking_spots()
            ],
            [
                "S1",
                "S2",
            ],
        )


class SessionTests(DbTestCase):

    def test_arrival_creates_active_session(self):
        self.enter()

        sessions = service.get_active_sessions()

        self.assertEqual(
            len(sessions),
            1,
        )

        session = sessions[0]

        self.assertEqual(
            session["id"],
            self.car()["session_id"],
        )

        self.assertEqual(
            session["plate"],
            PLATE,
        )

        self.assertEqual(
            session["car_type"],
            "Normal",
        )

        self.assertEqual(
            session["arrival_time"],
            "2026-09-19T14:00:00",
        )

        self.assertEqual(
            session["status"],
            "ACTIVE",
        )

    def test_spot_is_stored_when_car_reaches_it(self):
        self.enter()

        self.assertIsNone(
            self.session()["spot_name"]
        )

        self.park_in()

        self.assertEqual(
            self.session()["spot_name"],
            "S1",
        )

    def test_park_in_and_out_updates_database(self):
        self.adapter.sync_spots([
            sim_spot("S1"),
            sim_spot("S2"),
        ])

        self.enter()
        self.park_in()

        session = self.session()

        self.assertEqual(
            session["start_park"],
            "2026-09-19T14:00:00",
        )

        s1 = spot_row("S1")

        self.assertEqual(
            (
                s1["status"],
                s1["current_plate"],
            ),
            (
                "OCCUPIED",
                PLATE,
            ),
        )

        self.park_out()

        session = self.session()

        self.assertIsNotNone(
            session["end_park"]
        )

        s1 = spot_row("S1")

        self.assertEqual(
            (
                s1["status"],
                s1["current_plate"],
            ),
            (
                "FREE",
                None,
            ),
        )

    def test_exit_completes_session(self):
        self.use_valid_payments()

        self.to_charging()
        self.payment()

        self.assertEqual(
            self.session()["status"],
            "ACTIVE",
        )

        session_id = self.car()["session_id"]

        self.leave_exit()

        session = service.get_parking_session(
            session_id
        )

        self.assertEqual(
            session["status"],
            "COMPLETED",
        )

        self.assertEqual(
            session["exit_time"],
            "2026-09-19T14:10:00",
        )


class PaymentTests(DbTestCase):

    def test_charge_is_recorded_as_pending(self):
        self.to_charging()

        rows = payments()

        self.assertEqual(
            len(rows),
            1,
        )

        row = rows[0]

        self.assertEqual(
            row["session_id"],
            self.car()["session_id"],
        )

        self.assertEqual(
            row["id"],
            self.car()["payment_id"],
        )

        self.assertEqual(
            (
                row["parking_cost"],
                row["charging_cost"],
            ),
            (
                10.0,
                0,
            ),
        )

        self.assertEqual(
            row["status"],
            "PENDING",
        )

        self.assertIsNone(
            row["paid_time"]
        )

    def test_wrong_payment_remains_pending(self):
        self.to_charging()

        self.payment(
            amount=3.0
        )

        self.assertEqual(
            payments()[0]["status"],
            "PENDING",
        )

    def test_valid_payment_marks_paid(self):
        self.use_valid_payments()

        self.to_charging()
        self.payment()

        rows = payments()

        self.assertEqual(
            len(rows),
            1,
        )

        self.assertEqual(
            rows[0]["status"],
            "PAID",
        )

        self.assertIsNotNone(
            rows[0]["paid_time"]
        )

    def test_unknown_car_payment_changes_nothing(self):
        self.use_valid_payments()

        self.payment("ZZZ 999")

        self.assertEqual(
            payments(),
            [],
        )

    def test_payment_before_charge_changes_nothing(self):
        self.use_valid_payments()

        self.enter()
        self.park_in()
        self.payment()

        self.assertEqual(
            payments(),
            [],
        )

    def test_electric_charge_amounts_stored(self):
        self.to_charging(
            "EV 123",
            "Electric",
        )

        row = payments()[0]

        self.assertEqual(
            (
                row["parking_cost"],
                row["charging_cost"],
            ),
            (
                10.0,
                10.0,
            ),
        )


class EventLogTests(DbTestCase):

    def test_events_are_logged_in_v2_shape(self):
        self.enter()

        events = service.get_recent_events()

        self.assertEqual(
            len(events),
            1,
        )

        event = events[0]

        self.assertEqual(
            event["event_class"],
            "car_spot_action",
        )

        self.assertEqual(
            event["plate"],
            PLATE,
        )

        raw = json.loads(
            event["raw_data"]
        )

        self.assertEqual(
            raw["SpotName"],
            "ENTRY1",
        )

        self.assertEqual(
            raw["Direction"],
            "CarIn",
        )

    def test_duplicate_event_is_logged_once(self):
        event = self.event(
            SpotName="ENTRY1",
            SpotType="EntrySpot",
            Direction="CarIn",
            CarPlateNumber=PLATE,
            CarType="Normal",
        )

        self.flow.handle_event(event)
        self.flow.handle_event(dict(event))

        self.assertEqual(
            len(service.get_recent_events()),
            1,
        )

    def test_payment_event_is_logged(self):
        self.to_charging()
        self.payment()

        self.assertEqual(
            service.get_recent_events()[0][
                "event_class"
            ],
            "payment_made",
        )


class FailureIsolationTests(DbTestCase):

    def test_database_errors_never_break_flow(self):

        class BrokenAdapter:

            def __getattr__(self, name):

                def fail(*args, **kwargs):
                    raise sqlite3.Error(
                        f"{name} failed"
                    )

                return fail

        self.flow = self._make_flow(valid_payments=True)
        self.flow.db = BrokenAdapter()

        with self.assertLogs(
            "car_flow",
            "ERROR",
        ):
            self.to_charging()
            self.payment()

        self.assertTrue(
            self.client.calls
        )

    def test_without_db_writes_nothing(self):
        self.flow = self._make_flow()
        self.flow.db = None

        self.assertIsNone(
            self.flow.db
        )

        self.to_charging()

        self.assertEqual(
            service.get_active_sessions(),
            [],
        )

        self.assertEqual(
            payments(),
            [],
        )


if __name__ == "__main__":
    unittest.main()
