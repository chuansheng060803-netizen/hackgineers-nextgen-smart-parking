"""Offline tests for the dashboard's database mode.

Runs the real CarFlow + DatabaseAdapter (fake simulator client) against a temporary
SQLite file, then reads it back the way the dashboard does. No network, no simulator,
and the real database/parking.db is never touched.

  cd dashboard
  python -B -m unittest test_db_source
"""
import ast
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

DASHBOARD_DIR = Path(__file__).resolve().parent
BACKEND_DIR = DASHBOARD_DIR.parent / "backend"
sys.path.append(str(BACKEND_DIR))

import alerts  # noqa: E402
import auth  # noqa: E402
import create_user as create_user_cli  # noqa: E402
import data_source  # noqa: E402
import db_source  # noqa: E402
import ui  # noqa: E402
import database.database as database_module  # noqa: E402
from database import database_service as service  # noqa: E402
from car_flow import CarFlow  # noqa: E402
from test_car_flow import PLATE, spot  # noqa: E402
from test_database_integration import DbTestCase, sim_spot  # noqa: E402


class SnapshotTestCase(DbTestCase):
    def setUp(self):
        super().setUp()
        self.adapter.sync_spots([sim_spot("S1"), sim_spot("S2")])

    def snap(self):
        return db_source.fetch_db()

    def car_row(self, snap, plate=PLATE):
        return next(c for c in snap["cars"] if c["plate"] == plate)

    def spot_row(self, snap, name):
        return next(s for s in snap["spots"] if s["name"] == name)


class EmptyAndMissingTests(SnapshotTestCase):
    def test_missing_database_raises_and_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nope.db"
            with mock.patch.object(database_module, "DATABASE_PATH", missing):
                with self.assertRaises(db_source.DatabaseNotFound):
                    db_source.fetch_db()
            self.assertFalse(missing.exists())

    def test_empty_database_gives_valid_empty_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(database_module, "DATABASE_PATH", Path(tmp) / "empty.db"):
                self.adapter.initialize()
                snap = data_source.fetch_db()
        self.assertEqual((snap["spots"], snap["cars"], snap["sessions"], snap["gates"]), ([], [], [], []))
        self.assertEqual(snap["generated_at"], "")
        self.assertEqual(snap["source"], "database")

    def test_data_source_accepts_db_mode(self):
        with mock.patch.dict(os.environ, {"DASHBOARD_SOURCE": "db"}):
            self.assertEqual(data_source.mode(), "db")


class SpotTests(SnapshotTestCase):
    def test_spot_states_zone_type_and_only_parking_spots(self):
        self.adapter.sync_spots([
            sim_spot("S3", detected_cars=2),
            sim_spot("S4", broken=True),
            {**sim_spot("S5"), "isUnderMaintenance": True},
            {**sim_spot("ENTRY1"), "purpose": "EntrySpot"},
            sim_spot("S10"),
        ])
        snap = self.snap()
        self.assertEqual([s["name"] for s in snap["spots"]], ["S1", "S2", "S3", "S4", "S5", "S10"])
        states = {s["name"]: s["state"] for s in snap["spots"]}
        self.assertEqual(states, {"S1": "available", "S2": "available", "S3": "occupied",
                                  "S4": "broken", "S5": "maintenance", "S10": "available"})
        self.assertEqual(self.spot_row(snap, "S1")["zone"], "ZONE1")
        self.assertEqual(self.spot_row(snap, "S1")["type"], "Any")


class LifecycleTests(SnapshotTestCase):
    def test_arrival_marks_spot_reserved_and_car_heading_to_spot(self):
        self.enter()
        snap = self.snap()
        self.assertEqual(self.car_row(snap)["status"], "Heading to spot")
        self.assertEqual(self.car_row(snap)["spot"], "S1")
        self.assertEqual(self.spot_row(snap, "S1")["state"], "reserved")
        self.assertEqual(self.spot_row(snap, "S1")["car"], PLATE)
        self.assertEqual(snap["generated_at"], "2026-09-19 14:00:00")
        self.assertEqual(snap["events"][0]["kind"], "entry")
        self.assertIn(PLATE, snap["events"][0]["text"])

    def test_parking_marks_spot_occupied_then_free_when_it_leaves_the_spot(self):
        self.enter()
        self.park_in()
        snap = self.snap()
        self.assertEqual(self.car_row(snap)["status"], "Parked")
        self.assertEqual((self.spot_row(snap, "S1")["state"], self.spot_row(snap, "S1")["car"]), ("occupied", PLATE))
        self.assertEqual(snap["events"][0]["kind"], "park")

        self.park_out()
        snap = self.snap()
        self.assertEqual(self.spot_row(snap, "S1")["state"], "available")
        self.assertEqual(len(snap["cars"]), 1)  # still inside, on its way out

    def test_pending_payment_shows_estimate_but_no_income(self):
        self.to_charging()
        self.payment()  # default check is fail-closed: stays pending
        snap = self.snap()
        self.assertEqual(self.car_row(snap)["estimated_charge"], 10.0)
        self.assertEqual(snap["stats"]["revenue"], 0.0)

        self.leave_exit()
        snap = self.snap()
        self.assertEqual(snap["cars"], [])
        self.assertEqual(snap["sessions"][0]["status"], "Completed - payment pending")
        self.assertEqual((snap["sessions"][0]["charge"], snap["stats"]["revenue"]), (10.0, 0.0))

    def test_paid_completed_session_appears_in_history_and_income(self):
        self.use_valid_payments()
        self.to_charging()
        self.payment()
        self.leave_exit()
        snap = self.snap()
        self.assertEqual(snap["sessions"], [{
            "plate": PLATE, "car_type": "Normal", "spot": "S1",
            "entered_at": "2026-09-19 14:00:00", "left_at": "2026-09-19 14:10:00",
            "minutes": 10.0, "charge": 10.0, "status": "Completed - paid"}])
        self.assertEqual((snap["stats"]["revenue"], snap["stats"]["cars_served"]), (10.0, 1))

    def test_arrivals_are_bucketed_per_five_minutes(self):
        self.enter()
        arrivals = self.snap()["history"]["arrivals"]
        self.assertEqual(len(arrivals), 12)
        self.assertEqual(arrivals[-1], {"t": "2026-09-19T14:00:00", "count": 1})

    def test_event_text_uses_documented_fields_only(self):
        self.to_charging()
        self.payment(amount=10.0)
        self.flow.handle_event(self.event(EventClass="penalty"))
        events = self.snap()["events"]
        self.assertEqual(events[0]["kind"], "penalty")
        self.assertEqual(events[0]["text"], "Penalty event reported")
        self.assertEqual(events[1]["kind"], "payment")
        self.assertIn("10.0", events[1]["text"])
        self.assertEqual(self.snap()["stats"]["penalty_count"], 1)


class GateTests(SnapshotTestCase):
    def test_no_gates_when_the_table_is_empty(self):
        self.assertEqual(self.snap()["gates"], [])

    def test_gates_come_only_from_the_database_without_invented_roles(self):
        service.upsert_gate("gateA", state="Open")
        service.upsert_gate("gateB", state="Closed", broken=True)
        service.upsert_gate("gateC", state="Closed", under_maintenance=True)
        gates = {g["name"]: g for g in self.snap()["gates"]}
        self.assertEqual(set(gates), {"gateA", "gateB", "gateC"})
        self.assertEqual([gates[n]["health"] for n in ("gateA", "gateB", "gateC")], ["ok", "broken", "maintenance"])
        self.assertEqual(gates["gateA"]["state"], "Open")
        self.assertTrue(all(g["role"] == "" for g in gates.values()))


class RenderingTests(SnapshotTestCase):
    def test_snapshot_works_with_normalise_alerts_and_ui(self):
        self.use_valid_payments()
        self.to_charging()
        self.payment()
        service.upsert_gate("gateA", state="Open")
        snap = data_source.normalise(db_source.fetch_db())
        found = alerts.derive_alerts(snap)
        html = [
            ui.header("T", "sub", snap["source"], snap["generated_at"]),
            ui.alerts_block(found),
            ui.kpis(snap["spots"], snap["cars"], snap["stats"]),
            ui.parking_map(snap["spots"]),
            ui.gates_block(snap["gates"], snap["fans"]),
            ui.system_block(snap["source"], True, snap["generated_at"], snap["spots"], snap["gates"], snap["fans"], found),
            ui.co_block(snap["zones"]),
            ui.activity_feed(snap["events"]),
        ]
        self.assertTrue(all(isinstance(h, str) and h for h in html))
        for key in ("spots", "cars", "events", "sessions", "archive", "gates", "fans", "zones", "penalties", "history", "stats"):
            self.assertIn(key, snap)

    def test_dashboard_modules_do_not_import_simulator_or_network_code(self):
        forbidden = {"requests", "simulator_api", "payment_logic", "parking_algorithm", "webhook"}
        for module in (db_source, auth):
            tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
            imported = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module.split(".")[0])
            self.assertFalse(imported & forbidden, f"{module.__name__}: {imported & forbidden}")


class AuthTests(DbTestCase):
    def test_login_roles_and_failures(self):
        service.create_user("alice", "Secret123!", "Admin")
        service.create_user("bob", "Secret456!", "Operator")
        service.create_user("carol", "Secret789!", "Operator")
        with closing(database_module.get_connection()) as connection:
            connection.execute("UPDATE users SET is_active = 0 WHERE username = 'carol'")
            connection.commit()

        self.assertEqual(auth.authenticate("alice", "Secret123!"), "Admin")
        self.assertEqual(auth.authenticate("bob", "Secret456!"), "Operator")
        self.assertIsNone(auth.authenticate("alice", "wrong"))
        self.assertIsNone(auth.authenticate("nobody", "x"))
        self.assertIsNone(auth.authenticate("carol", "Secret789!"))  # inactive
        self.assertIsNone(auth.authenticate("", ""))

    def test_create_user_command(self):
        self.assertEqual(create_user_cli.main(["--username", "dave", "--role", "Operator"], prompt=lambda _: "pw12345"), 0)
        self.assertEqual(auth.authenticate("dave", "pw12345"), "Operator")
        # duplicate username and mismatching passwords are refused
        self.assertEqual(create_user_cli.main(["--username", "dave", "--role", "Admin"], prompt=lambda _: "pw12345"), 1)
        answers = iter(["one", "two"])
        self.assertEqual(create_user_cli.main(["--username", "erin", "--role", "Admin"], prompt=lambda _: next(answers)), 1)
        self.assertIsNone(service.get_user_by_username("erin"))
        with self.assertRaises(SystemExit):
            create_user_cli.main(["--username", "x", "--role", "Viewer"], prompt=lambda _: "pw")


try:
    from streamlit.testing.v1 import AppTest
except ImportError:  # pragma: no cover
    AppTest = None


@unittest.skipIf(AppTest is None, "streamlit is not installed")
class AppSmokeTests(DbTestCase):
    """Runs dashboard/app.py itself. requests.request is patched to raise (see FlowTestCase)."""

    def run_app(self, env, role=None):
        with mock.patch.dict(os.environ, env):
            at = AppTest.from_file(str(DASHBOARD_DIR / "app.py"), default_timeout=60)
            if role:
                at.session_state["role"], at.session_state["user"] = role, "tester"
            at.run()
        return at

    def test_db_mode_requires_sign_in(self):
        at = self.run_app({"DASHBOARD_SOURCE": "db"})
        self.assertFalse(at.exception)
        self.assertTrue(any("Sign in" in i.value for i in at.info))

    def test_db_mode_signed_in_shows_data_without_control_buttons(self):
        self.enter()
        self.park_in()
        at = self.run_app({"DASHBOARD_SOURCE": "db"}, role="Admin")
        self.assertFalse(at.exception)
        self.assertFalse(any(b.key and b.key.startswith("c_") for b in at.button))  # no gate/fan controls
        self.assertTrue(any("awaiting live gate synchronisation" in c.value for c in at.caption))

    def test_db_mode_missing_database_shows_banner_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(database_module, "DATABASE_PATH", Path(tmp) / "nope.db"):
                at = self.run_app({"DASHBOARD_SOURCE": "db"}, role="Operator")
        self.assertFalse(at.exception)
        self.assertTrue(any("Database file not found" in e.value for e in at.error))

    def test_mock_mode_still_runs_independently(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("DASHBOARD_SOURCE", None)
            at = AppTest.from_file(str(DASHBOARD_DIR / "app.py"), default_timeout=60)
            at.run()
        self.assertFalse(at.exception)


if __name__ == "__main__":
    unittest.main()
