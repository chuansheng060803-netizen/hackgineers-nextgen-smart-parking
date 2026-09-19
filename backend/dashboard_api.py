"""Serves the dashboard's read API from the live system.

The dashboard (dashboard/README.md) expects three endpoints:

    GET  /api/snapshot                        everything on screen right now
    GET  /api/history?date=YYYY-MM-DD         one past day's finished visits
    POST /api/control/{gate|fan}/{name}/{action}   manual buttons

This module builds those from what the rest of the backend already has:
CarFlow's live car state, the simulator client, and (optionally) Person 3's
SQLite database. It never changes the parking flow: the only commands it can
send are the gate open/close the operator presses in the dashboard.

Wiring (see run_flow.py):

    state = DashboardState(flow, client, use_db=args.db)
    webhook.register_handler(state.on_event)
    webhook.app.register_blueprint(build_blueprint(state))

Then start the dashboard with
    DASHBOARD_SOURCE=api  DASHBOARD_API_URL=http://localhost:5000
"""
import logging
import threading
from collections import deque
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

try:                                    # the team's own pricing rules
    from payment_logic import calculate_charges
except ImportError:                     # running outside backend/
    calculate_charges = None

logger = logging.getLogger(__name__)

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
POLL_EVERY_S = 2            # how often the background thread re-reads the simulator
SAMPLE_EVERY_S = 5          # how often the history series get a new point
FAN_CHECK_EVERY_S = 60      # level 1 has no exhausts; don't ask every time
ARCHIVE_EVERY_S = 60        # past days come from SQLite; never on a web request
HISTORY_MINUTES = 60        # how much of it the dashboard charts show
CO_MID = 50.0               # organisers: 50 ppm and up counts as "Mid"
CO_HIGH, CO_CRITICAL = 100.0, 200.0
SPOT_CACHE_S = 2.0          # don't hammer the simulator on every refresh

# CarFlow stages -> what the dashboard calls them
STAGE_STATUS = {
    "WAITING": "Heading to spot",
    "MOVING": "Heading to spot",
    "PARKED": "Parked",
    "LEAVING": "Heading to exit",
    "AT_EXIT": "Heading to exit",
    "CHARGING": "Heading to exit",
    "DONE": "Heading to exit",
}

# Simulator EventClass -> the dashboard's activity-feed kinds
EVENT_KINDS = {
    "payment_made": "payment",
    "penalty": "penalty",
    "component_broken": "component",
    "component_fixed": "component",
    "carbon_monoxide_event": "co",
    "gate_action": "gate",
}


def _text(value, default=""):
    return value if isinstance(value, str) else default


def _pick(mapping, *names, default=None):
    """First present key out of several spellings (the simulator is not consistent)."""
    for name in names:
        if isinstance(mapping, dict) and name in mapping and mapping[name] is not None:
            return mapping[name]
    return default


def _risk(ppm):
    if ppm >= CO_CRITICAL:
        return "Critical"
    if ppm >= CO_HIGH:
        return "High"
    if ppm >= CO_MID:
        return "Mid"
    return "Safe"


def _parse_time(value):
    if isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(value, TIME_FORMAT)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


class DashboardState:
    """Everything the dashboard needs, kept up to date from webhook events.

    Nothing here is required for the parking flow to work: if this object
    raises, CarFlow carries on (handle_event catches handler exceptions).
    """

    def __init__(self, flow=None, client=None, use_db=False, allow_commands=True):
        self.flow = flow
        self.client = client
        self.use_db = use_db
        self.allow_commands = allow_commands
        self.extra_health = None     # optional callable -> dict, added to /api/health
        self._lock = threading.RLock()

        self.events = deque(maxlen=300)        # newest first, dashboard shape
        self.sessions = deque(maxlen=2000)     # finished visits, newest first
        self.penalties = deque(maxlen=200)
        self.arrivals = deque(maxlen=5000)     # datetimes, for the arrivals chart
        self.refused = deque(maxlen=500)       # datetimes a car could not be placed

        self.co = {}                           # zone -> latest ppm
        self.history_occ = deque(maxlen=3000)
        self.history_co = deque(maxlen=12000)

        self.revenue = 0.0
        self.cars_served = 0
        self.penalty_total = 0.0
        self.paid_amounts = {}                 # plate -> what the car actually paid

        self.gate_manual = {}                  # name -> "open" / "closed" (operator hold)
        self.started_at = datetime.now()
        self._seen_event_ids = deque(maxlen=5000)
        self._seen_lookup = set()

        self._spots_cache = []
        self._spots_at = None
        self._gates_cache = []
        self._gates_at = None
        self._fans_cache = []
        self._zones_cache = []
        self._zones_at = None
        self._archive_cache = []               # past days, rebuilt in the background

        self._sampler = threading.Thread(target=self._sample_loop, daemon=True)
        self._sampler.start()

    # ---- webhook events ---------------------------------------------------

    def on_event(self, event):
        """Registered with webhook.register_handler; never raises."""
        try:
            with self._lock:
                if self._already_seen(event):
                    return
                self._on_event(event)
        except Exception:
            logger.exception("Dashboard failed to record event %s", event.get("EventId"))

    def _already_seen(self, event):
        """The simulator re-sends an event when the webhook is slow to answer.

        CarFlow drops the repeat by EventId, so it has already finished with that
        car and forgotten it. Without the same check here a repeated departure
        would be counted a second time, as a visit belonging to a car we no
        longer know anything about.
        """
        event_id = event.get("EventId")
        if event_id is None:
            return False
        if event_id in self._seen_lookup:
            logger.debug("Dashboard ignoring repeated event %s", event_id)
            return True
        if len(self._seen_event_ids) == self._seen_event_ids.maxlen:
            self._seen_lookup.discard(self._seen_event_ids[0])
        self._seen_event_ids.append(event_id)
        self._seen_lookup.add(event_id)
        return False

    def _on_event(self, event):
        event_class = _text(event.get("EventClass"))
        when = _parse_time(event.get("ServerDateTime")) or datetime.now()
        plate = event.get("CarPlateNumber")
        spot = _text(event.get("SpotName"))
        direction = _text(event.get("Direction"))
        spot_type = _text(event.get("SpotType"))

        kind, text = EVENT_KINDS.get(event_class, "component"), None

        if event_class == "car_spot_action":
            if spot_type == "EntrySpot" and direction == "CarIn":
                kind, text = "entry", f"{plate} arrived at {spot}"
                self.arrivals.append(when)
            elif spot_type == "ExitSpot" and direction == "CarIn":
                kind, text = "exit", f"{plate} reached the exit"
            elif spot_type == "ExitSpot" and direction == "CarOut":
                kind, text = "exit", f"{plate} left the car park"
                self._finish_session(plate, when)
            elif direction == "CarIn":
                kind, text = "park", f"{plate} parked in {spot}"
            elif direction == "CarOut":
                kind, text = "park", f"{plate} left {spot}"
            else:
                kind, text = "park", f"{plate} {direction} {spot}"

        elif event_class == "payment_made":
            amount = _pick(event, "Amount", "amount", default=0) or 0
            try:
                amount = float(amount)
                self.revenue += amount
                self.paid_amounts[plate] = amount
            except (TypeError, ValueError):
                pass
            text = f"{plate} paid {amount}"

        elif event_class == "penalty":
            # the real simulator sends the fine as "FineAmount": "10"
            amount = _pick(event, "FineAmount", "Fine", "Amount", "amount", "Penalty",
                           default=0) or 0
            reason = _text(_pick(event, "Reason", "reason", "Message", "Description"),
                           "Penalty")
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                amount = 0.0
            self.penalty_total += amount
            self.penalties.appendleft({
                "t": when.strftime("%H:%M:%S"), "reason": reason,
                "amount": amount, "component": spot or _text(event.get("ComponentName")),
            })
            text = f"Penalty {amount:g}: {reason}"

        elif event_class == "carbon_monoxide_event":
            zone = _text(_pick(event, "ZoneName", "Zone", "ZoneParent"), "ZONE1")
            ppm = _pick(event, "Value", "Ppm", "PPM", "CarbonMonoxide", "Level", default=None)
            try:
                ppm = float(ppm)
            except (TypeError, ValueError):
                ppm = None
            if ppm is not None:
                self.co[zone] = ppm
                self.history_co.append({"t": when, "zone": zone, "ppm": ppm})
                text = f"{zone} carbon monoxide {ppm:.0f} ppm"

        elif event_class in ("component_broken", "component_fixed"):
            name = spot or _text(_pick(event, "ComponentName", "Name"), "a component")
            word = "broke down" if event_class == "component_broken" else "is back in service"
            text = f"{name} {word}"

        elif event_class == "gate_action":
            name = _text(_pick(event, "GateName", "Name"), spot or "gate")
            text = f"{name} {_text(_pick(event, 'Action', 'State'), 'changed')}"

        self.events.appendleft({
            "t": when.strftime("%H:%M:%S"),
            "kind": kind,
            "text": text or f"{event_class or 'event'} {plate or spot or ''}".strip(),
        })

    def note_refused(self, plate):
        """CarFlow could not place a car. Called from the dashboard's own view of
        the flow; safe to call from anywhere."""
        with self._lock:
            self.refused.appendleft(datetime.now())
            self.events.appendleft({
                "t": datetime.now().strftime("%H:%M:%S"), "kind": "refused",
                "text": f"{plate} is waiting: no free spot",
            })

    def _finish_session(self, plate, when):
        car = (self.flow.cars.get(plate) if self.flow else None) or {}
        # A car we have no record of: it was already in the car park when the
        # backend started, so we never saw it arrive. We cannot say whether it
        # paid, and claiming it did not would be inventing a fact.
        tracked = bool(car.get("entry_in_time") or car.get("spot"))
        entered = car.get("entry_in_time") or when
        minutes = max(0.0, round((when - entered).total_seconds() / 60.0, 1))
        # What it was charged; if the flow never recorded a cost, fall back to what
        # the simulator's payment_made event said was actually paid.
        charge = (car.get("parking_cost") or 0) + (car.get("charging_cost") or 0)
        paid = self.paid_amounts.pop(plate, None)
        if not charge and paid:
            charge = paid
        if tracked:
            status = "Completed" if (car.get("paid") or paid) else "Left without paying"
            self.cars_served += 1          # only visits we actually handled
        else:
            status = "Left (not tracked)"
        self.sessions.appendleft({
            "plate": plate,
            "car_type": car.get("car_type") or "Normal",
            "spot": car.get("spot") or "-",
            "entered_at": entered.strftime(TIME_FORMAT),
            "left_at": when.strftime(TIME_FORMAT),
            "minutes": minutes,
            "charge": round(float(charge or 0), 2),
            "status": status,
        })

    # ---- background sampling ---------------------------------------------

    def _sample_loop(self):
        """The only place that talks to the simulator on a timer.

        Every web request is then served from these caches, so a slow or busy
        simulator can never make /api/snapshot time out.
        """
        last_sample = last_fan_check = last_archive = None
        while True:
            now = datetime.now()
            try:
                spots = self._read_spots()
                zones = self._read_zones()
                self._read_gates()
                if last_fan_check is None or \
                        (now - last_fan_check).total_seconds() >= FAN_CHECK_EVERY_S:
                    self._read_fans()
                    last_fan_check = now

                if self.use_db and (last_archive is None or
                                    (now - last_archive).total_seconds() >= ARCHIVE_EVERY_S):
                    self._read_archive()
                    last_archive = now

                if last_sample is None or \
                        (now - last_sample).total_seconds() >= SAMPLE_EVERY_S:
                    last_sample = now
                    occupied = sum(1 for s in spots if s["state"] == "occupied")
                    with self._lock:
                        self.history_occ.append({"t": now, "occupied": occupied,
                                                 "total": len(spots)})
                        for zone in zones:
                            self.history_co.append({"t": now, "zone": zone["name"],
                                                    "ppm": zone["co_ppm"]})
            except Exception:
                logger.debug("Simulator poll skipped", exc_info=True)
            threading.Event().wait(POLL_EVERY_S)

    # ---- the pieces of the snapshot ---------------------------------------

    def spots(self):
        """Parking spots as the dashboard draws them. Served from the cache the
        background thread keeps warm, so a web request never waits on the
        simulator."""
        return list(self._spots_cache)

    def _read_spots(self):
        """Park spots, read from the simulator. Background thread only."""
        now = datetime.now()
        raw = []
        if self.client is not None:
            try:
                raw = self.client.list_parking_spots() or []
            except Exception as exc:
                logger.warning("list_parking_spots failed: %s", exc)
                return self._spots_cache

        reserved, parked_in = {}, {}
        if self.flow is not None:
            for car in list(self.flow.cars.values()):
                if not car.get("spot"):
                    continue
                if car.get("stage") == "PARKED" or car.get("parked_in_time"):
                    parked_in[car["spot"]] = car["plate"]
                elif car.get("parked_out_time") is None:
                    reserved[car["spot"]] = car["plate"]

        out = []
        for spot in raw:
            if _text(_pick(spot, "purpose", "Purpose")) != "Park":
                continue
            name = _text(_pick(spot, "name", "Name"))
            broken = bool(_pick(spot, "broken", "Broken", default=False))
            maintenance = bool(_pick(spot, "isUnderMaintenance", "IsUnderMaintenance",
                                     default=False))
            detected = _pick(spot, "detectedCars", "DetectedCars", default=0) or 0
            if broken:
                state, plate = "broken", None
            elif maintenance:
                state, plate = "maintenance", None
            elif detected:
                state = "occupied"
                plate = parked_in.get(name) or _text(_pick(spot, "lastCarPlate", "LastCarPlate")) or None
            elif name in reserved:
                state, plate = "reserved", reserved[name]
            else:
                state, plate = "available", None
            out.append({
                "name": name,
                "zone": _text(_pick(spot, "zoneParent", "ZoneParent"), "ZONE1"),
                "type": _text(_pick(spot, "parkingForCarType", "CarType"), "Any"),
                "state": state,
                "car": plate,
            })
        out.sort(key=lambda s: (len(s["name"]), s["name"]))
        self._spots_cache, self._spots_at = out, now
        return out

    def cars(self):
        if self.flow is None:
            return []
        now = datetime.now()
        rows = []
        for car in list(self.flow.cars.values()):
            entered = car.get("entry_in_time") or now
            parked = car.get("parked_in_time")
            charge = self._charge_so_far(car, now)
            rows.append({
                "plate": car.get("plate"),
                "car_type": car.get("car_type") or "Normal",
                "status": STAGE_STATUS.get(car.get("stage"), "Heading to spot"),
                "spot": car.get("spot"),
                "entered_at": entered.strftime(TIME_FORMAT),
                "minutes_inside": round((now - (parked or entered)).total_seconds() / 60.0, 1),
                "estimated_charge": round(float(charge or 0), 2),
                "flag": None,
                "assigned_spot": car.get("spot"),
            })
        rows.sort(key=lambda r: r["entered_at"], reverse=True)
        return rows

    @staticmethod
    def _charge_so_far(car, now):
        """What this car owes right now.

        Once it reaches the exit the backend bills it and the real amount is
        known. Before that nothing has been charged yet, so showing 0 would
        suggest the car park is earning nothing from the cars parked in it. The
        running total is worked out with the team's own pricing rules
        (payment_logic), never a guess at them.
        """
        billed = (car.get("parking_cost") or 0) + (car.get("charging_cost") or 0)
        if billed:
            return round(float(billed), 2)
        parked = car.get("parked_in_time")
        if not parked or calculate_charges is None:
            return 0.0
        minutes = max(0.0, (now - parked).total_seconds() / 60.0)
        try:
            charges = calculate_charges(car.get("car_type"), minutes)
        except Exception:
            return 0.0
        return round(float(charges.get("parkingCost", 0))
                     + float(charges.get("chargingCost", 0)), 2)

    def gates(self):
        """Barrier gates, from the warm cache (see spots)."""
        return list(self._gates_cache)

    def _read_gates(self):
        """Barrier gates, read from the simulator. The list-barriers shape is
        read defensively. Background thread only."""
        now = datetime.now()
        raw = []
        if self.client is not None:
            try:
                raw = self.client.list_barriers() or []
            except Exception as exc:
                logger.warning("list_barriers failed: %s", exc)
                return self._gates_cache
        if isinstance(raw, dict):                       # {"barriers": [...]} or similar
            for value in raw.values():
                if isinstance(value, list):
                    raw = value
                    break
        out = []
        for gate in raw if isinstance(raw, list) else []:
            if not isinstance(gate, dict):
                continue
            name = _text(_pick(gate, "name", "Name"))
            broken = bool(_pick(gate, "broken", "Broken", "isBroken", default=False))
            maintenance = bool(_pick(gate, "isUnderMaintenance", "IsUnderMaintenance",
                                     default=False))
            state = _text(_pick(gate, "state", "State"), "Closed").capitalize()
            out.append({
                "name": name,
                "role": _text(_pick(gate, "role", "Role"), "Barrier gate"),
                "zone": _text(_pick(gate, "zoneParent", "ZoneParent")),
                "state": state,
                "health": "broken" if broken else "maintenance" if maintenance else "ok",
                "manual": self.gate_manual.get(name),
            })
        self._gates_cache, self._gates_at = out, now
        return out

    def _get(self, path):
        """GET any documented simulator endpoint through the shared client.

        Uses SimulatorClient.call, so no new HTTP or login code is added here.
        Returns None when the client cannot do it or the endpoint is missing.
        """
        if self.client is None or not hasattr(self.client, "call"):
            return None
        try:
            return self.client.call("GET", path)
        except Exception as exc:
            logger.debug("GET %s failed: %s", path, exc)
            return None

    def fans(self):
        """Exhaust fans, from the warm cache. Level 1 has none
        (/api/v1/list-exhausts is 404) so this is normally empty and the
        dashboard hides the fan panel."""
        return list(self._fans_cache)

    def _read_fans(self):
        raw = self._get("/api/v1/list-exhausts")
        if not isinstance(raw, list):
            return list(self._fans_cache)
        out = []
        for fan in raw:
            if not isinstance(fan, dict):
                continue
            broken = bool(_pick(fan, "broken", "Broken", default=False))
            maintenance = bool(_pick(fan, "isUnderMaintenance", "IsUnderMaintenance",
                                     default=False))
            out.append({
                "name": _text(_pick(fan, "name", "Name")),
                "zone": _text(_pick(fan, "zoneParent", "ZoneParent")),
                "on": bool(_pick(fan, "isOn", "IsOn", "on", default=False)),
                "health": "broken" if broken else "maintenance" if maintenance else "ok",
            })
        self._fans_cache = out
        return out

    def zones(self):
        """Carbon monoxide per zone.

        Served from the warm cache (see spots).
        """
        if self._zones_cache:
            return list(self._zones_cache)
        with self._lock:                       # fallback: whatever the webhook told us
            return [{"name": zone, "co_ppm": round(ppm, 1), "risk": _risk(ppm)}
                    for zone, ppm in sorted(self.co.items())]

    def _read_zones(self):
        """Carbon monoxide per zone, read from the simulator. The answer is
        {name, gasCarbonMonoxideLevel, risk}, exactly what the CO panel needs.
        Background thread only; webhook events are the fallback.
        """
        now = datetime.now()
        raw = self._get("/api/v1/list-zones")
        out = []
        if isinstance(raw, list):
            for zone in raw:
                if not isinstance(zone, dict):
                    continue
                name = _text(_pick(zone, "name", "Name"))
                ppm = _pick(zone, "gasCarbonMonoxideLevel", "GasCarbonMonoxideLevel",
                            "co_ppm", default=0) or 0
                try:
                    ppm = float(ppm)
                except (TypeError, ValueError):
                    ppm = 0.0
                with self._lock:
                    self.co[name] = ppm
                out.append({"name": name, "co_ppm": round(ppm, 1),
                            "risk": _text(_pick(zone, "risk", "Risk"), _risk(ppm))})
            self._zones_cache, self._zones_at = out, now
            return out
        with self._lock:                       # fallback: whatever the webhook told us
            return [{"name": zone, "co_ppm": round(ppm, 1), "risk": _risk(ppm)}
                    for zone, ppm in sorted(self.co.items())]

    def history(self):
        cutoff = datetime.now() - timedelta(minutes=HISTORY_MINUTES)
        with self._lock:
            occ = [{"t": r["t"].isoformat(timespec="seconds"),
                    "occupied": r["occupied"], "total": r["total"]}
                   for r in self.history_occ if r["t"] >= cutoff]
            co = [{"t": r["t"].isoformat(timespec="seconds"),
                   "zone": r["zone"], "ppm": round(r["ppm"], 1)}
                  for r in self.history_co if r["t"] >= cutoff]
            arrivals_in = [t for t in self.arrivals if t >= cutoff]
        buckets = {}
        for t in arrivals_in:
            bucket = t.replace(second=0, microsecond=0, minute=(t.minute // 5) * 5)
            buckets[bucket] = buckets.get(bucket, 0) + 1
        now = datetime.now()
        start = now.replace(second=0, microsecond=0, minute=(now.minute // 5) * 5) \
            - timedelta(minutes=55)
        arrivals = [{"t": (start + timedelta(minutes=5 * i)).isoformat(timespec="seconds"),
                     "count": buckets.get(start + timedelta(minutes=5 * i), 0)}
                    for i in range(12)]
        return {"occupancy": occ, "co": co, "arrivals": arrivals}

    def stats(self):
        cutoff = datetime.now() - timedelta(minutes=10)
        # A car CarFlow could not place is still at the entrance with no spot:
        # that is what the dashboard's congestion alert is about.
        waiting = 0
        if self.flow is not None:
            waiting = sum(1 for c in list(self.flow.cars.values())
                          if c.get("stage") == "WAITING" and not c.get("spot"))
        with self._lock:
            return {
                "revenue": round(self.revenue, 2),
                "cars_served": self.cars_served,
                "penalty_count": len(self.penalties),
                "penalty_total": round(self.penalty_total, 2),
                "refused_last_10min": sum(1 for t in self.refused if t >= cutoff) + waiting,
            }

    def archive(self):
        """One row per day, newest first. Today is built from memory; earlier days
        come from the cache the background thread refills.

        The database is deliberately NOT read here: CarFlow writes to the same
        SQLite file on every webhook, and a reader can block behind a writer for
        seconds, which would stall the dashboard.
        """
        with self._lock:
            today_rows = list(self.sessions)
        today = datetime.now().date()
        return [self._summarise(today.isoformat(), today_rows)] + list(self._archive_cache)

    def _read_archive(self):
        """Background thread only: rebuild the past-days cache from the database."""
        today = datetime.now().date().isoformat()
        self._archive_cache = self._archive_from_db(exclude=today)
        return self._archive_cache

    @staticmethod
    def _summarise(date, visits):
        parked = [v for v in visits if v.get("spot") not in (None, "-")]
        minutes = [v.get("minutes", 0) for v in visits if v.get("minutes")]
        return {
            "date": date,
            "visits": len(visits),
            "cars_parked": len(parked),
            "drive_through": len(visits) - len(parked),
            "income": round(sum(float(v.get("charge") or 0) for v in visits), 2),
            "penalties": 0,
            "peak_pct": 0,
            "avg_minutes": round(sum(minutes) / len(minutes), 1) if minutes else 0,
        }

    def _db_sessions(self, date=None):
        """Finished visits from the database, newest first. [] if no database."""
        if not self.use_db:
            return []
        try:
            from database import database_service as service
            from database.database import get_connection
        except Exception:
            return []
        query = """
            SELECT s.car_name, s.car_type, s.spot_name, s.arrival_time,
                   s.departure_time, s.status,
                   COALESCE(SUM(p.parking_cost + p.charging_cost), 0) AS charge
            FROM parking_sessions s
            LEFT JOIN payments p ON p.session_id = s.id AND p.status = 'paid'
            WHERE s.departure_time IS NOT NULL
        """
        params = []
        if date:
            query += " AND date(s.arrival_time) = ?"
            params.append(date)
        query += " GROUP BY s.id ORDER BY s.departure_time DESC"
        try:
            connection = get_connection()
            try:
                rows = connection.execute(query, params).fetchall()
            finally:
                connection.close()
        except Exception:
            logger.exception("Reading visits from the database failed")
            return []
        out = []
        for row in rows:
            arrival = _parse_time(row["arrival_time"])
            departure = _parse_time(row["departure_time"])
            minutes = round((departure - arrival).total_seconds() / 60.0, 1) \
                if arrival and departure else 0
            out.append({
                "plate": row["car_name"],
                "car_type": row["car_type"] or "Normal",
                "spot": row["spot_name"] or "-",
                "entered_at": row["arrival_time"],
                "left_at": row["departure_time"],
                "minutes": minutes,
                "charge": round(float(row["charge"] or 0), 2),
                "status": "Completed",
            })
        return out

    def _archive_from_db(self, exclude=None):
        by_day = {}
        for visit in self._db_sessions():
            day = (visit.get("entered_at") or "")[:10]
            if not day or day == exclude:
                continue
            by_day.setdefault(day, []).append(visit)
        return [self._summarise(day, visits)
                for day, visits in sorted(by_day.items(), reverse=True)][:29]

    def history_for(self, date):
        today = datetime.now().date().isoformat()
        if date == today:
            with self._lock:
                return list(self.sessions)
        return self._db_sessions(date)

    # ---- the whole snapshot ----------------------------------------------

    def snapshot(self):
        with self._lock:
            events = list(self.events)
            sessions = list(self.sessions)[:300]
            penalties = list(self.penalties)
        return {
            "generated_at": datetime.now().strftime(TIME_FORMAT),
            "source": "simulator",
            "spots": self.spots(),
            "cars": self.cars(),
            "events": events,
            "sessions": sessions,
            "archive": self.archive(),
            "gates": self.gates(),
            "fans": self.fans(),
            "zones": self.zones(),
            "history": self.history(),
            "stats": self.stats(),
            "penalties": penalties,
        }

    # ---- manual controls --------------------------------------------------

    def control(self, kind, name, action, role):
        """The dashboard's Open / Close / Auto / Repair buttons."""
        role = (role or "").lower()
        if role not in ("operator", "admin"):
            return False, "Only an Operator or an Admin can control components."
        if not self.allow_commands:
            return False, ("Dry run: nothing was sent to the simulator. "
                           "Start the backend with --live to use these buttons.")
        if kind == "fan" and not self._fans_cache:
            return False, "This car park has no exhaust fans to control."
        if kind != "gate":
            return False, f"Controlling a {kind} is not supported yet."

        gate = next((g for g in self._read_gates() if g["name"] == name), None)
        if gate is None:
            return False, f"Unknown gate '{name}'."
        if action == "repair":
            # The simulator exposes IsRepairRequested on components, but no
            # confirmed endpoint to request a repair, so this is not guessed.
            return False, ("Requesting a repair is not wired to the simulator yet. "
                           "Ask the backend teammate for the repair endpoint.")
        if gate["health"] != "ok":
            state = "broken" if gate["health"] == "broken" else "under repair"
            return False, f"{name} is {state}. Do not operate it."

        try:
            if action == "open":
                self.client.open_gate(name)
                self.gate_manual[name] = "open"
            elif action == "close":
                self.client.close_gate(name)
                self.gate_manual[name] = "closed"
            elif action == "auto":
                self.gate_manual.pop(name, None)
                return True, f"{name} set back to automatic."
            else:
                return False, f"Unknown action '{action}' for a gate."
        except Exception as exc:
            logger.error("control %s %s %s failed: %s", kind, name, action, exc)
            return False, f"The simulator refused: {exc}"

        self._read_gates()         # show the new state straight away
        with self._lock:
            self.events.appendleft({
                "t": datetime.now().strftime("%H:%M:%S"), "kind": "gate",
                "text": f"{role.capitalize()} {action}ed {name} from the dashboard",
            })
        return True, f"{name} {action}ed. Press Auto to hand it back."


def build_blueprint(state):
    api = Blueprint("dashboard_api", __name__)

    @api.get("/api/snapshot")
    def snapshot():
        return jsonify(state.snapshot())

    @api.get("/api/history")
    def history():
        date = request.args.get("date", "")
        return jsonify(state.history_for(date))

    @api.post("/api/control/<kind>/<name>/<action>")
    def control(kind, name, action):
        body = request.get_json(silent=True) or {}
        ok, message = state.control(kind, name, action, body.get("role"))
        return jsonify({"ok": ok, "message": message}), (200 if ok else 403)

    @api.get("/api/health")
    def health():
        body = {"ok": True, "cars": len(state.flow.cars) if state.flow else 0}
        if state.extra_health:
            try:
                body.update(state.extra_health())
            except Exception:                    # health must never fail
                pass
        return jsonify(body)

    return api
