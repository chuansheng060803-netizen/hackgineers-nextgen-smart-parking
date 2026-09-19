from database.database import get_connection
import json

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

def store_user(username, password_hash, role):
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