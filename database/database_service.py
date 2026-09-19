from database.database import get_connection
import hashlib
import hmac
import json
import secrets

def start_session(
    plate,
    car_type,
    entry_gate_name,
    arrival_time
):
    connection = get_connection()

    cursor = connection.execute(
        """
        INSERT INTO parking_sessions (
            plate,
            car_type,
            entry_gate_name,
            arrival_time,
            status
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            plate,
            car_type,
            entry_gate_name,
            arrival_time,
            "ACTIVE"
        )
    )

    connection.commit()

    session_id = cursor.lastrowid

    connection.close()

    return session_id


def start_parking(
    session_id,
    spot_name,
    start_park
):
    connection = get_connection()

    connection.execute(
        """
        UPDATE parking_sessions
        SET
            spot_name = ?,
            start_park = ?
        WHERE id = ?
        """,
        (
            spot_name,
            start_park,
            session_id
        )
    )

    connection.commit()
    connection.close()


def end_parking(
    session_id,
    end_park
):
    connection = get_connection()

    connection.execute(
        """
        UPDATE parking_sessions
        SET end_park = ?
        WHERE id = ?
        """,
        (
            end_park,
            session_id
        )
    )

    connection.commit()
    connection.close()


def complete_session(
    session_id,
    exit_gate_name,
    exit_time
):
    connection = get_connection()

    connection.execute(
        """
        UPDATE parking_sessions
        SET
            exit_gate_name = ?,
            exit_time = ?,
            status = ?
        WHERE id = ?
        """,
        (
            exit_gate_name,
            exit_time,
            "COMPLETED",
            session_id
        )
    )

    connection.commit()
    connection.close()

def create_payment(
    session_id,
    parking_cost,
    charging_cost
):
    connection = get_connection()

    cursor = connection.execute(
        """
        INSERT INTO payments (
            session_id,
            parking_cost,
            charging_cost,
            status
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            session_id,
            parking_cost,
            charging_cost,
            "PENDING"
        )
    )

    connection.commit()

    payment_id = cursor.lastrowid

    connection.close()

    return payment_id

def mark_payment_paid(
    payment_id,
    paid_time
):
    connection = get_connection()

    connection.execute(
        """
        UPDATE payments
        SET
            status = ?,
            paid_time = ?
        WHERE id = ?
        """,
        (
            "PAID",
            paid_time,
            payment_id
        )
    )

    connection.commit()
    connection.close()

def complete_payment(
    payment_id,
    completed_time
):
    connection = get_connection()

    connection.execute(
        """
        UPDATE payments
        SET
            status = ?,
            completed_time = ?
        WHERE id = ?
        """,
        (
            "COMPLETED",
            completed_time,
            payment_id
        )
    )

    connection.commit()
    connection.close()

def add_or_update_spot(
    name,
    zone,
    status,
    updated_at
):
    connection = get_connection()

    connection.execute(
        """
        INSERT INTO parking_spots (
            name,
            zone,
            status,
            updated_at
        )
        VALUES (?, ?, ?, ?)

        ON CONFLICT(name)
        DO UPDATE SET
            zone = excluded.zone,
            status = excluded.status,
            updated_at = excluded.updated_at
        """,
        (
            name,
            zone,
            status,
            updated_at
        )
    )

    connection.commit()
    connection.close()

def set_spot_occupied(
    spot_name,
    plate,
    updated_at
):
    connection = get_connection()

    connection.execute(
        """
        UPDATE parking_spots
        SET
            status = ?,
            current_plate = ?,
            updated_at = ?
        WHERE name = ?
        """,
        (
            "OCCUPIED",
            plate,
            updated_at,
            spot_name
        )
    )

    connection.commit()
    connection.close()

def set_spot_available(
    spot_name,
    updated_at
):
    connection = get_connection()

    connection.execute(
        """
        UPDATE parking_spots
        SET
            status = ?,
            current_plate = NULL,
            updated_at = ?
        WHERE name = ?
        """,
        (
            "FREE",
            updated_at,
            spot_name
        )
    )

    connection.commit()
    connection.close()

def update_gate_status(
    gate_name,
    status,
    updated_at
):
    connection = get_connection()

    connection.execute(
        """
        INSERT INTO gates (
            name,
            status,
            updated_at
        )
        VALUES (?, ?, ?)

        ON CONFLICT(name)
        DO UPDATE SET
            status = excluded.status,
            updated_at = excluded.updated_at
        """,
        (
            gate_name,
            status,
            updated_at
        )
    )

    connection.commit()
    connection.close()

def log_event(event):
    connection = get_connection()

    connection.execute(
        """
        INSERT INTO events (
            event_id,
            event_class,
            plate,
            event_time,
            raw_data
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            event.get("EventId"),
            event.get("EventClass"),
            event.get("CarPlateNumber"),
            event.get("ServerDateTime"),
            json.dumps(event)
        )
    )

    connection.commit()
    connection.close()

PBKDF2_ITERATIONS = 200_000


def normalize_role(role):
    role = str(role).strip().upper()

    if role not in ("ADMIN", "OPERATOR"):
        raise ValueError("Role must be ADMIN or OPERATOR")

    return role


def hash_password(password):
    if not password:
        raise ValueError("Password must not be empty")

    salt = secrets.token_hex(16)

    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt),
        PBKDF2_ITERATIONS
    ).hex()

    return (
        f"pbkdf2_sha256$"
        f"{PBKDF2_ITERATIONS}$"
        f"{salt}$"
        f"{password_hash}"
    )


def verify_password(password, stored_hash):
    if not password or not stored_hash:
        return False

    try:
        algorithm, iterations, salt, expected_hash = stored_hash.split("$")

        if algorithm != "pbkdf2_sha256":
            return False

        calculated_hash = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt),
            int(iterations)
        ).hex()

        return hmac.compare_digest(
            calculated_hash,
            expected_hash
        )

    except (ValueError, TypeError):
        return False


def store_user(username, password_hash, role):
    role = normalize_role(role)

    connection = get_connection()

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
            role
        )
    )

    connection.commit()

    user_id = cursor.lastrowid

    connection.close()

    return user_id

def create_user(username, password, role):
    username = str(username).strip()

    if not username:
        raise ValueError("Username must not be empty")

    if not password:
        raise ValueError("Password must not be empty")

    role = normalize_role(role)

    password_hash = hash_password(password)

    return store_user(
        username,
        password_hash,
        role
    )


def get_user_by_username(username):
    connection = get_connection()

    user = connection.execute(
        """
        SELECT
            id,
            username,
            password_hash,
            role
        FROM users
        WHERE username = ?
        """,
        (username,)
    ).fetchone()

    connection.close()

    return user


def authenticate_user(username, password):
    user = get_user_by_username(username)

    if user is None:
        return None

    if not verify_password(
        password,
        user["password_hash"]
    ):
        return None

    return user


def has_role(user, role):
    if user is None:
        return False

    try:
        required_role = normalize_role(role)
    except ValueError:
        return False

    return user["role"] == required_role


def get_parking_session(session_id):
    connection = get_connection()

    row = connection.execute(
        """
        SELECT *
        FROM parking_sessions
        WHERE id = ?
        """,
        (session_id,)
    ).fetchone()

    connection.close()

    return dict(row) if row else None


def get_sessions(status=None):
    connection = get_connection()

    if status is None:
        rows = connection.execute(
            """
            SELECT *
            FROM parking_sessions
            ORDER BY id DESC
            """
        ).fetchall()

    else:
        status = str(status).strip().upper()

        rows = connection.execute(
            """
            SELECT *
            FROM parking_sessions
            WHERE status = ?
            ORDER BY id DESC
            """,
            (status,)
        ).fetchall()

    connection.close()

    return [dict(row) for row in rows]


def get_active_sessions():
    return get_sessions("ACTIVE")


def get_payments():
    connection = get_connection()

    rows = connection.execute(
        """
        SELECT *
        FROM payments
        ORDER BY id DESC
        """
    ).fetchall()

    connection.close()

    return [dict(row) for row in rows]


def get_parking_spots():
    connection = get_connection()

    rows = connection.execute(
        """
        SELECT *
        FROM parking_spots
        ORDER BY name
        """
    ).fetchall()

    connection.close()

    return [dict(row) for row in rows]


def get_gates():
    connection = get_connection()

    rows = connection.execute(
        """
        SELECT *
        FROM gates
        ORDER BY name
        """
    ).fetchall()

    connection.close()

    return [dict(row) for row in rows]


def get_recent_events(limit=100):
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 100

    limit = max(1, min(limit, 1000))

    connection = get_connection()

    rows = connection.execute(
        """
        SELECT *
        FROM events
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,)
    ).fetchall()

    connection.close()

    return [dict(row) for row in rows]
