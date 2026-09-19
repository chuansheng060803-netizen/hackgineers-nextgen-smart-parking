"""Mock car park for the dashboard (no backend, no simulator needed).

It runs a tiny car-park simulation so the dashboard looks alive: cars arrive, get a spot, park, pay and
leave; carbon monoxide rises when cars move; gates and fans can break. It returns the SAME shape of
data (a "snapshot" dict, described in README.md) that the real backend will return later.

Demo buttons in the sidebar call `MockWorld.trigger(...)` to force the scenarios from the brief
(traffic surge, CO buildup, gate breakdown, rogue car).
"""
import datetime as dt
import math
import random
import string
import threading
import time
from collections import deque

ZONES = ["ZONE1", "ZONE2", "ZONE3", "ZONE4", "ZONE5", "ZONE6"]
SPOTS_PER_ZONE = 5
STEP_S = 5.0                                   # the simulation moves in 5-second steps
CO_MID, CO_HIGH, CO_CRITICAL = 50.0, 100.0, 200.0   # ppm; the organisers say 50 counts as "medium"
ACCESSIBLE_SPOTS = {"S1", "S6", "S11", "S16", "S21", "S26"}      # first slot of each zone
ELECTRIC_SPOTS = {"S5", "S10", "S15", "S20", "S25", "S30"}       # last slot of each zone
ARRIVAL_EVERY_S = 14                          # on average one car every 14 s (busy enough to watch, not always full)
STAY_RANGE_S = (180, 420)                     # parked cars stay 3 to 7 minutes
DRIVE_THROUGH_SHARE = 0.12                    # 12% just use the car park as a short cut and leave straight away
PARKING_RATE = 1.0                             # per minute (organisers' example)
ELECTRIC_RATE = 2.0                            # extra per minute for electricity


def co_risk(ppm):
    if ppm >= CO_CRITICAL:
        return "Critical"
    if ppm >= CO_HIGH:
        return "High"
    if ppm >= CO_MID:
        return "Mid"
    return "Safe"


def _natural(name):
    return int("".join(ch for ch in name if ch.isdigit()) or 0)


class MockWorld:
    def __init__(self, seed=7):
        self.rng = random.Random(seed)
        self.lock = threading.RLock()
        self.speed = 1.0
        self._accum = 0.0
        self.repairs = {}                            # component name -> sim time its repair ends
        self.until = {}                              # scenario name -> sim time it ends
        self.gate_break_at = None
        self.co_started = None
        self.spots = {}
        self.cars = {}
        self.events = deque(maxlen=300)
        self.penalties = deque(maxlen=300)
        self.arrivals = deque(maxlen=3000)
        self.sessions = deque(maxlen=2000)            # finished visits, newest first
        self.history_occ = deque(maxlen=900)
        self.history_co = deque(maxlen=900)
        self.revenue = 0.0
        self.cars_served = 0
        self.refused = deque(maxlen=200)
        self._build()
        # Fast-forward 60 minutes so charts have history the moment the page opens.
        self.now = dt.datetime.now().replace(microsecond=0) - dt.timedelta(minutes=60)
        self._next_sample = self.now
        for _ in range(int(60 * 60 / STEP_S)):
            self._step()
        # The warm-up gave the charts some history. Now start the counters from zero.
        self.revenue, self.cars_served = 0.0, 0
        for q in (self.penalties, self.sessions, self.refused, self.events):
            q.clear()
        self._build_archive()
        self._last_real = time.time()

    # ------------------------------------------------------------------ set-up
    def _build(self):
        self.spots = {}
        for i in range(1, len(ZONES) * SPOTS_PER_ZONE + 1):
            name = f"S{i}"
            typ = "Accessible" if name in ACCESSIBLE_SPOTS else "Electric" if name in ELECTRIC_SPOTS else "Any"
            self.spots[name] = {"name": name, "zone": ZONES[(i - 1) // SPOTS_PER_ZONE], "type": typ,
                                "state": "available", "car": None, "health": "ok"}
        self.gates = [
            {"name": "gate0", "zone": "", "role": "Entrance", "state": "Closed", "health": "ok", "uses": 0, "close_at": None, "manual": None},
            {"name": "gate1", "zone": "", "role": "Exit", "state": "Closed", "health": "ok", "uses": 0, "close_at": None, "manual": None},
        ]
        self.fans = [{"name": f"fan{i}", "zone": z, "on": False, "health": "ok", "manual": None, "hours": 0.0}
                     for i, z in enumerate(ZONES)]
        self.co = {z: 8.0 for z in ZONES}
        self.co_extra = {z: 0.0 for z in ZONES}

    # ------------------------------------------------------------------ helpers
    def _log(self, kind, text, plate=None):
        self.events.appendleft({"t": self.now.strftime("%H:%M:%S"), "kind": kind, "text": text, "plate": plate})

    def _penalty(self, reason, amount=10.0, component=""):
        self.penalties.appendleft({"t": self.now.strftime("%H:%M:%S"), "reason": reason, "amount": amount,
                                   "component": component})
        self._log("penalty", f"Penalty -{amount:.0f}: {reason}", None)

    def _plate(self):
        while True:
            p = f"{self.rng.choice('WBPV')}{self.rng.choice(string.ascii_uppercase)}" \
                f"{self.rng.choice(string.ascii_uppercase)} {self.rng.randint(100, 9999)}"
            if p not in self.cars:
                return p

    def _allocate(self, car_type):
        wanted = {"Electric": ["Electric", "Any"], "Accessible": ["Accessible", "Any"]}.get(car_type, ["Any"])
        free = [s for s in self.spots.values() if s["state"] == "available" and s["health"] == "ok"]
        for typ in wanted:
            pool = sorted((s for s in free if s["type"] == typ), key=lambda s: _natural(s["name"]))
            if pool:
                return pool[0]
        return None

    def _gate(self, name):
        return next(g for g in self.gates if g["name"] == name)

    def _open_gate(self, name, seconds=6):
        g = self._gate(name)
        if g["health"] != "ok" or g.get("manual") == "closed":
            return False
        g["state"], g["uses"] = "Open", g["uses"] + 1
        # manual "open" = an operator holds it open; otherwise it closes by itself a few seconds later
        g["close_at"] = None if g.get("manual") == "open" else self.now + dt.timedelta(seconds=seconds)
        return True

    def _held_shut(self, name):
        """An operator closed this gate by hand: no car can pass until they press Auto (or Open)."""
        return self._gate(name).get("manual") == "closed"

    def _active(self, name):
        return name in self.until and self.now < self.until[name]

    # ------------------------------------------------------------------ one simulation step
    def _rate(self):
        minutes = (self.now - dt.datetime(2026, 1, 1)).total_seconds() / 60.0
        base = (1.0 / ARRIVAL_EVERY_S) * (1 + 0.55 * math.sin(minutes / 3.0))       # arrivals per second, slow waves
        return base * (10.0 if self._active("surge") else 1.0)

    def _step(self):
        self.now += dt.timedelta(seconds=STEP_S)
        self._arrivals()
        self._progress_cars()
        self._gates_and_fans()
        self._co()
        self._scenarios()
        self._finish_repairs()
        if self.now >= self._next_sample:
            self._next_sample = self.now + dt.timedelta(seconds=30)
            occupied = sum(1 for s in self.spots.values() if s["state"] in ("occupied", "reserved"))
            self.history_occ.append({"t": self.now, "occupied": occupied, "total": len(self.spots)})
            for z in ZONES:
                self.history_co.append({"t": self.now, "zone": z, "ppm": round(self.co[z], 1)})

    def _arrivals(self):
        if self.rng.random() > 1 - math.exp(-self._rate() * STEP_S):
            return
        car_type = self.rng.choices(["Normal", "Electric", "Accessible"], [0.72, 0.2, 0.08])[0]
        plate = self._plate()
        if self._gate("gate0")["health"] != "ok":
            self.refused.appendleft(self.now)
            self._log("refused", f"{plate} is stuck: entrance gate is down", plate)
            if self.rng.random() < 0.5:
                self._penalty("Car left the entrance because it was neglected", 10, "gate0")
            return
        if self._held_shut("gate0"):
            self.refused.appendleft(self.now)
            self._log("refused", f"{plate} is waiting outside: entrance gate was closed by an operator", plate)
            return
        if self.rng.random() < DRIVE_THROUGH_SHARE:          # short cut: in through the entrance, straight out again
            self.arrivals.append(self.now)
            self.cars[plate] = {"plate": plate, "type": car_type, "state": "passing", "spot": None, "entered": self.now,
                                "arrive_at": None, "planned_s": 0, "parked_at": None,
                                "leave_at": self.now + dt.timedelta(seconds=self.rng.randint(15, 40)), "flag": None}
            self._open_gate("gate0")
            self._log("entry", f"{plate} entered and is only passing through (short cut)", plate)
            return
        spot = self._allocate(car_type)
        self.arrivals.append(self.now)
        if not spot:
            self.refused.appendleft(self.now)
            self._log("refused", f"{plate} sent away: car park full", plate)
            return
        spot["state"], spot["car"] = "reserved", plate
        self.cars[plate] = {"plate": plate, "type": car_type, "state": "arriving", "spot": spot["name"],
                            "entered": self.now, "arrive_at": self.now + dt.timedelta(seconds=self.rng.randint(6, 12)),
                            "planned_s": self.rng.randint(*STAY_RANGE_S), "parked_at": None, "leave_at": None, "flag": None}
        self._open_gate("gate0")
        self._log("entry", f"{plate} ({car_type}) entered, sent to {spot['name']}", plate)

    def _progress_cars(self):
        for plate, car in list(self.cars.items()):
            if car["state"] == "passing":
                if self.now >= car["leave_at"] and not self._held_shut("gate1"):
                    self._open_gate("gate1")
                    self._log("exit", f"{plate} drove through and left without parking", plate)
                    self.sessions.appendleft(self._visit_row(car, "-", 0.0, "Drove through"))
                    del self.cars[plate]
                continue
            spot = self.spots[car["spot"]]
            if car["state"] == "arriving" and self.now >= car["arrive_at"]:
                car["state"], car["parked_at"] = "parked", self.now
                spot["state"] = "occupied"
                self._log("park", f"{plate} parked in {spot['name']}", plate)
            elif car["state"] == "parked" and self.now >= car["parked_at"] + dt.timedelta(seconds=car["planned_s"]):
                car["state"] = "to_exit"
                car["leave_at"] = self.now + dt.timedelta(seconds=self.rng.randint(8, 14))
                spot["state"], spot["car"] = "available", None
                self._log("park", f"{plate} left {spot['name']}", plate)
            elif car["state"] == "to_exit" and self.now >= car["leave_at"] and not self._held_shut("gate1"):
                minutes = max(1, round(car["planned_s"] / 60.0))
                charge = minutes * PARKING_RATE + (minutes * ELECTRIC_RATE if car["type"] == "Electric" else 0)
                self.revenue += charge
                self.cars_served += 1
                self._log("payment", f"{plate} paid {charge:.2f} ({minutes} min)", plate)
                self._open_gate("gate1")
                self._log("exit", f"{plate} left the car park", plate)
                self.sessions.appendleft(self._visit_row(car, car["spot"], charge, "Completed"))
                del self.cars[plate]

    def _gates_and_fans(self):
        for g in self.gates:
            if g.get("manual") == "open":                  # held open by an operator
                g["state"], g["close_at"] = "Open", None
            elif g.get("manual") == "closed":              # held shut by an operator
                g["state"], g["close_at"] = "Closed", None
            elif g["state"] == "Open" and g["close_at"] and self.now >= g["close_at"]:
                g["state"], g["close_at"] = "Closed", None
        for f, z in zip(self.fans, ZONES):
            if f["health"] != "ok":
                f["on"] = False
                continue
            if f.get("manual") == "on":                # an operator took over the fan
                f["on"] = True
            elif f.get("manual") == "off":
                f["on"] = False
            elif self.co[z] >= CO_MID and not f["on"]:
                f["on"] = True
                self._log("component", f"Exhaust fan {f['name']} switched on ({z})")
            elif self.co[z] < 40 and f["on"]:
                f["on"] = False
            if f["on"]:
                f["hours"] += STEP_S / 3600.0

    def _co(self):
        moving = {z: 0 for z in ZONES}
        parked = {z: 0 for z in ZONES}
        for car in self.cars.values():
            z = self.spots[car["spot"]]["zone"] if car["spot"] else ZONES[0]     # passing cars stay near the entrance
            if car["state"] in ("arriving", "to_exit", "passing"):
                moving[z] += 1
            elif car["state"] == "parked":
                parked[z] += 1
        for f, z in zip(self.fans, ZONES):
            target = 6 + 9 * moving[z] + 0.4 * parked[z]
            if f["on"]:
                target *= 0.55
            target += self.co_extra[z] * (0.75 if f["on"] else 1.0)
            self.co[z] += (target - self.co[z]) * 0.18 + self.rng.uniform(-0.6, 0.6)
            self.co[z] = max(2.0, self.co[z])

    def _scenarios(self):
        # CO buildup in ZONE2: ramps up for ~75 s, then fades
        if self.co_started:
            elapsed = (self.now - self.co_started).total_seconds()
            if elapsed <= 75:
                self.co_extra["ZONE2"] = min(140.0, elapsed * 2.4)
            else:
                self.co_extra["ZONE2"] *= 0.85
                if self.co_extra["ZONE2"] < 3:
                    self.co_extra["ZONE2"], self.co_started = 0.0, None
        # Gate breakdown timeline: broken -> repair started -> fixed
        if self.gate_break_at:
            g = self._gate("gate0")
            elapsed = (self.now - self.gate_break_at).total_seconds()
            if elapsed >= 25 and g["health"] == "broken":
                g["health"] = "maintenance"
                self._log("component", "Repair of gate0 started")
            if elapsed >= 55 and g["health"] == "maintenance":
                g["health"], self.gate_break_at = "ok", None
                self._log("component", "gate0 repaired and back in service")
        # rogue flags clear once the car is gone
        for plate in [p for p in list(self.until) if p.startswith("rogue:")]:
            if plate[6:] not in self.cars:
                del self.until[plate]

    def _finish_repairs(self):
        for name, ends in list(self.repairs.items()):
            if self.now >= ends:
                comp = next((c for c in self.gates + self.fans if c["name"] == name), None)
                if comp:
                    comp["health"] = "ok"
                    self._log("component", f"{name} repaired and back in service")
                del self.repairs[name]

    def _visit_row(self, car, spot, charge, status):
        return {"plate": car["plate"], "car_type": car["type"], "spot": spot,
                "entered_at": car["entered"].strftime("%Y-%m-%d %H:%M:%S"), "left_at": self.now.strftime("%Y-%m-%d %H:%M:%S"),
                "minutes": round((self.now - car["entered"]).total_seconds() / 60.0, 1), "charge": round(charge, 2), "status": status}

    def _build_archive(self):
        """Made-up records for the previous 29 days, so the '30 days' view has something to show.
        A real backend would keep these in its database and delete anything older than 30 days."""
        rng = random.Random(4242)                    # fixed seed: the same 29 days every run
        hours = [1, 1, 1, 1, 1, 2, 4, 8, 10, 8, 6, 7, 8, 6, 5, 6, 9, 10, 7, 5, 3, 2, 1, 1]   # busy at 8-9, 12, 17-18
        today = self.now.date()
        self.archive_visits, self.archive_days = {}, []
        for back in range(1, 30):
            day = today - dt.timedelta(days=back)
            n = rng.randint(800, 1300) if day.weekday() >= 5 else rng.randint(1100, 1900)
            rows, edges = [], []
            for _ in range(n):
                car_type = rng.choices(["Normal", "Electric", "Accessible"], [0.72, 0.2, 0.08])[0]
                start = dt.datetime.combine(day, dt.time(0)) + dt.timedelta(hours=rng.choices(range(24), hours)[0],
                                                                            minutes=rng.randint(0, 59), seconds=rng.randint(0, 59))
                plate = f"{rng.choice('WBPV')}{rng.choice(string.ascii_uppercase)}{rng.choice(string.ascii_uppercase)} {rng.randint(100, 9999)}"
                if rng.random() < DRIVE_THROUGH_SHARE:
                    minutes, spot, charge, status = round(rng.uniform(0.3, 0.7), 1), "-", 0.0, "Drove through"
                else:
                    minutes = round(rng.uniform(3, 8), 1)
                    pool = ELECTRIC_SPOTS if car_type == "Electric" else ACCESSIBLE_SPOTS if car_type == "Accessible" else set()
                    spot = rng.choice(sorted(pool, key=_natural)) if pool else f"S{rng.randint(1, len(ZONES) * SPOTS_PER_ZONE)}"
                    m = max(1, round(minutes))
                    charge, status = m * PARKING_RATE + (m * ELECTRIC_RATE if car_type == "Electric" else 0), "Completed"
                    edges += [(start, 1), (start + dt.timedelta(minutes=minutes), -1)]
                rows.append({"plate": plate, "car_type": car_type, "spot": spot, "entered_at": start.strftime("%Y-%m-%d %H:%M:%S"),
                             "left_at": (start + dt.timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S"),
                             "minutes": minutes, "charge": round(charge, 2), "status": status})
            rows.sort(key=lambda r: r["entered_at"], reverse=True)
            level = peak = 0
            for _, d in sorted(edges):
                level += d
                peak = max(peak, level)
            parked = [r for r in rows if r["status"] == "Completed"]
            self.archive_visits[day.isoformat()] = rows
            self.archive_days.append({"date": day.isoformat(), "visits": len(rows), "cars_parked": len(parked),
                                      "drive_through": len(rows) - len(parked), "income": round(sum(r["charge"] for r in rows), 2),
                                      "penalties": rng.randint(0, 6), "peak_pct": min(100, round(100 * peak / (len(ZONES) * SPOTS_PER_ZONE))),
                                      "avg_minutes": round(sum(r["minutes"] for r in parked) / max(1, len(parked)), 1)})

    def _today_row(self):
        rows = list(self.sessions)
        inside = list(self.cars.values())
        parked = [r for r in rows if r["status"] == "Completed"] + [c for c in inside if c["state"] != "passing"]
        today = self.now.strftime("%Y-%m-%d")
        peaks = [100 * h["occupied"] / h["total"] for h in self.history_occ if h["total"] and h["t"].strftime("%Y-%m-%d") == today]
        done = [r for r in rows if r["status"] == "Completed"]
        return {"date": today, "visits": len(rows) + len(inside), "cars_parked": len(parked),
                "drive_through": sum(1 for r in rows if r["status"] == "Drove through") + sum(1 for c in inside if c["state"] == "passing"),
                "income": round(self.revenue, 2), "penalties": len(self.penalties), "peak_pct": round(max(peaks)) if peaks else 0,
                "avg_minutes": round(sum(r["minutes"] for r in done) / max(1, len(done)), 1)}

    def control(self, kind, name, action, role="operator"):
        """Manual control from the dashboard buttons. Follows the organisers' rules: never operate a component that is
        broken or under repair. Returns {"ok": bool, "message": str}."""
        with self.lock:
            role = str(role).lower()
            if role not in ("operator", "admin"):
                return {"ok": False, "message": "Only an Operator or an Admin can control components."}
            comp = next((c for c in (self.gates if kind == "gate" else self.fans if kind == "fan" else []) if c["name"] == name), None)
            if comp is None:
                return {"ok": False, "message": f"Unknown {kind} '{name}'."}
            who = role.capitalize()
            if action == "repair":
                if comp["health"] == "maintenance":
                    return {"ok": False, "message": f"{name} is already being repaired."}
                if comp["health"] != "broken":
                    return {"ok": False, "message": f"{name} is working, nothing to repair."}
                comp["health"] = "maintenance"
                self.repairs[name] = self.now + dt.timedelta(seconds=20)
                if name == "gate0":
                    self.gate_break_at = None
                self._log("component", f"{who} started the repair of {name}")
                return {"ok": True, "message": f"Repair of {name} started (about 20 s)."}
            if comp["health"] != "ok":
                state = "broken" if comp["health"] == "broken" else "under repair"
                return {"ok": False, "message": f"{name} is {state}. Repair it first, do not operate it."}
            if kind == "gate" and action in ("open", "close", "auto"):
                comp["close_at"] = None
                if action == "auto":
                    comp["manual"], comp["state"] = None, "Closed"
                    self._log("gate", f"{who} set {name} to automatic")
                    return {"ok": True, "message": f"{name} set to automatic."}
                comp["manual"] = "open" if action == "open" else "closed"
                comp["state"] = "Open" if action == "open" else "Closed"
                if action == "open":
                    comp["uses"] += 1
                self._log("gate", f"{who} {'opened' if action == 'open' else 'closed'} {name} (manual, stays that way until Auto)")
                return {"ok": True, "message": f"{name} {'opened' if action == 'open' else 'closed'} by hand. Press Auto to return to normal."}
            if kind == "fan" and action in ("on", "off", "auto"):
                comp["manual"] = None if action == "auto" else action
                self._log("component", f"{who} set {name} to {'automatic' if action == 'auto' else action.upper()}")
                return {"ok": True, "message": f"{name} set to {'automatic' if action == 'auto' else action}."}
            return {"ok": False, "message": f"'{action}' is not a valid action for a {kind}."}

    def history(self, date):
        """All visits of one past day (for the '30 days' view)."""
        with self.lock:
            return list(self.archive_visits.get(date, []))

    # ------------------------------------------------------------------ public API
    def advance_to_real_time(self):
        with self.lock:
            now = time.time()
            self._accum += (now - self._last_real) * self.speed
            self._last_real = now
            n = 0
            while self._accum >= STEP_S and n < 240:
                self._accum -= STEP_S
                self._step()
                n += 1

    def trigger(self, name):
        with self.lock:
            if name == "surge":
                self.until["surge"] = self.now + dt.timedelta(seconds=150)
                self._log("entry", "Traffic surge: many cars arriving at once (demo)")
            elif name == "co":
                self.co_started = self.now
                self._log("co", "Carbon monoxide rising in ZONE2 (demo)")
            elif name == "gate":
                g = self._gate("gate0")
                if g["health"] == "ok":
                    g["health"], g["state"], g["manual"], self.gate_break_at = "broken", "Closed", None, self.now
                    self._log("component", "gate0 (entrance barrier) broke down")
                    self._penalty("Gate broke: predictive maintenance was missed", 10, "gate0")
            elif name == "fan":
                f = self.fans[1]
                if f["health"] == "ok":
                    f["health"], f["on"], f["manual"] = "broken", False, None
                    self._log("component", f"{f['name']} (ZONE2 exhaust fan) broke down")
            elif name == "rogue":
                candidates = [c for c in self.cars.values() if c["state"] in ("parked", "arriving") and not c["flag"]]
                free = [s for s in self.spots.values() if s["state"] == "available" and s["health"] == "ok"
                        and s["type"] == "Any"]
                if candidates and free:
                    car = self.rng.choice(candidates)
                    old = self.spots[car["spot"]]
                    new = self.rng.choice([s for s in free if s["zone"] != old["zone"]] or free)
                    old["state"], old["car"] = "available", None
                    new["state"], new["car"] = "occupied", car["plate"]
                    car["flag"], car["assigned"], car["spot"] = "rogue", old["name"], new["name"]
                    car["state"], car["parked_at"] = "parked", car["parked_at"] or self.now
                    self.until["rogue:" + car["plate"]] = self.now + dt.timedelta(days=1)
                    self._log("rogue", f"ROGUE: {car['plate']} was sent to {old['name']} but parked in {new['name']}",
                              car["plate"])
                    self._penalty("Car parked in a spot it was not sent to", 10, new["name"])
            elif name == "reset":
                self.__init__(seed=self.rng.randint(1, 9999))

    def snapshot(self):
        self.advance_to_real_time()
        with self.lock:
            spots = [{k: v for k, v in s.items()} for s in sorted(self.spots.values(), key=lambda s: _natural(s["name"]))]
            for s in spots:
                car = self.cars.get(s["car"]) if s["car"] else None
                s["flag"] = car["flag"] if car else None
            cars = []
            for c in self.cars.values():
                minutes_inside = (self.now - c["entered"]).total_seconds() / 60.0
                minutes_parked = ((self.now - c["parked_at"]).total_seconds() / 60.0) if c["parked_at"] else 0.0
                est = round(max(1, round(minutes_parked)) * (PARKING_RATE + (ELECTRIC_RATE if c["type"] == "Electric" else 0)), 2) \
                    if c["parked_at"] else 0.0
                cars.append({"plate": c["plate"], "car_type": c["type"],
                             "status": {"arriving": "Heading to spot", "parked": "Parked", "to_exit": "Heading to exit", "passing": "Passing through"}[c["state"]],
                             "spot": c["spot"] or "-", "entered_at": c["entered"].strftime("%H:%M:%S"),
                             "minutes_inside": round(minutes_inside, 1), "planned_minutes": round(c["planned_s"] / 60.0, 1),
                             "estimated_charge": est, "flag": c["flag"], "assigned_spot": c.get("assigned")})
            cars.sort(key=lambda c: c["entered_at"], reverse=True)
            horizon = self.now - dt.timedelta(minutes=60)
            buckets = {}
            for t in self.arrivals:
                if t >= horizon:
                    b = t.replace(second=0, microsecond=0, minute=(t.minute // 5) * 5)
                    buckets[b] = buckets.get(b, 0) + 1
            start = self.now.replace(second=0, microsecond=0, minute=(self.now.minute // 5) * 5) - dt.timedelta(minutes=55)
            arrivals = [{"t": (start + dt.timedelta(minutes=5 * i)).isoformat(timespec="seconds"),
                         "count": buckets.get(start + dt.timedelta(minutes=5 * i), 0)} for i in range(12)]
            fans = [dict(f, hours=round(f["hours"], 2)) for f in self.fans]
            gates = [{k: v for k, v in g.items() if k != "close_at"} for g in self.gates]
            return {
                "generated_at": self.now.strftime("%Y-%m-%d %H:%M:%S"),
                "source": "mock",
                "spots": spots,
                "cars": cars,
                "events": list(self.events)[:200],
                "sessions": list(self.sessions)[:300],
                "archive": [self._today_row()] + list(self.archive_days),
                "gates": gates,
                "fans": fans,
                "zones": [{"name": z, "co_ppm": round(self.co[z], 1), "risk": co_risk(self.co[z])} for z in ZONES],
                "history": {
                    "occupancy": [{"t": h["t"].isoformat(timespec="seconds"), "occupied": h["occupied"], "total": h["total"]}
                                  for h in self.history_occ if h["t"] >= horizon],
                    "co": [{"t": h["t"].isoformat(timespec="seconds"), "zone": h["zone"], "ppm": h["ppm"]}
                           for h in self.history_co if h["t"] >= horizon],
                    "arrivals": arrivals,
                },
                "stats": {"revenue": round(self.revenue, 2), "cars_served": self.cars_served,
                          "penalty_count": len(self.penalties), "penalty_total": sum(p["amount"] for p in self.penalties),
                          "refused_last_10min": sum(1 for t in self.refused if t >= self.now - dt.timedelta(minutes=10))},
                "penalties": list(self.penalties)[:15],
            }
