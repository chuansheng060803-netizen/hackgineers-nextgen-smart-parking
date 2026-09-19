import os
import sqlite3
from pathlib import Path


DATABASE_DIR = Path(__file__).resolve().parent

DATABASE_PATH = Path(
    os.getenv(
        "PARKING_DB_PATH",
        DATABASE_DIR / "parking.db"
    )
)

SCHEMA_PATH = DATABASE_DIR / "schema.sql"


def get_connection():
    """
    Create and return a SQLite database connection.

    Rows can be accessed like dictionaries:
        row["username"]
        row["role"]
    """

    connection = sqlite3.connect(DATABASE_PATH)

    connection.row_factory = sqlite3.Row

    # SQLite does not enable foreign keys automatically.
    connection.execute("PRAGMA foreign_keys = ON")

    return connection


def init_database():
    """
    Create all database tables defined in schema.sql.

    Safe to run more than once because schema.sql uses
    CREATE TABLE IF NOT EXISTS.
    """

    DATABASE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    with open(SCHEMA_PATH, "r", encoding="utf-8") as schema_file:
        schema = schema_file.read()

    connection = get_connection()

    try:
        connection.executescript(schema)
        connection.commit()

    finally:
        connection.close()


if __name__ == "__main__":
    init_database()

    print(f"Database initialized successfully:")
    print(DATABASE_PATH)