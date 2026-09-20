import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import database.database as database_module
from database import database_service as service


class AuthenticationTests(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        test_db = Path(self.temp_dir.name) / "test_auth.db"

        self.db_patch = mock.patch.object(
            database_module,
            "DATABASE_PATH",
            test_db,
        )

        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)

        database_module.init_database()

    def test_admin_login(self):
        user_id = service.create_user(
            "admin_test",
            "Admin123!",
            "Admin",
        )

        self.assertIsNotNone(user_id)

        user = service.authenticate_user(
            "admin_test",
            "Admin123!",
        )

        self.assertIsNotNone(user)
        self.assertEqual(
            user["username"],
            "admin_test",
        )
        self.assertEqual(
            user["role"],
            "ADMIN",
        )

    def test_operator_login(self):
        service.create_user(
            "operator_test",
            "Operator123!",
            "Operator",
        )

        user = service.authenticate_user(
            "operator_test",
            "Operator123!",
        )

        self.assertIsNotNone(user)
        self.assertEqual(
            user["role"],
            "OPERATOR",
        )

    def test_wrong_password_is_rejected(self):
        service.create_user(
            "admin_test",
            "Admin123!",
            "Admin",
        )

        user = service.authenticate_user(
            "admin_test",
            "wrongpassword",
        )

        self.assertIsNone(user)

    def test_unknown_user_is_rejected(self):
        user = service.authenticate_user(
            "does_not_exist",
            "password",
        )

        self.assertIsNone(user)

    def test_role_checks(self):
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

        admin = service.authenticate_user(
            "admin_test",
            "Admin123!",
        )

        operator = service.authenticate_user(
            "operator_test",
            "Operator123!",
        )

        self.assertTrue(
            service.has_role(admin, "Admin")
        )

        self.assertFalse(
            service.has_role(admin, "Operator")
        )

        self.assertTrue(
            service.has_role(operator, "Operator")
        )

        self.assertFalse(
            service.has_role(operator, "Admin")
        )

    def test_password_is_hashed(self):
        service.create_user(
            "admin_test",
            "Admin123!",
            "Admin",
        )

        user = service.get_user_by_username(
            "admin_test"
        )

        self.assertNotEqual(
            user["password_hash"],
            "Admin123!",
        )

        self.assertTrue(
            user["password_hash"].startswith(
                "pbkdf2_sha256$"
            )
        )

    def test_duplicate_username_is_rejected(self):
        service.create_user(
            "admin_test",
            "Admin123!",
            "Admin",
        )

        with self.assertRaises(
            sqlite3.IntegrityError
        ):
            service.create_user(
                "admin_test",
                "Different123!",
                "Operator",
            )

    def test_invalid_role_is_rejected(self):
        with self.assertRaises(ValueError):
            service.create_user(
                "viewer_test",
                "Password123!",
                "Viewer",
            )


if __name__ == "__main__":
    unittest.main()
