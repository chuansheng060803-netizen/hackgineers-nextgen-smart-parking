import json
import hashlib
import hmac
import secrets

from datetime import datetime, timezone

from database.database import get_connection


def _now():
    """Return the current UTC time as an ISO formatted string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# =========================================================
def hash_password(password):
    """
    Convert a plain password into a salted secure hash.
    The plain password is never stored in the database.
    """
    salt = secrets.token_bytes(16)
    iterations = 200_000

    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )

    return (
        f"pbkdf2_sha256$"
        f"{iterations}$"
        f"{salt.hex()}$"
        f"{password_hash.hex()}"
    )


def verify_password(password, stored_hash):
    """
    Check a plain password against the stored password hash.
    """
    try:
        algorithm, iterations, salt_hex, hash_hex = stored_hash.split("$")

        if algorithm != "pbkdf2_sha256":
            return False

        calculated_hash = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )

        return hmac.compare_digest(
            calculated_hash.hex(),
            hash_hex,
        )

    except (ValueError, TypeError):
        return False
# =========================================================

def create_user(username, password, role):
    if role not in ("Admin", "Operator"):
        raise ValueError("Role must be 'Admin' or 'Operator'")

    password_hash = hash_password(password)

    connection = get_connection()

    try:
        cursor = connection.execute(
            """
            INSERT INTO users (
                username,
                password_hash,
                role
            )
            VALUES (?, ?, ?)
            """,
            (
                username,
                password_hash,
                role,
            ),
        )

        connection.commit()
        return cursor.lastrowid

    finally:
        connection.close()


def get_user_by_username(username):
    connection = get_connection()

    try:
        row = connection.execute(
            """
            SELECT *
            FROM users
            WHERE username = ?
            """,
            (username,),
        ).fetchone()

        return dict(row) if row else None

    finally:
        connection.close()

def authenticate_user(username, password):
    """
    Authenticate a user using username and password.

    Returns the user dictionary if authentication succeeds.
    Returns None if authentication fails.
    """
    user = get_user_by_username(username)

    if user is None:
        return None

    if not user["is_active"]:
        return None

    if not verify_password(password, user["password_hash"]):
        return None

    return user

def has_role(user, required_role):
    """
    Check whether an authenticated user has a specific role.
    """
    if user is None:
        return False

    return user["role"] == required_role

# =========================================================
# PARKING SPOTS
# =========================================================

def upsert_parking_spot(
    name,
    zone=None,
    purpose=None,
    parking_for_car_type=None,
    status="available",
    current_car=None,
    broken=False,
    under_maintenance=False,
):
    connection = get_connection()

    try:
        connection.execute(
            """
            INSERT INTO parking_spots (
                name,
                zone,
                purpose,
                parking_for_car_type,
                status,
                current_car,
                broken,
                under_maintenance,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)

            ON CONFLICT(name)
            DO UPDATE SET
                zone = excluded.zone,
                purpose = excluded.purpose,
                parking_for_car_type = excluded.parking_for_car_type,
                status = excluded.status,
                current_car = excluded.current_car,
                broken = excluded.broken,
                under_maintenance = excluded.under_maintenance,
                updated_at = excluded.updated_at
            """,
            (
                name,
                zone,
                purpose,
                parking_for_car_type,
                status,
                current_car,
                int(broken),
                int(under_maintenance),
                _now(),
            ),
        )

        connection.commit()

    finally:
        connection.close()


def get_parking_spots():
    connection = get_connection()

    try:
        rows = connection.execute(
            """
            SELECT *
            FROM parking_spots
            ORDER BY name
            """
        ).fetchall()

        return [dict(row) for row in rows]

    finally:
        connection.close()


# =========================================================
# GATES
# =========================================================

def upsert_gate(
    name,
    state="Closed",
    broken=False,
    under_maintenance=False,
):
    connection = get_connection()

    try:
        connection.execute(
            """
            INSERT INTO gates (
                name,
                state,
                broken,
                under_maintenance,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?)

            ON CONFLICT(name)
            DO UPDATE SET
                state = excluded.state,
                broken = excluded.broken,
                under_maintenance = excluded.under_maintenance,
                updated_at = excluded.updated_at
            """,
            (
                name,
                state,
                int(broken),
                int(under_maintenance),
                _now(),
            ),
        )

        connection.commit()

    finally:
        connection.close()


def get_gates():
    connection = get_connection()

    try:
        rows = connection.execute(
            """
            SELECT *
            FROM gates
            ORDER BY name
            """
        ).fetchall()

        return [dict(row) for row in rows]

    finally:
        connection.close()


# =========================================================
# PARKING SESSIONS
# =========================================================

def start_parking_session(
    car_name,
    car_type=None,
    spot_name=None,
    arrival_time=None,
):
    if arrival_time is None:
        arrival_time = _now()

    connection = get_connection()

    try:
        cursor = connection.execute(
            """
            INSERT INTO parking_sessions (
                car_name,
                car_type,
                spot_name,
                arrival_time,
                status
            )
            VALUES (?, ?, ?, ?, 'active')
            """,
            (
                car_name,
                car_type,
                spot_name,
                arrival_time,
            ),
        )

        connection.commit()
        return cursor.lastrowid

    finally:
        connection.close()


def mark_car_parked(session_id, parked_time=None):
    if parked_time is None:
        parked_time = _now()

    connection = get_connection()

    try:
        connection.execute(
            """
            UPDATE parking_sessions
            SET parked_time = ?
            WHERE id = ?
            """,
            (
                parked_time,
                session_id,
            ),
        )

        connection.commit()

    finally:
        connection.close()


def assign_session_spot(session_id, spot_name):
    connection = get_connection()

    try:
        connection.execute(
            """
            UPDATE parking_sessions
            SET spot_name = ?
            WHERE id = ?
            """,
            (
                spot_name,
                session_id,
            ),
        )

        connection.commit()

    finally:
        connection.close()


def end_parking_session(session_id, departure_time=None):
    if departure_time is None:
        departure_time = _now()

    connection = get_connection()

    try:
        connection.execute(
            """
            UPDATE parking_sessions
            SET
                departure_time = ?,
                status = 'completed'
            WHERE id = ?
            """,
            (
                departure_time,
                session_id,
            ),
        )

        connection.commit()

    finally:
        connection.close()


def get_active_sessions():
    connection = get_connection()

    try:
        rows = connection.execute(
            """
            SELECT *
            FROM parking_sessions
            WHERE status = 'active'
            ORDER BY arrival_time DESC
            """
        ).fetchall()

        return [dict(row) for row in rows]

    finally:
        connection.close()

def get_parking_session(session_id):
    connection = get_connection()

    try:
        row = connection.execute(
            """
            SELECT *
            FROM parking_sessions
            WHERE id = ?
            """,
            (session_id,),
        ).fetchone()

        return dict(row) if row else None

    finally:
        connection.close()


def get_sessions(status=None, limit=500):
    """Newest first. status: 'active', 'completed' or None for all."""
    query = "SELECT * FROM parking_sessions"
    params = []

    if status is not None:
        query += " WHERE status = ?"
        params.append(status)

    query += " ORDER BY arrival_time DESC, id DESC LIMIT ?"
    params.append(limit)

    connection = get_connection()

    try:
        rows = connection.execute(query, params).fetchall()

        return [dict(row) for row in rows]

    finally:
        connection.close()


# =========================================================
# PAYMENTS
# =========================================================

def record_payment(
    session_id,
    parking_cost,
    charging_cost,
    status="paid",
    paid_at=None,
):
    if paid_at is None and status == "paid":
        paid_at = _now()

    connection = get_connection()

    try:
        cursor = connection.execute(
            """
            INSERT INTO payments (
                session_id,
                parking_cost,
                charging_cost,
                status,
                paid_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                session_id,
                parking_cost,
                charging_cost,
                status,
                paid_at,
            ),
        )

        connection.commit()
        return cursor.lastrowid

    finally:
        connection.close()


def mark_payment_paid(payment_id, paid_at=None):
    if paid_at is None:
        paid_at = _now()

    connection = get_connection()

    try:
        connection.execute(
            """
            UPDATE payments
            SET
                status = 'paid',
                paid_at = ?
            WHERE id = ?
            """,
            (
                paid_at,
                payment_id,
            ),
        )

        connection.commit()

    finally:
        connection.close()


def get_payments(limit=2000):
    """Newest first."""
    connection = get_connection()

    try:
        rows = connection.execute(
            """
            SELECT *
            FROM payments
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        return [dict(row) for row in rows]

    finally:
        connection.close()


# =========================================================
# EVENTS
# =========================================================

def log_event(
    event_type,
    payload,
    car_name=None,
    component_name=None,
):
    payload_json = json.dumps(payload)

    connection = get_connection()

    try:
        cursor = connection.execute(
            """
            INSERT INTO events (
                event_type,
                car_name,
                component_name,
                payload_json,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                event_type,
                car_name,
                component_name,
                payload_json,
                _now(),
            ),
        )

        connection.commit()
        return cursor.lastrowid

    finally:
        connection.close()


def get_recent_events(limit=100):
    connection = get_connection()

    try:
        rows = connection.execute(
            """
            SELECT *
            FROM events
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        events = []

        for row in rows:
            event = dict(row)

            try:
                event["payload"] = json.loads(
                    event.pop("payload_json")
                )
            except json.JSONDecodeError:
                event["payload"] = {}

            events.append(event)

        return events

    finally:
        connection.close()