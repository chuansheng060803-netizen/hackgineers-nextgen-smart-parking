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

ZONES = ["ZONE1", "ZONE2", "ZONE3"]
SPOTS_PER_ZONE = 10
STEP_S = 5.0                                   # the simulation moves in 5-second steps
CO_MID, CO_HIGH, CO_CRITICAL = 50.0, 100.0, 200.0   # ppm; the organisers say 50 counts as "medium"
ACCESSIBLE_SPOTS = {"S1", "S2", "S11"}
ELECTRIC_SPOTS = {"S9", "S10", "S19", "S20", "S29", "S30"}
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
        self.until = {}                              # scenario name -> sim time it ends
        self.gate_break_at = None
        self.co_started = None
        self.spots = {}
        self.cars = {}
        self.events = deque(maxlen=300)
        self.penalties = deque(maxlen=300)
        self.arrivals = deque(maxlen=3000)
        self.history_occ = deque(maxlen=900)
        self.history_co = deque(maxlen=900)
        self.revenue = 0.0
        self.cars_served = 0
        self.refused = deque(maxlen=200)
        self._build()
        # Fast-forward 30 minutes so charts have history the moment the page opens.
        self.now = dt.datetime.now().replace(microsecond=0) - dt.timedelta(minutes=30)
        self._next_sample = self.now
        for _ in range(int(30 * 60 / STEP_S)):
            self._step()
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
            {"name": "gate0", "zone": "", "role": "Entrance", "state": "Closed", "health": "ok", "uses": 0, "close_at": None},
            {"name": "gate1", "zone": "", "role": "Exit", "state": "Closed", "health": "ok", "uses": 0, "close_at": None},
            {"name": "gate2", "zone": "ZONE2", "role": "Zone door", "state": "Closed", "health": "ok", "uses": 0, "close_at": None},
            {"name": "gate3", "zone": "ZONE3", "role": "Zone door", "state": "Closed", "health": "ok", "uses": 0, "close_at": None},
        ]
        self.fans = [{"name": f"fan{i}", "zone": z, "on": False, "health": "ok", "hours": 0.0}
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
        if g["health"] != "ok":
            return False
        g["state"], g["uses"] = "Open", g["uses"] + 1
        g["close_at"] = self.now + dt.timedelta(seconds=seconds)
        return True

    def _active(self, name):
        return name in self.until and self.now < self.until[name]

    # ------------------------------------------------------------------ one simulation step
    def _rate(self):
        minutes = (self.now - dt.datetime(2026, 1, 1)).total_seconds() / 60.0
        base = (1 / 30.0) * (1 + 0.55 * math.sin(minutes / 3.0))       # arrivals per second, slow waves
        return base * (10.0 if self._active("surge") else 1.0)

    def _step(self):
        self.now += dt.timedelta(seconds=STEP_S)
        self._arrivals()
        self._progress_cars()
        self._gates_and_fans()
        self._co()
        self._scenarios()
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
        spot = self._allocate(car_type)
        self.arrivals.append(self.now)
        if not spot:
            self.refused.appendleft(self.now)
            self._log("refused", f"{plate} sent away: car park full", plate)
            return
        spot["state"], spot["car"] = "reserved", plate
        self.cars[plate] = {"plate": plate, "type": car_type, "state": "arriving", "spot": spot["name"],
                            "entered": self.now, "arrive_at": self.now + dt.timedelta(seconds=self.rng.randint(6, 12)),
                            "planned_s": self.rng.randint(60, 240), "parked_at": None, "leave_at": None, "flag": None}
        self._open_gate("gate0")
        if spot["zone"] in ("ZONE2", "ZONE3"):
            self._open_gate("gate2" if spot["zone"] == "ZONE2" else "gate3", 8)
        self._log("entry", f"{plate} ({car_type}) entered, sent to {spot['name']}", plate)

    def _progress_cars(self):
        for plate, car in list(self.cars.items()):
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
            elif car["state"] == "to_exit" and self.now >= car["leave_at"]:
                minutes = max(1, round(car["planned_s"] / 60.0))
                charge = minutes * PARKING_RATE + (minutes * ELECTRIC_RATE if car["type"] == "Electric" else 0)
                self.revenue += charge
                self.cars_served += 1
                self._log("payment", f"{plate} paid {charge:.2f} ({minutes} min)", plate)
                self._open_gate("gate1")
                self._log("exit", f"{plate} left the car park", plate)
                del self.cars[plate]

    def _gates_and_fans(self):
        for g in self.gates:
            if g["state"] == "Open" and g["close_at"] and self.now >= g["close_at"]:
                g["state"], g["close_at"] = "Closed", None
        for f, z in zip(self.fans, ZONES):
            if f["health"] != "ok":
                f["on"] = False
                continue
            if self.co[z] >= CO_MID and not f["on"]:
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
            z = self.spots[car["spot"]]["zone"]
            if car["state"] in ("arriving", "to_exit"):
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
                    g["health"], g["state"], self.gate_break_at = "broken", "Closed", self.now
                    self._log("component", "gate0 (entrance barrier) broke down")
                    self._penalty("Gate broke: predictive maintenance was missed", 10, "gate0")
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
                             "status": {"arriving": "Heading to spot", "parked": "Parked", "to_exit": "Heading to exit"}[c["state"]],
                             "spot": c["spot"], "entered_at": c["entered"].strftime("%H:%M:%S"),
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
                "events": list(self.events)[:40],
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
