"""Everything the dashboard reads, straight from the backend's SQLite database.

The dashboard never talks to the simulator and never writes simulator data: the backend
writes, the dashboard reads. This file opens the database read-only and turns the tables
(named exactly as the simulator names them) into pandas tables for biz_metrics.py.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from biz_metrics import to_time

REPO_ROOT = Path(__file__).resolve().parent.parent

FRESH_S = 15          # every list was read this recently: data is live
OFFLINE_S = 120       # nothing read for this long: the backend is not running


class DatabaseMissing(Exception):
    """The database file does not exist yet (the backend has not been started)."""


def default_db_path():
    """The same file the backend writes: PARKING_DB_PATH, or database/parking.db."""
    return os.environ.get("PARKING_DB_PATH") or str(REPO_ROOT / "database" / "parking.db")


def open_db(path=None):
    """Open the database for reading only. Never blocks the backend (WAL)."""
    path = Path(path or default_db_path()).expanduser()
    if not path.exists():
        raise DatabaseMissing(str(path))
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
    except sqlite3.Error:                       # some setups cannot open WAL files read-only
        conn = sqlite3.connect(str(path), timeout=10)
    conn.execute("PRAGMA query_only = ON")      # whatever happens, this connection cannot write
    return conn


def frame(conn, sql, params=()):
    """A query as a DataFrame; a table that does not exist yet gives an empty one."""
    try:
        return pd.read_sql_query(sql, conn, params=params)
    except (pd.errors.DatabaseError, sqlite3.OperationalError):
        return pd.DataFrame()


def scalar(conn, sql, params=()):
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


# ---------------------------------------------------------------------------
# history (money, visits)
# ---------------------------------------------------------------------------

def car_events(conn):
    """Every car_spot_action event (who was where, in and out)."""
    return frame(conn, 'SELECT "SequenceId", "ServerDateTime", "CarPlateNumber", "CarType", '
                       '"SpotName", "SpotType", "Direction" FROM car_spot_action ORDER BY "SequenceId"')


def data_span(conn):
    """(first, last) time found in the events, or None when the database has no events yet."""
    firsts, lasts = [], []
    for table in ("car_spot_action", "payment_made", "penalty", "gate_action"):
        low = scalar(conn, f'SELECT MIN("ServerDateTime") FROM "{table}"')
        high = scalar(conn, f'SELECT MAX("ServerDateTime") FROM "{table}"')
        if low and high:
            firsts.append(low)
            lasts.append(high)
    if not firsts:
        return None
    return to_time(pd.Series([min(firsts)])).iloc[0], to_time(pd.Series([max(lasts)])).iloc[0]


def income(conn):
    """Money earned: time, amount, plate, verified.

    verified=True  : a bill the car logic sent and whose payment it checked and accepted
                     (charges table). Fake payments never appear here.
    verified=False : a payment_made event from BEFORE the backend started recording bills.
                     Those were stored exactly as the simulator sent them, so fake
                     payments could be among them. The dashboard says so.
    """
    paid = frame(conn, "SELECT paid_at AS t, ROUND(billed_total, 2) AS amount, CarPlateNumber AS plate "
                       "FROM charges WHERE status = 'paid'")
    paid["verified"] = True
    cutoff = scalar(conn, "SELECT MIN(billed_at) FROM charges")
    if cutoff:
        earlier = frame(conn, 'SELECT "ServerDateTime" AS t, CAST("Amount" AS REAL) AS amount, '
                              '"CarPlateNumber" AS plate FROM payment_made WHERE "ServerDateTime" < ?',
                        (cutoff,))
    else:
        earlier = frame(conn, 'SELECT "ServerDateTime" AS t, CAST("Amount" AS REAL) AS amount, '
                              '"CarPlateNumber" AS plate FROM payment_made')
    earlier["verified"] = False
    both = pd.concat([earlier, paid], ignore_index=True)
    if both.empty:
        return pd.DataFrame(columns=["time", "amount", "plate", "verified"])
    both["time"] = to_time(both.pop("t"))
    both["amount"] = pd.to_numeric(both["amount"], errors="coerce").fillna(0.0)
    return both.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)


def penalties(conn):
    """Fines the simulator charged us: time, amount, plate (in ComponentName), reason."""
    rows = frame(conn, 'SELECT "ServerDateTime" AS t, CAST("FineAmount" AS REAL) AS amount, '
                       '"ComponentName" AS plate, "Reason" AS reason FROM penalty')
    if rows.empty:
        return pd.DataFrame(columns=["time", "amount", "plate", "reason"])
    rows["time"] = to_time(rows.pop("t"))
    rows["amount"] = pd.to_numeric(rows["amount"], errors="coerce").fillna(0.0)
    return rows.dropna(subset=["time"]).sort_values("time").reset_index(drop=True)


def charges(conn):
    """Every bill we sent (paid or not), for matching against visits."""
    rows = frame(conn, "SELECT CarPlateNumber, billed_total, billed_at, status FROM charges")
    if rows.empty:
        return rows
    rows["billed_at"] = to_time(rows["billed_at"])
    return rows.dropna(subset=["billed_at"])


# ---------------------------------------------------------------------------
# the live picture
# ---------------------------------------------------------------------------

def live_state(conn):
    """The current lists the backend polls from the simulator."""
    parking = frame(conn, "SELECT name, purpose, parkingForCarType, zoneParent, detectedCars, broken, "
                          "isUnderMaintenance FROM list_parking_spots ORDER BY name")
    return {
        "spots": parking,
        "barriers": frame(conn, "SELECT name, state, broken, isUnderMaintenance, updated_at "
                                "FROM list_barriers ORDER BY name"),
        "zones": frame(conn, "SELECT name, gasCarbonMonoxideLevel, risk FROM list_zones ORDER BY name"),
        "lights": frame(conn, 'SELECT name, "group", isOn FROM list_lights ORDER BY name'),
        "alarms": frame(conn, "SELECT name FROM list_alarms"),
        "status": frame(conn, "SELECT isActive, cars FROM status WHERE id = 1"),
    }


def car_park_spots(state):
    """Only the real parking bays (not the entrance / exit spots), in natural order."""
    spots = state["spots"]
    if spots.empty:
        return spots
    bays = spots[spots["purpose"].astype(str).str.lower() == "park"].copy()
    number = bays["name"].str.extract(r"(\d+)$")[0].astype(float)
    return bays.assign(_n=number).sort_values(["_n", "name"]).drop(columns="_n").reset_index(drop=True)


def plates_in_spots(conn):
    """{spot name: plate} for spots whose latest event is a CarIn (a car is there now)."""
    rows = frame(conn, 'SELECT a."SpotName", a."CarPlateNumber", a."Direction" FROM car_spot_action a '
                       'JOIN (SELECT "SpotName" AS n, MAX("SequenceId") AS m FROM car_spot_action '
                       'WHERE "SpotType" = \'Park\' GROUP BY "SpotName") b '
                       'ON a."SpotName" = b.n AND a."SequenceId" = b.m')
    if rows.empty:
        return {}
    inside = rows[rows["Direction"] == "CarIn"]
    return dict(zip(inside["SpotName"], inside["CarPlateNumber"]))


def recent_activity(conn, limit=30):
    """The newest events as short sentences: [(time text, kind, sentence)]."""
    rows = frame(conn, 'SELECT "EventClass", "ServerDateTime", "raw_json" FROM webhook_events '
                       'ORDER BY COALESCE("SequenceId", 0) DESC, "received_at" DESC LIMIT ?', (int(limit),))
    return [(str(r.ServerDateTime or "")[-8:], r.EventClass, describe_event(r.EventClass, r.raw_json))
            for r in rows.itertuples(index=False)]


def describe_event(event_class, raw_json):
    try:
        e = json.loads(raw_json or "{}")
    except ValueError:
        return event_class
    plate = e.get("CarPlateNumber") or e.get("ComponentName") or ""
    if event_class == "car_spot_action":
        where, kind = e.get("SpotName", "?"), e.get("SpotType")
        if kind == "EntrySpot":
            return f"{plate} arrived at the entrance" if e.get("Direction") == "CarIn" else f"{plate} passed the entrance gate"
        if kind == "ExitSpot":
            return f"{plate} reached the exit" if e.get("Direction") == "CarIn" else f"{plate} left the car park"
        return f"{plate} parked in {where}" if e.get("Direction") == "CarIn" else f"{plate} left {where}"
    if event_class == "payment_made":
        return f"Payment of {e.get('Amount', '?')} from {plate}"
    if event_class == "penalty":
        return f"Penalty {e.get('FineAmount', '?')} - {e.get('Reason', '')}".strip(" -")
    if event_class == "gate_action":
        return f"{e.get('Name', 'Gate')} {str(e.get('Action', '')).lower()}"
    if event_class in ("component_broken", "component_fixed"):
        return f"{e.get('ComponentName') or e.get('Name') or 'Component'} " + \
               ("broke down" if event_class == "component_broken" else "was repaired")
    return event_class.replace("_", " ")


def health(conn, now=None):
    """Is the backend feeding the database right now? -> (level, message, seconds since last read).

    level: "live" (every list read in the last 15 s), "delayed", "offline", "empty", or "demo".
    """
    if is_demo(conn):
        return "demo", "Demo data - generated for the presentation, not from the simulator.", 0.0
    polls = frame(conn, "SELECT endpoint, last_ok_at FROM poll_status")
    if polls.empty or polls["last_ok_at"].isna().all():
        return "empty", "No data yet - start the backend (python backend/run_all.py --live).", None
    now = now or datetime.now(timezone.utc)
    newest = pd.to_datetime(polls["last_ok_at"], utc=True, errors="coerce").max()
    oldest = pd.to_datetime(polls["last_ok_at"], utc=True, errors="coerce").min()
    age_newest = (now - newest).total_seconds()
    age_oldest = (now - oldest).total_seconds()
    if age_newest > OFFLINE_S:
        return "offline", f"Backend is not running. Showing what was saved (last update {int(age_newest // 60)} min ago).", age_newest
    if age_oldest > FRESH_S:
        return "delayed", f"Some data is {int(age_oldest)} s old - the backend or simulator may be slow.", age_oldest
    return "live", "Live", age_oldest


def is_demo(conn):
    return scalar(conn, "SELECT value FROM ingest_meta WHERE key = 'demo_data'") == "1"


# ---------------------------------------------------------------------------
# manual control (what operators asked for; the backend does the work)
# ---------------------------------------------------------------------------

def gate_modes(conn):
    """{gate: {"mode", "set_by", "set_at"}} as the backend last saved them (missing = automatic)."""
    rows = frame(conn, "SELECT gate, mode, set_by, set_at FROM gate_control")
    return {r.gate: {"mode": r.mode, "set_by": r.set_by, "set_at": r.set_at} for r in rows.itertuples(index=False)}


def control_log(conn, limit=15):
    """The newest manual commands: at, username, gate, mode (what was asked), result, detail."""
    return frame(conn, "SELECT at, username, gate, mode, result, detail FROM control_log "
                       "ORDER BY log_id DESC LIMIT ?", (int(limit),))


# ---------------------------------------------------------------------------
# sign-in
# ---------------------------------------------------------------------------

def user_row(conn, username):
    """(username, password_hash, role) or None."""
    try:
        return conn.execute("SELECT username, password_hash, role FROM app_users WHERE username = ?",
                            (username,)).fetchone()
    except sqlite3.OperationalError:
        return None


def any_users(conn):
    return bool(scalar(conn, "SELECT COUNT(*) FROM app_users"))
