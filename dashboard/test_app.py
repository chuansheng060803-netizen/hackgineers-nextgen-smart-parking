"""End-to-end tests of the Streamlit page (no browser): sign-in, roles, every period, odd databases."""
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from streamlit.testing.v1 import AppTest

import create_user
import make_demo_db
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from store import Store  # noqa: E402

APP = str(Path(__file__).resolve().parent / "app.py")
_demo_cache = {}


def demo_db(directory):
    """A small (4 day) demo database, made once and copied for each test."""
    if "path" not in _demo_cache:
        holder = tempfile.mkdtemp()
        path = os.path.join(holder, "demo.db")
        make_demo_db.generate(path, days=4, seed=3, end=datetime(2026, 9, 20, 12, 0), per_hour=12)
        _demo_cache["path"] = path
    target = os.path.join(directory, "test.db")
    src = sqlite3.connect(_demo_cache["path"])
    dst = sqlite3.connect(target)
    src.backup(dst)
    src.close()
    dst.close()
    return target


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self._old = os.environ.get("PARKING_DB_PATH")
        self.addCleanup(self.restore_env)

    def restore_env(self):
        if self._old is None:
            os.environ.pop("PARKING_DB_PATH", None)
        else:
            os.environ["PARKING_DB_PATH"] = self._old

    def open_app(self, path, role=None, username="tester"):
        os.environ["PARKING_DB_PATH"] = path
        at = AppTest.from_file(APP, default_timeout=60)
        if role:
            at.session_state["user"] = {"username": username, "role": role}
        return at.run()

    def texts(self, at):
        parts = [m.value for m in at.markdown] + [i.value for i in at.info] + [w.value for w in at.warning] \
                + [e.value for e in at.error] + [c.value for c in at.caption]
        return " ".join(parts)


class TestSignedIn(AppTestCase):
    def test_the_page_draws_without_errors(self):
        at = self.open_app(demo_db(self.dir.name), "operator")
        self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])
        text = self.texts(at)
        for expected in ("Income", "Occupancy now", "Penalties", "Earnings over time", "What to expect next", "Visit history"):
            self.assertIn(expected, text)
        self.assertIn("Demo data", text)

    def test_the_two_roles_are_shown(self):
        db = demo_db(self.dir.name)
        self.assertIn("Operator", self.texts(self.open_app(db, "operator")))
        self.assertIn("Admin (view only)", self.texts(self.open_app(db, "admin")))

    def test_every_period_and_grouping_draws(self):
        db = demo_db(self.dir.name)
        for period in ("Today", "Yesterday", "Last 7 days", "Last 30 days", "This month"):
            for group in ("Auto", "15 minutes", "Hour", "Day", "Month"):
                os.environ["PARKING_DB_PATH"] = db
                at = AppTest.from_file(APP, default_timeout=60)
                at.session_state["user"] = {"username": "t", "role": "admin"}
                at.session_state["period"] = period
                at.session_state["group"] = group
                at.run()
                self.assertEqual(len(at.exception), 0, (period, group, [e.value for e in at.exception]))

    def test_a_custom_period_draws_and_a_backwards_one_is_refused(self):
        db = demo_db(self.dir.name)
        os.environ["PARKING_DB_PATH"] = db
        at = AppTest.from_file(APP, default_timeout=60)
        at.session_state["user"] = {"username": "t", "role": "admin"}
        at.session_state["period"] = "Custom"
        at.run()
        self.assertEqual(len(at.exception), 0)
        at.session_state["h1"], at.session_state["h2"] = 20, 5           # same day: end before start
        at.run()
        self.assertIn("The end must be after the start.", self.texts(at))

    def test_the_forecast_can_switch_to_cars_arriving(self):
        os.environ["PARKING_DB_PATH"] = demo_db(self.dir.name)
        at = AppTest.from_file(APP, default_timeout=60)
        at.session_state["user"] = {"username": "t", "role": "admin"}
        at.session_state["fc_metric"] = "Cars arriving"
        at.run()
        self.assertEqual(len(at.exception), 0)
        self.assertIn("arrivals", self.texts(at))

    def test_signing_out_returns_to_the_sign_in_page(self):
        at = self.open_app(demo_db(self.dir.name), "operator")
        [b for b in at.button if b.key == "signout"][0].click().run()
        self.assertNotIn("user", at.session_state)


class TestSignIn(AppTestCase):
    def setUp(self):
        super().setUp()
        self.db = demo_db(self.dir.name)
        create_user.save_user(self.db, "olivia", "operator-pass-1", "operator")

    def test_the_sign_in_page_shows_when_nobody_is_signed_in(self):
        at = self.open_app(self.db)
        self.assertEqual(len(at.exception), 0)
        self.assertEqual(len(at.text_input), 2)
        self.assertNotIn("Earnings over time", self.texts(at))            # nothing private before sign-in

    def test_wrong_password_is_refused_and_right_one_lets_in(self):
        at = self.open_app(self.db)
        at.text_input(key="signin_name").input("olivia")
        at.text_input(key="signin_password").input("nope-nope")
        at.button[0].click().run(timeout=60)
        self.assertIn("Wrong username or password.", self.texts(at))
        self.assertNotIn("user", at.session_state)
        at.text_input(key="signin_name").input("olivia")
        at.text_input(key="signin_password").input("operator-pass-1")
        at.button[0].click().run(timeout=60)
        self.assertEqual(at.session_state["user"], {"username": "olivia", "role": "operator"})

    def test_no_users_yet_explains_how_to_create_one(self):
        path = os.path.join(self.dir.name, "nousers.db")
        s = Store(path).start()
        s.stop()
        at = self.open_app(path)
        self.assertIn("create_user.py", self.texts(at))


class TestOddDatabases(AppTestCase):
    def test_missing_database_says_so(self):
        at = self.open_app(os.path.join(self.dir.name, "nothing.db"))
        self.assertEqual(len(at.exception), 0)
        self.assertIn("does not exist yet", self.texts(at))

    def test_a_database_with_no_events_yet_is_friendly_not_an_error(self):
        path = os.path.join(self.dir.name, "empty.db")
        s = Store(path).start()
        s.stop()
        at = self.open_app(path, "operator")
        self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])
        self.assertIn("No cars have been recorded yet", self.texts(at))


if __name__ == "__main__":
    unittest.main()
