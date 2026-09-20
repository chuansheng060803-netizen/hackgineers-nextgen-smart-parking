"""Make a DEMO database: 30 days of made-up traffic, for showing the month view and the forecast.

    python dashboard/make_demo_db.py                       # writes database/demo.db
    PARKING_DB_PATH=database/demo.db streamlit run dashboard/app.py

Clearly fake: the database is marked as demo data and the dashboard shows a "Demo data"
banner. The rules follow the simulator's own (cost = minutes parked, 30 bays, some
fake payments that are refused, some penalties), so the screens behave like the real thing.
It never touches the real database.
"""
import argparse
import heapq
import math
import random
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from store import Store  # noqa: E402

TIME = "%Y-%m-%d %H:%M:%S"
BAYS = [f"S{i}" for i in range(1, 31)]
HOURLY_SHAPE = [0.15, 0.1, 0.08, 0.08, 0.12, 0.3, 0.7, 1.4, 1.8, 1.5, 1.1, 1.0, 1.3, 1.2, 1.0, 1.1, 1.4,
                1.9, 1.7, 1.2, 0.8, 0.55, 0.35, 0.2]            # busy at 8-9 and 17-18
WEEKDAY_FACTOR = [1.0, 1.0, 1.0, 1.05, 1.2, 1.35, 0.8]         # Mon..Sun: the weekend is different


def plate(rng):
    letters = "ABCDEFGHJKLMNPRSTVWXYZ"
    return f"{''.join(rng.choice(letters) for _ in range(3))} {rng.randint(100, 999)}"


def generate(db_path, days=30, seed=7, end=None, per_hour=26):
    rng = random.Random(seed)
    end = end or datetime.now().replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=days)
    Path(db_path).unlink(missing_ok=True)
    store = Store(str(db_path), batch_size=500).start()
    seq = [1000]

    def send(cls, when, **fields):
        seq[0] += 1
        store.submit_event({"EventClass": cls, "EventId": str(uuid.uuid4()), "SequenceId": seq[0], "Signature": None,
                            "ServerDateTime": when.strftime(TIME), **fields}, received_at=when.strftime("%Y-%m-%dT%H:%M:%S+00:00"))

    # all the moments of the story, then written in time order so SequenceId follows the clock
    story, free_at, charge_id = [], {b: start for b in BAYS}, [int(start.timestamp() * 1000)]
    moment = start
    while moment < end:
        rate = per_hour * HOURLY_SHAPE[moment.hour] * WEEKDAY_FACTOR[moment.weekday()]
        for _ in range(_poisson(rng, rate)):
            arrive = moment + timedelta(seconds=rng.randint(0, 3599))
            minutes = max(2, min(240, int(rng.lognormvariate(math.log(22), 0.7))))
            bay = next((b for b in BAYS if free_at[b] <= arrive), None)
            if bay is None:
                continue                                        # car park full: the car drives off
            car, kind = plate(rng), "Normal"
            parked_in = arrive + timedelta(seconds=40)
            parked_out = parked_in + timedelta(minutes=minutes)
            free_at[bay] = parked_out + timedelta(seconds=5)
            if parked_out >= end:
                continue                                        # still parked at the end: not part of the history
            cost = float(minutes)
            fake = rng.random() < 0.03
            fined = (not fake) and rng.random() < 0.012
            steps = [("EntrySpot", "ENTRY1", "CarIn", arrive), ("EntrySpot", "ENTRY1", "CarOut", arrive + timedelta(seconds=20)),
                     ("Park", bay, "CarIn", parked_in), ("Park", bay, "CarOut", parked_out),
                     ("ExitSpot", "EXIT_EXIT", "CarIn", parked_out + timedelta(seconds=3))]
            leave = parked_out + timedelta(seconds=12)
            if not fined:
                steps.append(("ExitSpot", "EXIT_EXIT", "CarOut", leave))
            for spot_type, spot, direction, when in steps:
                story.append((when, "car_spot_action", dict(CarPlateNumber=car, CarType=kind, SpotName=spot, SpotType=spot_type,
                                                            Direction=direction, PlannedParkingDurationInMinutes=str(minutes))))
            paid_at = parked_out + timedelta(seconds=8)
            charge_id[0] += 1
            if fined:
                story.append((leave, "penalty", dict(Reason="Car escaped without paying after parking for some time.",
                                                     FineAmount="10", Type="Car", ComponentName=car)))
                story.append((paid_at, "charge", dict(charge_id=charge_id[0], plate=car, cost=cost, paid=False, at=paid_at)))
            elif fake:
                story.append((paid_at, "payment_made", dict(CarPlateNumber=car, Amount=f"{cost + rng.choice([-1, 1, 5]):.2f}", Reason="Car Payment")))
                story.append((paid_at, "charge", dict(charge_id=charge_id[0], plate=car, cost=cost, paid=False, at=paid_at)))
                story.append((leave, "payment_made", dict(CarPlateNumber=car, Amount=f"{cost:.2f}", Reason="Car Payment")))
                story.append((leave, "charge", dict(charge_id=charge_id[0], plate=car, cost=cost, paid=True, at=leave)))
            else:
                story.append((paid_at, "payment_made", dict(CarPlateNumber=car, Amount=f"{cost:.2f}", Reason="Car Payment")))
                story.append((paid_at, "charge", dict(charge_id=charge_id[0], plate=car, cost=cost, paid=True, at=paid_at)))
        moment += timedelta(hours=1)

    story.sort(key=lambda item: item[0])
    for when, kind, fields in story:
        if kind == "charge":
            row = {"charge_id": fields["charge_id"], "CarPlateNumber": fields["plate"], "CarType": "Normal",
                   "billed_parking": fields["cost"], "billed_charging": 0.0, "billed_total": fields["cost"],
                   "billed_at": when.strftime(TIME), "status": "paid" if fields["paid"] else "billed",
                   "paid_at": fields["at"].strftime(TIME) if fields["paid"] else None}
            store.submit_record("charges", row)
        else:
            send(kind, when, **fields)

    # the live lists: every bay free, gates as normal, a quiet zone (the story ends with an empty car park)
    store.submit_state("list-parking-spots", [{"name": b, "purpose": "Park", "parkingForCarType": "Any", "zoneParent": "ZONE1",
                                               "detectedCars": 0, "broken": False, "isUnderMaintenance": False} for b in BAYS])
    store.submit_state("list-barriers", [{"name": g, "zoneParent": "ZONE1", "broken": False, "isUnderMaintenance": False,
                                          "state": s} for g, s in (("gateA", "Closed"), ("gateB", "Open"), ("gateC", "Open"))])
    store.submit_state("list-zones", [{"name": "ZONE1", "gasCarbonMonoxideLevel": 3, "risk": "Safe"}])
    store.submit_state("list-lights", [{"name": f"t_{i}", "group": "G1", "zoneParent": "ZONE1", "isOn": True} for i in range(4)])
    store.submit_state("list-alarms", [])
    store.submit_state("status", {"isActive": True, "cars": 0})
    store.flush(timeout=120)
    store.stop()

    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT OR REPLACE INTO ingest_meta (key, value, updated_at) VALUES ('demo_data', '1', ?)",
                 (datetime.now().strftime(TIME),))
    conn.commit()
    conn.close()
    return len([s for s in story if s[1] == "car_spot_action"]) // 6


def _poisson(rng, lam):
    """A random count with average `lam` (small counts only, so the simple method is fine)."""
    if lam <= 0:
        return 0
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(ROOT / "database" / "demo.db"))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()
    visits = generate(args.out, args.days, args.seed)
    print(f"Wrote {visits:,} visits over {args.days} days to {args.out} (marked as demo data).")


if __name__ == "__main__":
    main()
