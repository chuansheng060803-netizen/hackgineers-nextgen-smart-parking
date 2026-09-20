"""Business numbers for the dashboard, calculated from plain tables (no database, no Streamlit).

Every function takes pandas data in and gives pandas data back, so it can be tested with
small hand-made tables. All times are the simulator's own clock (ServerDateTime text,
"YYYY-MM-DD HH:MM:SS") turned into naive timestamps; a period is [start, end) - the end
itself is not included.
"""
import numpy as np
import pandas as pd

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

GROUP_BY = {"15 minutes": "15min", "Hour": "h", "Day": "D", "Month": "MS"}

# An error (a penalty event) costs CREDITS, not money: it lowers our credit score and is never
# taken off the income. Five credits per error, whatever fine amount the simulator prints.
PENALTY_CREDITS = 5


def to_time(values):
    """Simulator time text -> timestamps (unreadable text becomes NaT, never an error)."""
    return pd.to_datetime(values, format=TIME_FORMAT, errors="coerce")


def as_text(ts):
    return pd.Timestamp(ts).strftime(TIME_FORMAT)


# ---------------------------------------------------------------------------
# periods and buckets
# ---------------------------------------------------------------------------

def presets(latest):
    """Ready-made periods measured from the newest data (so old or replayed data still works)."""
    day = pd.Timestamp(latest).normalize()
    month = day.replace(day=1)
    return {
        "Today": (day, day + pd.Timedelta(days=1)),
        "Yesterday": (day - pd.Timedelta(days=1), day),
        "Last 7 days": (day - pd.Timedelta(days=6), day + pd.Timedelta(days=1)),
        "Last 30 days": (day - pd.Timedelta(days=29), day + pd.Timedelta(days=1)),
        "This month": (month, month + pd.offsets.MonthBegin(1)),
    }


def auto_group(start, end):
    """Pick a sensible bucket size for a period: short periods by minute, long by month."""
    span = pd.Timestamp(end) - pd.Timestamp(start)
    if span <= pd.Timedelta(hours=6):
        return "15 minutes"
    if span <= pd.Timedelta(days=2):
        return "Hour"
    if span <= pd.Timedelta(days=92):
        return "Day"
    return "Month"


def _floor(ts, freq):
    ts = pd.Timestamp(ts)
    if freq == "MS":
        return ts.to_period("M").start_time
    if freq == "D":
        return ts.normalize()
    return ts.floor(freq)


def bucket_starts(start, end, freq):
    """The start of every bucket that overlaps [start, end)."""
    first = _floor(start, freq)
    starts = pd.date_range(first, pd.Timestamp(end), freq=freq)
    return starts[starts < pd.Timestamp(end)]


def sum_by_bucket(df, time_col, value_col, start, end, freq):
    """Total of `value_col` per bucket; a bucket with nothing in it shows 0, not a gap."""
    index = bucket_starts(start, end, freq)
    if df is None or df.empty:
        return pd.Series(0.0, index=index)
    inside = df[(df[time_col] >= pd.Timestamp(start)) & (df[time_col] < pd.Timestamp(end))]
    if inside.empty:
        return pd.Series(0.0, index=index)
    grouped = inside.set_index(time_col)[value_col].resample(freq).sum()
    return grouped.reindex(index, fill_value=0.0).astype(float)


def count_by_bucket(df, time_col, start, end, freq):
    """How many rows per bucket (0 for a quiet bucket)."""
    if df is None or df.empty:
        return pd.Series(0.0, index=bucket_starts(start, end, freq))
    ones = df[[time_col]].assign(one=1.0)
    return sum_by_bucket(ones, time_col, "one", start, end, freq)


def active_hours(*time_series):
    """The hours in which the car park was operating: any hour with at least one event.

    An hour with no events at all means the simulator was not running, which is not the
    same as an hour with no customers, so the forecast leaves those hours out.
    """
    stamps = pd.concat([pd.Series(t).dropna() for t in time_series], ignore_index=True)
    if stamps.empty:
        return pd.DatetimeIndex([])
    return pd.DatetimeIndex(stamps.dt.floor("h").unique()).sort_values()


def previous_period(start, end):
    """The period of the same length right before this one (for "vs previous")."""
    length = pd.Timestamp(end) - pd.Timestamp(start)
    return pd.Timestamp(start) - length, pd.Timestamp(start)


# ---------------------------------------------------------------------------
# visits: one row per car visit, rebuilt from the car_spot_action events
# ---------------------------------------------------------------------------

VISIT_COLUMNS = ["plate", "car_type", "arrived", "entered", "spot", "parked_in", "parked_out",
                 "at_exit", "left"]


STALE_AFTER = pd.Timedelta(minutes=30)


def build_visits(events):
    """Turn the stream of car_spot_action events into one row per visit.

    A visit starts when a car reaches the entrance (EntrySpot CarIn) and ends when it
    leaves through the exit (ExitSpot CarOut). The same plate can visit again later.
    Events are taken in SequenceId order, because the simulator's webhooks can arrive
    out of order.
    """
    if events is None or events.empty:
        return pd.DataFrame(columns=VISIT_COLUMNS + ["minutes_parked", "state"])

    ordered = events.sort_values("SequenceId", kind="stable")
    times = to_time(ordered["ServerDateTime"])
    finished, open_visits = [], {}

    def start_visit(plate, car_type):
        return {"plate": plate, "car_type": car_type, "arrived": pd.NaT, "entered": pd.NaT,
                "spot": None, "parked_in": pd.NaT, "parked_out": pd.NaT,
                "at_exit": pd.NaT, "left": pd.NaT}

    for row, when in zip(ordered.itertuples(index=False), times):
        plate, spot_type, direction = row.CarPlateNumber, row.SpotType, row.Direction
        if spot_type == "EntrySpot" and direction == "CarIn":
            if plate in open_visits:                       # the earlier visit never showed an exit
                finished.append(open_visits.pop(plate))
            visit = open_visits[plate] = start_visit(plate, row.CarType)
            visit["arrived"] = when
            continue
        visit = open_visits.get(plate)
        if visit is None:                                  # we started listening mid-visit
            visit = open_visits[plate] = start_visit(plate, row.CarType)
        if spot_type == "EntrySpot":                       # CarOut
            visit["entered"] = when
        elif spot_type == "Park":
            if direction == "CarIn":
                visit["spot"], visit["parked_in"] = row.SpotName, when
            else:
                visit["parked_out"] = when
        elif spot_type == "ExitSpot":
            if direction == "CarIn":
                visit["at_exit"] = when
            else:
                visit["left"] = when
                finished.append(open_visits.pop(plate))
    finished.extend(open_visits.values())

    visits = pd.DataFrame(finished, columns=VISIT_COLUMNS)
    for col in ("arrived", "entered", "parked_in", "parked_out", "at_exit", "left"):
        visits[col] = pd.to_datetime(visits[col])
    visits["minutes_parked"] = (visits["parked_out"] - visits["parked_in"]).dt.total_seconds() / 60
    newest = times.max()
    visits["state"] = visits.apply(lambda row: _visit_state(row, newest), axis=1)
    return visits.sort_values("arrived", na_position="last").reset_index(drop=True)


def _visit_state(row, newest):
    if pd.notna(row["left"]):
        return "Left"
    last_seen = max((row[c] for c in ("arrived", "entered", "parked_in", "parked_out", "at_exit")
                     if pd.notna(row[c])), default=pd.NaT)
    if pd.notna(newest) and pd.notna(last_seen) and newest - last_seen > STALE_AFTER:
        return "No exit seen"          # the recording has moved on; this car's exit was missed
    if pd.notna(row["at_exit"]) or pd.notna(row["parked_out"]):
        return "Leaving"
    if pd.notna(row["parked_in"]):
        return "Parked"
    return "Arriving"


def attach_charges(visits, charges):
    """Add what each visit was billed and whether the payment was accepted.

    A bill belongs to the visit of the same plate whose parking ended just before it.
    Visits without a bill (or data without a charges table) get empty values.
    """
    visits = visits.copy()
    visits["billed"] = np.nan
    visits["payment"] = None
    if charges is None or charges.empty or visits.empty:
        return visits
    key = visits.dropna(subset=["parked_out"]).sort_values("parked_out")
    bills = charges.sort_values("billed_at")
    matched = pd.merge_asof(
        key.assign(_key=key["parked_out"] - pd.Timedelta(seconds=10))[["plate", "_key"]],
        bills.rename(columns={"CarPlateNumber": "plate"})[["plate", "billed_at", "billed_total", "status"]],
        left_on="_key", right_on="billed_at", by="plate", direction="forward",
        tolerance=pd.Timedelta(minutes=5))
    matched.index = key.index
    visits.loc[key.index, "billed"] = matched["billed_total"]
    visits.loc[key.index, "payment"] = matched["status"].map({"paid": "Paid", "billed": "Not paid"})
    return visits


# ---------------------------------------------------------------------------
# occupancy
# ---------------------------------------------------------------------------

def occupancy_curve(events):
    """Number of occupied bays over time, counted PER BAY.

    A bay is occupied when its latest event is a CarIn. Counting "cars in minus cars out"
    instead would drift upwards for good after every missed CarOut (the backend was not
    running for a while); per-bay state repairs itself the next time that bay is used,
    and can never go above the number of bays.
    """
    if events is None or events.empty:
        return pd.Series(dtype=float)
    park = events[events["SpotType"] == "Park"].sort_values("SequenceId", kind="stable")
    if park.empty:
        return pd.Series(dtype=float)
    times = to_time(park["ServerDateTime"])
    state, occupied, counts, stamps = {}, 0, [], []
    for spot, direction, when in zip(park["SpotName"], park["Direction"], times):
        if pd.isna(when):
            continue
        now_in = 1 if direction == "CarIn" else 0
        occupied += now_in - state.get(spot, 0)
        state[spot] = now_in
        counts.append(float(occupied))
        stamps.append(when)
    curve = pd.Series(counts, index=pd.DatetimeIndex(stamps), dtype=float)
    return curve.groupby(level=0).last().sort_index()


def occupancy_by_bucket(curve, capacity, start, end, freq, until=None):
    """Highest occupancy (percent of the bays) reached in each bucket.

    A bucket with no event keeps the level it started with. Before the first event, and
    in buckets that start after `until` (the newest data, so the future), the value is
    unknown (NaN), not 0.
    """
    index = bucket_starts(start, end, freq)
    if curve is None or curve.empty or not capacity:
        return pd.Series(np.nan, index=index)
    full = pd.date_range(_floor(min(curve.index.min(), index[0]), freq),
                         max(curve.index.max(), index[-1]), freq=freq)
    biggest = curve.resample(freq).max().reindex(full)
    carried_in = curve.resample(freq).last().reindex(full).ffill().shift(1)
    peak = pd.concat([biggest, carried_in], axis=1).max(axis=1)
    result = (peak.reindex(index) / float(capacity) * 100.0).clip(lower=0.0)
    if until is not None:
        result[result.index > pd.Timestamp(until)] = np.nan
    return result


# ---------------------------------------------------------------------------
# headline numbers
# ---------------------------------------------------------------------------

def summarize(income, penalties, visits, start, end):
    """The headline numbers of a period. `income` and `penalties` have time + amount columns.

    Penalties are counted, not added up as money: each one costs PENALTY_CREDITS credits.
    """
    def total(df, time_col):
        if df is None or df.empty:
            return 0.0, 0
        inside = df[(df[time_col] >= pd.Timestamp(start)) & (df[time_col] < pd.Timestamp(end))]
        return float(inside["amount"].sum()), int(len(inside))

    earned, paying_cars = total(income, "time")
    _ignored_money, fines = total(penalties, "time")
    stays = pd.Series(dtype=float)
    if visits is not None and not visits.empty:
        done = visits[(visits["parked_out"] >= pd.Timestamp(start)) & (visits["parked_out"] < pd.Timestamp(end))]
        stays = done["minutes_parked"].dropna()
    return {
        "income": earned,
        "paying_cars": paying_cars,
        "penalty_count": fines,
        "credits_lost": fines * PENALTY_CREDITS,
        "income_per_car": earned / paying_cars if paying_cars else 0.0,
        "avg_stay_minutes": float(stays.mean()) if len(stays) else 0.0,
    }


def change_percent(now, before):
    """Percent change against the previous period; None when there is nothing to compare with."""
    if before is None or before == 0:
        return None
    return (now - before) / abs(before) * 100.0


def best_bucket(series):
    """(bucket start, value) of the biggest bucket, or None if everything is zero."""
    if series is None or series.empty or float(series.max()) <= 0:
        return None
    return series.idxmax(), float(series.max())
