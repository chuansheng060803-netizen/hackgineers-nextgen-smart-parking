"""The dashboard's only way to move something: ask the backend, which decides.

The dashboard never talks to the simulator and never changes gates itself. A button sends
a small request to the backend (POST /api/control/...), which
  * checks the request comes from this computer and carries the shared key,
  * looks the user up in the database and refuses anyone who is not an operator,
  * and only then sends the command (or refuses, with a reason we show as it is).

The shared key is CONTROL_TOKEN, or the file `control.token` the backend creates next to
the database. BACKEND_URL says where the backend listens (default http://127.0.0.1:5000).
"""
import os
from pathlib import Path

import requests

TOKEN_HEADER = "X-Control-Token"
TIMEOUT_S = 10
MODE_TEXT = {"auto": "back on automatic", "forced_open": "held open", "forced_closed": "held closed"}


def backend_url():
    return os.environ.get("BACKEND_URL", "http://127.0.0.1:5000").rstrip("/")


def read_token(db_path):
    env = os.environ.get("CONTROL_TOKEN")
    if env:
        return env
    try:
        return (Path(db_path).with_name("control.token").read_text(encoding="utf-8").strip()) or None
    except OSError:
        return None


def _post(db_path, path, payload):
    """POST to the backend. Returns (ok, message); never raises."""
    token = read_token(db_path)
    if not token:
        return False, ("The control key was not found. Start the backend once "
                       "(python backend/run_all.py --live); it creates the key. Nothing was changed.")
    try:
        response = requests.post(backend_url() + path, json=payload, headers={TOKEN_HEADER: token}, timeout=TIMEOUT_S)
    except requests.RequestException:
        return False, "The backend is not reachable, so nothing was changed. Is run_all.py running?"
    try:
        body = response.json()
    except ValueError:
        return False, f"The backend answered with an error ({response.status_code}). Nothing was changed."
    return bool(body.get("ok")), body.get("message", "")


def set_mode(db_path, gate, mode, username):
    """Hold a gate open / closed, or give it back to the car logic (mode "auto")."""
    ok, message = _post(db_path, f"/api/control/gates/{gate}/mode", {"mode": mode, "username": username})
    return ok, (f"{gate} is now {MODE_TEXT.get(mode, mode)}." if ok else message)


def request_repair(db_path, gate, username, confirm=False):
    """Ask for a gate to be repaired. A working gate is only sent if confirm is True."""
    ok, message = _post(db_path, f"/api/control/gates/{gate}/repair", {"username": username, "confirm": bool(confirm)})
    return ok, (f"Repair requested for {gate}." if ok else message)
