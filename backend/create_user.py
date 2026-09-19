"""Create a dashboard user (Admin or Operator) in the SQLite database.

  python create_user.py --username alice --role Admin

The password is asked for interactively and stored hashed. The database used is
PARKING_DB_PATH if set, otherwise database/parking.db.
"""
import argparse
import getpass
import sqlite3
import sys
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parent.parent))

from database import database as database_module  # noqa: E402
from database.database_service import create_user  # noqa: E402


def main(argv=None, prompt=getpass.getpass):
    parser = argparse.ArgumentParser(description="Create an Admin or Operator user.")
    parser.add_argument("--username", required=True)
    parser.add_argument("--role", required=True, choices=["Admin", "Operator"])
    args = parser.parse_args(argv)

    password = prompt("Password: ")
    if not password:
        print("The password must not be empty.")
        return 1
    if prompt("Repeat password: ") != password:
        print("The passwords do not match.")
        return 1

    database_module.init_database()
    try:
        user_id = create_user(args.username, password, args.role)
    except sqlite3.IntegrityError:
        print(f"The username '{args.username}' already exists.")
        return 1
    print(f"Created {args.role} '{args.username}' (id {user_id}) in {database_module.DATABASE_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
