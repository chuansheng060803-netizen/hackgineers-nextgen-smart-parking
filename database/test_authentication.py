"""Manual authentication test. Writes to its own scratch database.

Run it from the repo root:  python -B -m database.test_authentication
"""
import os
from pathlib import Path

# Never write test users into the real parking.db (it holds live password
# hashes). Set PARKING_DB_PATH yourself to override.
os.environ.setdefault(
    "PARKING_DB_PATH",
    str(Path(__file__).resolve().parent / "test_scratch.db"),
)

from database.database import init_database, get_connection  # noqa: E402
from database.database_service import (
    create_user,
    authenticate_user,
    has_role,
)


init_database()


# -------------------------------------------------
# Remove old test users so this test can be rerun
# -------------------------------------------------

connection = get_connection()

try:
    connection.execute(
        "DELETE FROM users WHERE username IN (?, ?)",
        ("admin_test", "operator_test"),
    )
    connection.commit()
finally:
    connection.close()


# -------------------------------------------------
# Create test users
# -------------------------------------------------

admin_id = create_user(
    username="admin_test",
    password="Admin123!",
    role="Admin",
)

operator_id = create_user(
    username="operator_test",
    password="Operator123!",
    role="Operator",
)

print("Created Admin:", admin_id)
print("Created Operator:", operator_id)


# -------------------------------------------------
# Test 1: Correct Admin login
# -------------------------------------------------

admin = authenticate_user(
    "admin_test",
    "Admin123!",
)

assert admin is not None
assert admin["role"] == "Admin"

print("✅ Correct Admin login passed")


# -------------------------------------------------
# Test 2: Wrong Admin password
# -------------------------------------------------

wrong_password = authenticate_user(
    "admin_test",
    "wrongpassword",
)

assert wrong_password is None

print("✅ Wrong password rejected")


# -------------------------------------------------
# Test 3: Correct Operator login
# -------------------------------------------------

operator = authenticate_user(
    "operator_test",
    "Operator123!",
)

assert operator is not None
assert operator["role"] == "Operator"

print("✅ Correct Operator login passed")


# -------------------------------------------------
# Test 4: Unknown username
# -------------------------------------------------

unknown_user = authenticate_user(
    "does_not_exist",
    "password",
)

assert unknown_user is None

print("✅ Unknown username rejected")


# -------------------------------------------------
# Test 5: Role checks
# -------------------------------------------------

assert has_role(admin, "Admin") is True
assert has_role(admin, "Operator") is False

assert has_role(operator, "Operator") is True
assert has_role(operator, "Admin") is False

print("✅ Admin / Operator role checks passed")


print("\nALL AUTHENTICATION TESTS PASSED ✅")