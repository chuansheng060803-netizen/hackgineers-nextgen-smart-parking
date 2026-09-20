"""The backend's control endpoint: the only way the dashboard can move a gate.

    GET  /api/control/gates                    the mode of every gate
    POST /api/control/gates/<gate>/mode        {"mode": "auto|forced_open|forced_closed",
                                                "username": "..."}
    POST /api/control/gates/<gate>/repair      {"username": "...", "confirm": false}
                                               (confirm must be true to repair a working gate)

Three checks, all on the backend (the dashboard hiding a button is only a courtesy):

  1. the request comes from this same computer (loopback address);
  2. it carries the shared key (header X-Control-Token) - a random key the backend
     creates next to the database (control.token), or CONTROL_TOKEN from the environment.
     The dashboard, running from the same folder, reads the same file;
  3. the username is looked up in app_users HERE and must be an operator. The role is
     never taken from the request.

A refused request answers with a plain message the dashboard shows as it is.
"""
import hmac
import logging
import os
import secrets
import sqlite3
from pathlib import Path

from flask import Blueprint, request

from gate_control import GateController, GateRefused

logger = logging.getLogger(__name__)

TOKEN_HEADER = "X-Control-Token"
LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def token_file(db_path):
    return Path(db_path).with_name("control.token")


def load_token(db_path, create=False):
    """The shared key: CONTROL_TOKEN, else the file beside the database (made if create)."""
    env = os.environ.get("CONTROL_TOKEN")
    if env:
        return env
    path = token_file(db_path)
    try:
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    except OSError:
        pass
    if not create:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_hex(24)
    path.write_text(token + "\n", encoding="utf-8")
    logger.info("Created the gate control key: %s", path)
    return token


def role_of(db_path, username):
    """The role of a dashboard user, straight from app_users; None if there is no such user."""
    try:
        conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT role FROM app_users WHERE username = ?", (username,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return row[0] if row else None


bp = Blueprint("control", __name__)          # registered once, by run_all
_cfg = {"controller": None, "db_path": None, "token": None}


def configure(controller: GateController, db_path, token):
    """Say which controller / database / key the endpoint uses (called by run_all.build)."""
    _cfg.update(controller=controller, db_path=db_path, token=token)


def _refuse_unless_trusted():
    if _cfg["controller"] is None:
        return {"ok": False, "message": "Gate control is not running."}, 503
    if request.remote_addr not in LOOPBACK:
        return {"ok": False, "message": "Gate control is only available from this computer."}, 403
    token = _cfg["token"]
    sent = request.headers.get(TOKEN_HEADER, "")
    if not token or not hmac.compare_digest(sent.encode(), token.encode()):
        return {"ok": False, "message": "Missing or wrong control key."}, 401
    return None


@bp.get("/api/control/gates")
def gates():
    refused = _refuse_unless_trusted()
    if refused:
        return refused
    return {"ok": True, **_cfg["controller"].status()}


@bp.post("/api/control/gates/<path:gate>/mode")
def set_mode(gate):
    refused = _refuse_unless_trusted()
    if refused:
        return refused
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return {"ok": False, "message": "Send JSON: {\"mode\": ..., \"username\": ...}."}, 400
    username = str(body.get("username") or "").strip()
    role = role_of(_cfg["db_path"], username) if username else None
    if role is None:
        return {"ok": False, "message": f"Unknown user {username!r}."}, 403
    try:
        result = _cfg["controller"].set_mode(gate, body.get("mode"), username, role)
    except GateRefused as refusal:
        return {"ok": False, "message": str(refusal)}, refusal.status
    return {"ok": True, **result}


@bp.post("/api/control/gates/<path:gate>/repair")
def repair(gate):
    refused = _refuse_unless_trusted()
    if refused:
        return refused
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return {"ok": False, "message": "Send JSON: {\"username\": ..., \"confirm\": true/false}."}, 400
    username = str(body.get("username") or "").strip()
    role = role_of(_cfg["db_path"], username) if username else None
    if role is None:
        return {"ok": False, "message": f"Unknown user {username!r}."}, 403
    try:
        result = _cfg["controller"].request_repair(gate, username, role, confirm_healthy=body.get("confirm") is True)
    except GateRefused as refusal:
        return {"ok": False, "message": str(refusal)}, refusal.status
    return {"ok": True, **result}
