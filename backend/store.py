"""The one and only writer of simulator data.

Everything the simulator tells us ends up in the SQLite database through this
class, using the simulator's own table and field names (see
database/simulator_schema.sql):

    webhook events ──┐
                     ├──▶ queue ──▶ ONE writer thread ──▶ SQLite (WAL)
    REST poll results┘

Why one writer: SQLite lets one connection write at a time. With a single
thread that owns the connection there are no "database is locked" collisions,
and callers (the webhook request, the poller) never wait for the disk: they only
put a job on the queue.

What gets written:
  * a webhook event is stored once. EventId is the primary key, so an event
    that arrives twice is ignored; events that arrive out of order are stored as
    they come (order them by SequenceId when reading).
  * a REST answer is compared with what we already hold. Only items that
    changed are written, and each changed field is logged in state_changes.
    Asking again and getting the same answer writes nothing.
  * poll_status records when each endpoint was last read successfully, so the
    dashboard can say how old its data is.

Nothing here talks to the simulator, and nothing here throws into the caller:
a bad payload is logged and skipped, never allowed to stop the writer.
"""
import hashlib
import json
import logging
import queue
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = _REPO_ROOT / "database" / "simulator_schema.sql"

BATCH_SIZE = 200              # jobs written per transaction
POLL_STATUS_EVERY_S = 2.0     # freshness rows are refreshed at most this often


# ---------------------------------------------------------------------------
# What the simulator gives us, by its own names
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StateTable:
    """One REST endpoint and the table that holds its current state."""
    table: str
    key: str                    # column that identifies an item
    columns: tuple              # simulator field names, key first
    single: bool = False        # the endpoint returns one object, not a list
    delete_missing: bool = False  # items that vanish from the answer are removed


STATE_TABLES = {
    "list-parking-spots": StateTable(
        "list_parking_spots", "name",
        ("name", "purpose", "parkingForCarType", "zoneParent",
         "detectedCars", "broken", "isUnderMaintenance")),
    "list-barriers": StateTable(
        "list_barriers", "name",
        ("name", "zoneParent", "state", "broken", "isUnderMaintenance")),
    "list-zones": StateTable(
        "list_zones", "name",
        ("name", "gasCarbonMonoxideLevel", "risk")),
    "list-lights": StateTable(
        "list_lights", "name",
        ("name", "group", "zoneParent", "isOn")),
    "list-alarms": StateTable(
        "list_alarms", "name", ("name",), delete_missing=True),
    "status": StateTable(
        "status", "id", ("id", "isActive", "cars"), single=True),
}

# One table per webhook EventClass -> the fields it has besides the common ones
# (EventId, SequenceId, ServerDateTime). Confirmed against ~7,500 real events.
# component_broken / component_fixed / carbon_monoxide_event never occur in
# Level 1, so only their raw_json is kept.
EVENT_TABLES = {
    "car_spot_action": ("CarPlateNumber", "CarType", "SpotName", "SpotType",
                        "Direction", "PlannedParkingDurationInMinutes"),
    "payment_made": ("CarPlateNumber", "Amount", "Reason"),
    "penalty": ("Reason", "FineAmount", "Type", "ComponentName"),
    "gate_action": ("Name", "Action"),
    "component_broken": (),
    "component_fixed": (),
    "carbon_monoxide_event": (),
}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _default_db_path():
    """The same file the rest of the project uses (PARKING_DB_PATH or database/parking.db)."""
    if str(_REPO_ROOT) not in sys.path:
        sys.path.append(str(_REPO_ROOT))
    from database.database import DATABASE_PATH
    return DATABASE_PATH


def _sql_value(value):
    """A simulator value as SQLite can hold it: booleans become 0/1, lists become JSON text."""
    if isinstance(value, bool):
        return int(value)
    if value is None or isinstance(value, (int, float, str)):
        return value
    return json.dumps(value, sort_keys=True)


def _text(value):
    return None if value is None else str(value)


def _as_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _hash(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def items_from_response(spec, data):
    """The list of item dicts inside a REST answer.

    Raises ValueError for an answer that cannot be right (None, text, an object
    where a list is expected), so the caller records a poll error instead of
    treating "nothing" as "everything is gone".
    """
    if spec.single:
        if isinstance(data, dict):
            return [data]
        raise ValueError(f"expected an object, got {type(data).__name__}")
    if isinstance(data, list):
        return data
    # A dict here is most likely an error body ({"error": ...}); never read it as
    # "the list is empty", which would look like every item had vanished.
    raise ValueError(f"expected a list, got {type(data).__name__}")


# ---------------------------------------------------------------------------
# the writer
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, db_path=None, batch_size=BATCH_SIZE):
        self.db_path = str(db_path if db_path is not None else _default_db_path())
        self.batch_size = batch_size
        self._queue = queue.Queue()
        self._thread = None
        self._stopped = False   # set by stop(): later submissions are refused loudly, not queued for nothing
        self._lock = threading.Lock()
        # Owned by the writer thread once it is running.
        self._cache = {}        # (table, key) -> tuple of column values + raw_json
        self._poll = {}         # endpoint -> counters for poll_status
        self.stats = {"events_saved": 0, "events_duplicate": 0, "events_bad": 0,
                      "state_rows_changed": 0, "state_polls": 0, "poll_errors": 0,
                      "job_errors": 0}

    # ---- callers' side: these never block on the database and never raise ----

    def start(self):
        """Start the writer thread (safe to call twice)."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            self._stopped = False
            ready = threading.Event()
            self._thread = threading.Thread(
                target=self._run, args=(ready,), name="sim-store-writer", daemon=True)
            self._thread.start()
        if not ready.wait(timeout=15):
            raise RuntimeError("database writer did not start")
        return self

    def stop(self, timeout=15):
        """Write everything still queued, then stop the thread."""
        thread = self._thread
        self._stopped = True
        if thread is None or not thread.is_alive():
            return
        self._queue.put(("stop",))
        thread.join(timeout)

    def flush(self, timeout=15):
        """Wait until every job queued so far has been written. True if it did."""
        thread = self._thread
        if self._stopped or thread is None or not thread.is_alive():
            return False
        done = threading.Event()
        self._queue.put(("flush", done))
        return done.wait(timeout)

    def backlog(self):
        """Jobs waiting for the writer."""
        return self._queue.qsize()

    def is_alive(self):
        """Is the writer thread running? (a dead writer means nothing is being saved)"""
        return self._thread is not None and self._thread.is_alive()

    def _put(self, job):
        if self._stopped:
            logger.warning("Store is stopped; %s not saved", job[0])
            return
        self._queue.put(job)

    def submit_event(self, event, received_at=None):
        """Queue one webhook event (a dict, or a list of dicts)."""
        try:
            events = event if isinstance(event, list) else [event]
            when = received_at or _now()
            for one in events:
                if isinstance(one, dict):
                    self._put(("event", one, when))
                else:
                    self.stats["events_bad"] += 1
                    logger.warning("Webhook body is not an object, ignored: %.120r", one)
        except Exception:
            logger.exception("submit_event failed")

    def submit_state(self, endpoint, data, duration_ms=None):
        """Queue the answer of one REST GET (endpoint like "list-parking-spots").

        An answer that cannot be right is recorded as a poll error instead.
        """
        spec = STATE_TABLES.get(endpoint)
        if spec is None:
            logger.warning("submit_state: unknown endpoint %r", endpoint)
            return
        try:
            items = items_from_response(spec, data)
        except ValueError as exc:
            self.submit_poll_error(endpoint, exc, duration_ms)
            return
        self._put(("state", endpoint, items, _now(), duration_ms))

    def submit_poll_error(self, endpoint, error, duration_ms=None):
        """Queue "this endpoint could not be read" so the dashboard can show stale data as stale."""
        self._put(("poll_error", endpoint, str(error)[:500], _now(), duration_ms))

    # ---- the writer thread ----

    def _connect(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=15, isolation_level=None)
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]   # readers (the dashboard) never block the writer
        if str(mode).lower() != "wal":
            logger.warning("Database is in %r mode, not WAL (another program has it open?); "
                           "the dashboard may briefly block writes", mode)
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        return conn

    def _load_cache(self, conn):
        """What we already hold, so a restart does not log everything as new."""
        self._cache.clear()
        for spec in STATE_TABLES.values():
            cols = ", ".join(f'"{c}"' for c in spec.columns)
            for row in conn.execute(f'SELECT {cols}, raw_json FROM "{spec.table}"'):
                self._cache[(spec.table, row[0])] = tuple(row)

    def _run(self, ready):
        try:
            conn = self._connect()
            self._load_cache(conn)
            self._meta(conn, "writer_started_at", _now())
        except Exception:
            logger.exception("Database writer could not start")
            return
        ready.set()
        stopping = False
        while not stopping:
            jobs = [self._queue.get()]
            while len(jobs) < self.batch_size:
                try:
                    jobs.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            markers = [j for j in jobs if j[0] in ("flush", "stop")]
            work = [j for j in jobs if j[0] not in ("flush", "stop")]
            stopping = any(j[0] == "stop" for j in markers)
            try:
                self._write(conn, work)
            except Exception:                    # nothing may kill this thread
                logger.exception("Database writer error")
            finally:
                for marker in markers:           # a flush() is always answered
                    if marker[0] == "flush":
                        marker[1].set()
        conn.close()

    def _write(self, conn, work):
        """Write a batch in one transaction; if that fails, try again, then job by job."""
        if not work:
            return
        for attempt in (1, 2, 3):
            try:
                self._write_batch(conn, work)
                return
            except Exception:
                logger.exception("Database batch failed (attempt %d of 3); rolled back", attempt)
                self._recover(conn)
                time.sleep(0.2 * attempt)
        for job in work:                         # one poisonous job must not take the others down
            try:
                self._write_batch(conn, [job])
            except Exception:
                self.stats["job_errors"] += 1
                logger.exception("Database job %s dropped", job[0])
                self._recover(conn)

    def _write_batch(self, conn, work):
        # IMMEDIATE takes the write lock now (and waits for it), so another program
        # committing in between can never make a later statement fail half way.
        conn.execute("BEGIN IMMEDIATE")
        for job in work:
            self._apply_safely(conn, job)
        conn.execute("COMMIT")

    def _recover(self, conn):
        """After a failure: drop any open transaction and make the cache match the file again."""
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        try:
            self._load_cache(conn)
        except Exception:
            logger.exception("Could not reload the cache; carrying on with what was loaded")

    def _apply_safely(self, conn, job):
        """One bad job is skipped; it never spoils the rest of the batch."""
        conn.execute("SAVEPOINT job")
        try:
            kind = job[0]
            if kind == "event":
                self._apply_event(conn, job[1], job[2])
            elif kind == "state":
                self._apply_state(conn, *job[1:])
            elif kind == "poll_error":
                self._apply_poll_error(conn, *job[1:])
            conn.execute("RELEASE job")
        except Exception:
            self.stats["job_errors"] += 1
            logger.exception("Database job %s failed; skipped", job[0])
            conn.execute("ROLLBACK TO job")
            conn.execute("RELEASE job")
            self._load_cache(conn)      # the cache must match what is really in the database

    # ---- webhook events ----

    def _apply_event(self, conn, event, received_at):
        raw = json.dumps(event, ensure_ascii=False)
        event_class = str(event.get("EventClass") or "unknown")
        event_id = event.get("EventId") or f"noid-{_hash(raw)}"
        sequence = _as_int(event.get("SequenceId"))
        server_time = _text(event.get("ServerDateTime"))

        cursor = conn.execute(
            'INSERT OR IGNORE INTO webhook_events '
            '("EventId","EventClass","SequenceId","Signature","ServerDateTime",received_at,raw_json) '
            'VALUES (?,?,?,?,?,?,?)',
            (event_id, event_class, sequence, _text(event.get("Signature")),
             server_time, received_at, raw))
        if cursor.rowcount == 0:
            self.stats["events_duplicate"] += 1
            logger.info("Duplicate event %s ignored", event_id)
            return

        fields = EVENT_TABLES.get(event_class)
        if fields is not None:                       # table name comes from our own dict, never from the payload
            columns = ("EventId", "SequenceId", "ServerDateTime") + fields
            values = [event_id, sequence, server_time] + [_sql_value(event.get(f)) for f in fields]
            names = ", ".join(f'"{c}"' for c in columns)
            marks = ", ".join("?" * (len(columns) + 1))
            conn.execute(
                f'INSERT OR IGNORE INTO "{event_class}" ({names}, raw_json) VALUES ({marks})',
                values + [raw])
        else:
            logger.info("EventClass %r has no table of its own; kept in webhook_events", event_class)

        self.stats["events_saved"] += 1
        self._meta(conn, "last_event_at", received_at)

    # ---- REST state ----

    def _item_key(self, spec, item, raw):
        if spec.single:
            return 1
        key = item.get(spec.key)
        if key is None and spec.table == "list_alarms":
            key = item.get("Name") or item.get("id") or f"alarm-{_hash(raw)[:12]}"
        return None if key is None else str(key)

    def _apply_state(self, conn, endpoint, items, received_at, duration_ms):
        spec = STATE_TABLES[endpoint]
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                logger.warning("%s: item is not an object, skipped: %.120r", endpoint, item)
                continue
            raw = json.dumps(item, sort_keys=True, ensure_ascii=False)
            key = self._item_key(spec, item, raw)
            if key is None:
                logger.warning("%s: item without %r, skipped: %.120s", endpoint, spec.key, raw)
                continue
            seen.add(key)
            values = [_sql_value(item.get(c)) for c in spec.columns]
            values[0] = key                           # str name, or 1 for the single-row status table
            signature = tuple(values) + (raw,)
            cache_key = (spec.table, values[0])
            old = self._cache.get(cache_key)
            if old == signature:
                continue                              # unchanged: write nothing
            self._write_item(conn, spec, endpoint, values, raw, old, received_at)
            self._cache[cache_key] = signature
            self.stats["state_rows_changed"] += 1

        if spec.delete_missing:
            for (table, key) in [k for k in self._cache if k[0] == spec.table]:
                if str(key) not in seen:
                    conn.execute(f'DELETE FROM "{spec.table}" WHERE "{spec.key}" = ?', (key,))
                    del self._cache[(table, key)]

        self.stats["state_polls"] += 1
        self._poll_ok(conn, endpoint, len(seen), duration_ms, received_at)

    def _write_item(self, conn, spec, endpoint, values, raw, old, when):
        names = ", ".join(f'"{c}"' for c in spec.columns)
        if old is None:
            marks = ", ".join("?" * len(values))
            conn.execute(
                f'INSERT OR REPLACE INTO "{spec.table}" ({names}, raw_json, first_seen_at, updated_at) '
                f'VALUES ({marks}, ?, ?, ?)', values + [raw, when, when])
            changed = list(zip(spec.columns[1:], [None] * (len(values) - 1), values[1:]))
        else:
            sets = "".join(f'"{c}" = ?, ' for c in spec.columns[1:])   # alarms have no columns besides the key
            conn.execute(
                f'UPDATE "{spec.table}" SET {sets}raw_json = ?, updated_at = ? WHERE "{spec.key}" = ?',
                values[1:] + [raw, when, values[0]])
            changed = [(c, o, n) for c, o, n in zip(spec.columns[1:], old[1:-1], values[1:]) if o != n]
        item_name = str(values[0])
        for field, before, after in changed:
            conn.execute(
                'INSERT INTO state_changes (changed_at, endpoint, "name", field, old_value, new_value) '
                'VALUES (?,?,?,?,?,?)', (when, endpoint, item_name, field, _text(before), _text(after)))

    # ---- freshness ----

    def _meta(self, conn, key, value):
        conn.execute(
            'INSERT INTO ingest_meta (key, value, updated_at) VALUES (?,?,?) '
            'ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at',
            (key, value, _now()))

    def _poll_state(self, endpoint):
        return self._poll.setdefault(endpoint, {
            "ok": 0, "err": 0, "last_ok_at": None, "last_error_at": None, "last_error": None,
            "items": None, "ms": None, "written": 0.0, "was_error": False})

    def _poll_ok(self, conn, endpoint, item_count, duration_ms, when):
        st = self._poll_state(endpoint)
        st.update(ok=st["ok"] + 1, last_ok_at=when, items=item_count, ms=duration_ms)
        recovered = st["was_error"]
        st["was_error"] = False
        if recovered:
            st["last_error"] = None
        if recovered or time.monotonic() - st["written"] >= POLL_STATUS_EVERY_S:
            self._write_poll(conn, endpoint, st)

    def _apply_poll_error(self, conn, endpoint, message, when, duration_ms):
        st = self._poll_state(endpoint)
        st.update(err=st["err"] + 1, last_error_at=when, last_error=message, ms=duration_ms,
                  was_error=True)
        self.stats["poll_errors"] += 1
        self._write_poll(conn, endpoint, st)

    def _write_poll(self, conn, endpoint, st):
        conn.execute(
            'INSERT INTO poll_status (endpoint,last_ok_at,last_error_at,last_error,ok_count,'
            'error_count,item_count,last_duration_ms) VALUES (?,?,?,?,?,?,?,?) '
            'ON CONFLICT(endpoint) DO UPDATE SET last_ok_at=excluded.last_ok_at, '
            'last_error_at=excluded.last_error_at, last_error=excluded.last_error, '
            'ok_count=excluded.ok_count, error_count=excluded.error_count, '
            'item_count=excluded.item_count, last_duration_ms=excluded.last_duration_ms',
            (endpoint, st["last_ok_at"], st["last_error_at"], st["last_error"], st["ok"], st["err"],
             st["items"], st["ms"]))
        st["written"] = time.monotonic()
