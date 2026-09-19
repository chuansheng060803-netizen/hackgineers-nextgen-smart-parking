import sqlite3
from pathlib import Path


DATABASE_PATH = Path(__file__).parent / "parking.db"


def get_connection():
    connection = sqlite3.connect(DATABASE_PATH)

    # Allows rows to behave like dictionaries
    connection.row_factory = sqlite3.Row

    return connection