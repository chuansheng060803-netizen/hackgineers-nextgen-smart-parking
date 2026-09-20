"""The gate control panel and the credit-based penalties, on the whole page (no browser).

A small LIVE database is built with the backend's own Store (fresh poll times, three gates,
some bays, two penalties), so the page shows the live controls. The backend itself is not
running: button presses are caught where the dashboard would send them (control_client._post).
"""
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path

from streamlit.testing.v1 import AppTest

import control_client
import create_user

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from store import Store  # noqa: E402

APP = str(Path(__file__).resolve().parent / "app.py")


def barrier(name, state="Closed", broken=False, maintenance=False):
    return {"name": name, "zoneParent": "ZONE1", "state": state, "broken": broken, "isUnderMaintenance": maintenance}


def live_database(path, gates=None, taken=3, bays=10, broken_bays=0):
    store = Store(path).start()
    gates = gates or [barrier("gateA"), barrier("gateB"), barrier("gateC")]
    spots = [{"name": f"S{i}", "purpose": "Park", "parkingForCarType": "Any", "zoneParent": "ZONE1",
              "detectedCars": 1 if i <= taken else 0, "broken": i > bays - broken_bays, "isUnderMaintenance": False}
             for i in range(1, bays + 1)]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    store.submit_state("list-parking-spots", spots)
    store.submit_state("list-barriers", gates)
    store.submit_state("list-zones", [{"name": "ZONE1", "gasCarbonMonoxideLevel": 0, "risk": "Safe"}])
    for n, plate in enumerate(["AAA 111", "BBB 222"], 1):
        store.submit_event({"EventClass": "penalty", "EventId": f"pen-{n}", "SequenceId": n, "Signature": None,
                            "ServerDateTime": now, "Reason": "Car escaped without paying after parking for some time.",
                            "FineAmount": "10", "Type": "Car", "ComponentName": plate})
    for i in range(1, taken + 1):
        store.submit_event({"EventClass": "car_spot_action", "EventId": f"car-{i}", "SequenceId": 100 + i, "Signature": None,
                            "ServerDateTime": now, "CarPlateNumber": f"CAR {i}", "CarType": "Normal", "SpotName": f"S{i}",
                            "SpotType": "Park", "Direction": "CarIn", "PlannedParkingDurationInMinutes": "2"})
    store.flush()
    store.stop()
    create_user.save_user(path, "ks", "12345678", "operator")
    create_user.save_user(path, "boss", "12345678", "admin")
    return path


class PanelCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = os.path.join(self.dir.name, "live.db")
        self._old = os.environ.get("PARKING_DB_PATH")
        self.addCleanup(lambda: os.environ.pop("PARKING_DB_PATH", None) if self._old is None
                        else os.environ.__setitem__("PARKING_DB_PATH", self._old))
        self.sent = []                                   # what the dashboard tried to send to the backend
        self._post = control_client._post
        control_client._post = lambda db, path, payload: (self.sent.append((path, payload)) or (True, ""))
        self.addCleanup(lambda: setattr(control_client, "_post", self._post))

    def open(self, role, username=None):
        os.environ["PARKING_DB_PATH"] = self.path
        at = AppTest.from_file(APP, default_timeout=60)
        at.session_state["user"] = {"username": username or ("ks" if role == "operator" else "boss"), "role": role}
        return at.run()

    def labels(self, at):
        return [b.label for b in at.button]

    def text(self, at):
        return " ".join([m.value for m in at.markdown] + [c.value for c in at.caption])


class TestControls(PanelCase):
    def test_an_operator_sees_the_buttons_for_every_gate(self):
        live_database(self.path)
        at = self.open("operator")
        self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])
        labels = self.labels(at)
        self.assertEqual(labels.count("Hold open"), 3)
        self.assertEqual(labels.count("Hold closed"), 3)
        self.assertEqual(labels.count("Automatic"), 3)
        self.assertEqual(labels.count("Send to maintenance"), 3)

    def test_an_admin_sees_the_gates_but_no_buttons(self):
        live_database(self.path)
        at = self.open("admin")
        self.assertEqual(len(at.exception), 0)
        self.assertNotIn("Hold open", self.labels(at))
        self.assertIn("view only", self.text(at))
        self.assertIn("gateA", self.text(at))

    def test_pressing_hold_closed_asks_the_backend_with_the_signed_in_user(self):
        live_database(self.path)
        at = self.open("operator")
        at.button(key="close_gateA").click().run()
        self.assertEqual(self.sent, [("/api/control/gates/gateA/mode", {"mode": "forced_closed", "username": "ks"})])
        self.assertIn("gateA is now held closed.", [s.value for s in at.success])

    def test_a_refusal_from_the_backend_is_shown_as_an_error(self):
        live_database(self.path)
        control_client._post = lambda db, path, payload: (False, "gateA is broken. A gate in that state is never operated.")
        at = self.open("operator")
        at.button(key="open_gateA").click().run()
        self.assertIn("gateA is broken. A gate in that state is never operated.", [e.value for e in at.error])

    def test_a_working_gate_needs_a_second_click_before_maintenance(self):
        live_database(self.path)
        at = self.open("operator")
        at.button(key="repair_gateB").click().run()
        self.assertEqual(self.sent, [])                                    # only asked "are you sure?"
        self.assertIn("Yes, send it", self.labels(at))
        at.button(key="repair_yes_gateB").click().run()
        self.assertEqual(self.sent, [("/api/control/gates/gateB/repair", {"username": "ks", "confirm": True})])

    def test_a_broken_gate_offers_repair_and_cannot_be_held(self):
        live_database(self.path, gates=[barrier("gateA", broken=True), barrier("gateB")])
        at = self.open("operator")
        self.assertIn("Request repair", self.labels(at))
        self.assertTrue(at.button(key="open_gateA").disabled)
        self.assertTrue(at.button(key="close_gateA").disabled)
        self.assertFalse(at.button(key="open_gateB").disabled)
        at.button(key="repair_gateA").click().run()
        self.assertEqual(self.sent, [("/api/control/gates/gateA/repair", {"username": "ks", "confirm": False})])

    def test_a_gate_under_maintenance_cannot_be_touched(self):
        live_database(self.path, gates=[barrier("gateA", maintenance=True)])
        at = self.open("operator")
        self.assertTrue(at.button(key="repair_gateA").disabled)
        self.assertTrue(at.button(key="open_gateA").disabled)
        self.assertIn("under maintenance", self.text(at))

    def test_the_saved_order_and_who_gave_it_are_shown_and_its_button_is_off(self):
        live_database(self.path)
        store = Store(self.path).start()
        store.submit_record("gate_control", {"gate": "gateA", "mode": "forced_open", "set_by": "ks", "set_at": "2026-09-20 10:15:00"})
        store.submit_record("control_log", {"log_id": 1, "at": "2026-09-20 10:15:00", "username": "ks", "role": "operator",
                                            "gate": "gateA", "mode": "forced_open", "result": "done", "detail": None})
        store.flush()
        store.stop()
        at = self.open("operator")
        self.assertIn("Held open", self.text(at))
        self.assertIn("by ks at 10:15:00", self.text(at))
        self.assertTrue(at.button(key="open_gateA").disabled)              # already held open
        self.assertFalse(at.button(key="close_gateA").disabled)
        logs = [d.value for d in at.dataframe if "Command" in d.value.columns]
        self.assertEqual(list(logs[0]["Command"]), ["Hold open"])


class TestNumbers(PanelCase):
    def test_occupancy_shows_both_taken_and_available(self):
        live_database(self.path, taken=3, bays=10)
        text = self.text(self.open("admin"))
        self.assertIn('>3</div><div class="k-foot" style="margin:2px 0 0 0">taken', text)
        self.assertIn('>7</div><div class="k-foot" style="margin:2px 0 0 0">available', text)
        self.assertIn("30% of 10 bays taken", text)

    def test_bays_out_of_service_are_not_counted_as_available(self):
        live_database(self.path, taken=3, bays=10, broken_bays=2)
        text = self.text(self.open("admin"))
        self.assertIn('>5</div><div class="k-foot" style="margin:2px 0 0 0">available', text)
        self.assertIn("2 out of service", text)

    def test_penalties_cost_five_credits_each_and_never_reduce_income(self):
        live_database(self.path)
        at = self.open("admin")
        text = self.text(at)
        self.assertIn("-10", text)                                         # 2 errors x 5
        self.assertIn("2 errors &times; 5 credits each", text)
        self.assertIn("2 errors = 10 credits lost", text)
        self.assertNotIn("net after penalties", text)
        tables = [d.value for d in at.dataframe if "Credits lost" in d.value.columns]
        self.assertEqual(list(tables[0]["Credits lost"]), [5, 5])           # 5 each, not the simulator's fine of 10
        self.assertEqual(set(tables[0]["Plate"]), {"AAA 111", "BBB 222"})

    def test_no_errors_says_nothing_was_deducted(self):
        live_database(self.path)
        conn = __import__("sqlite3").connect(self.path)
        conn.execute("DELETE FROM penalty")
        conn.commit()
        conn.close()
        self.assertIn("No errors in this period. Nothing was deducted.", self.text(self.open("admin")))


class TestClient(unittest.TestCase):
    """control_client: the key file, and plain messages when things are missing."""

    def test_no_key_and_no_backend_give_plain_messages_and_never_raise(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "x.db")
            os.environ.pop("CONTROL_TOKEN", None)
            ok, message = control_client.set_mode(db, "gateA", "forced_open", "ks")
            self.assertFalse(ok)
            self.assertIn("control key was not found", message)
            Path(d, "control.token").write_text("abc\n")
            self.assertEqual(control_client.read_token(db), "abc")
            old = os.environ.get("BACKEND_URL")
            os.environ["BACKEND_URL"] = "http://127.0.0.1:1"               # nothing listens here
            try:
                ok, message = control_client.request_repair(db, "gateA", "ks")
            finally:
                os.environ.pop("BACKEND_URL") if old is None else os.environ.__setitem__("BACKEND_URL", old)
            self.assertFalse(ok)
            self.assertIn("not reachable", message)


if __name__ == "__main__":
    unittest.main()
