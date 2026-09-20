"""Create or update a dashboard user (admin or operator).

    python dashboard/create_user.py --username alice --role operator
    python dashboard/create_user.py --list

The password is asked for on the keyboard (not shown, not kept in your shell history).
For scripts only: set NEW_USER_PASSWORD instead. Works on the same database the backend
writes (PARKING_DB_PATH, or database/parking.db); the backend can be running.
"""
import argparse
import getpass
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import db_read  # noqa: E402
import login  # noqa: E402

SCHEMA = db_read.REPO_ROOT / "database" / "app_schema.sql"


def save_user(db_path, username, password, role):
    if role not in login.ROLES:
        raise ValueError(f"role must be one of {login.ROLES}")
    problem = login.check_new_password(password)
    if problem:
        raise ValueError(problem)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15)
    try:
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO app_users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash, "
            "role = excluded.role",
            (username, login.hash_password(password), role, datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--username")
    parser.add_argument("--role", choices=login.ROLES)
    parser.add_argument("--list", action="store_true", help="show the existing users")
    parser.add_argument("--db", default=db_read.default_db_path())
    args = parser.parse_args()

    if args.list:
        conn = db_read.open_db(args.db)
        for name, role in conn.execute("SELECT username, role FROM app_users ORDER BY username"):
            print(f"{name}  ({role})")
        return
    if not args.username or not args.role:
        parser.error("--username and --role are required (or use --list)")
    password = os.environ.get("NEW_USER_PASSWORD") or getpass.getpass("Password: ")
    if not os.environ.get("NEW_USER_PASSWORD") and getpass.getpass("Repeat password: ") != password:
        sys.exit("The two passwords are different.")
    try:
        save_user(args.db, args.username.strip(), password, args.role)
    except ValueError as problem:
        sys.exit(str(problem))
    print(f"Saved {args.username} as {args.role} in {args.db}")


if __name__ == "__main__":
    main()
