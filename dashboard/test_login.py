"""Tests for login.py and create_user.py (passwords are hashed, roles are limited)."""
import os
import sqlite3
import tempfile
import unittest

import create_user
import db_read
import login


class TestPasswords(unittest.TestCase):
    def test_right_password_only(self):
        stored = login.hash_password("correct horse")
        self.assertTrue(login.verify_password("correct horse", stored))
        self.assertFalse(login.verify_password("wrong horse", stored))

    def test_stored_value_is_salted_and_never_the_password(self):
        a, b = login.hash_password("same-password"), login.hash_password("same-password")
        self.assertNotEqual(a, b)
        self.assertNotIn("same-password", a)

    def test_damaged_hash_is_just_a_no(self):
        for bad in ("", "nonsense", "pbkdf2_sha256$x$y$z", None, "md5$1$00$00"):
            self.assertFalse(login.verify_password("x", bad))

    def test_authenticate_gives_the_role_or_nothing(self):
        row = ("ann", login.hash_password("longenough1"), "operator")
        self.assertEqual(login.authenticate(row, "longenough1"), "operator")
        self.assertIsNone(login.authenticate(row, "nope"))
        self.assertIsNone(login.authenticate(None, "anything"))
        self.assertIsNone(login.authenticate(("bob", row[1], "superuser"), "longenough1"))

    def test_only_the_operator_can_control(self):
        self.assertTrue(login.can_control("operator"))
        self.assertFalse(login.can_control("admin"))
        self.assertFalse(login.can_control(None))

    def test_short_passwords_are_refused(self):
        self.assertIsNotNone(login.check_new_password("short"))
        self.assertIsNone(login.check_new_password("long enough"))


class TestCreateUser(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "u.db")
        self.addCleanup(self.dir.cleanup)

    def test_user_is_saved_and_can_sign_in(self):
        create_user.save_user(self.path, "ann", "longenough1", "admin")
        conn = db_read.open_db(self.path)
        self.assertEqual(login.authenticate(db_read.user_row(conn, "ann"), "longenough1"), "admin")
        self.assertTrue(db_read.any_users(conn))

    def test_saving_again_changes_password_and_role_not_the_count(self):
        create_user.save_user(self.path, "ann", "longenough1", "admin")
        create_user.save_user(self.path, "ann", "another-pass1", "operator")
        conn = db_read.open_db(self.path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM app_users").fetchone()[0], 1)
        self.assertEqual(login.authenticate(db_read.user_row(conn, "ann"), "another-pass1"), "operator")
        self.assertIsNone(login.authenticate(db_read.user_row(conn, "ann"), "longenough1"))

    def test_bad_role_or_weak_password_saves_nothing(self):
        with self.assertRaises(ValueError):
            create_user.save_user(self.path, "ann", "longenough1", "root")
        with self.assertRaises(ValueError):
            create_user.save_user(self.path, "ann", "short", "admin")
        self.assertFalse(os.path.exists(self.path))

    def test_the_dashboard_connection_cannot_write(self):
        create_user.save_user(self.path, "ann", "longenough1", "admin")
        conn = db_read.open_db(self.path)
        with self.assertRaises(sqlite3.OperationalError):
            conn.execute("DELETE FROM app_users")

    def test_missing_database_is_reported_clearly(self):
        with self.assertRaises(db_read.DatabaseMissing):
            db_read.open_db(os.path.join(self.dir.name, "nope.db"))


if __name__ == "__main__":
    unittest.main()
