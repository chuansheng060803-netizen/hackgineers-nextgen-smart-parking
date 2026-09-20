"""A simple, explainable forecast for the owner's "what to expect next" graph.

No machine-learning library: two plain methods, chosen by how much history exists,
and the dashboard says which one was used and how many hours it saw.

  * 48 hours or more of history  -> "daily pattern": for every hour of the day, the
    average of that same hour on earlier days (up to the last 7), scaled by how the
    last 24 hours compare with that pattern (so a busier-than-usual day lifts the
    whole forecast).
  * 3 to 47 hours                -> "recent level": a smoothed average of the latest
    hours, carried forward flat (there is not yet enough to know the daily rhythm).
  * fewer than 3 hours           -> no forecast (it would be a guess).

The "likely range" is the forecast plus or minus 1.28 times the typical miss of the
method on the history it saw (about an 80% band), never below zero. It is an
estimate from the past, not a promise.
"""
import numpy as np
import pandas as pd

MIN_HOURS = 3
DAILY_PATTERN_HOURS = 48
MAX_PATTERN_DAYS = 7
SMOOTHING = 0.35              # weight of the newest hour in the "recent level"
BAND_Z = 1.28                 # about an 80% range
SCALE_LIMITS = (0.5, 2.0)     # how far the last 24 h may lift or lower the daily pattern


def complete_hours(hourly):
    """Give a series of per-hour totals one entry for EVERY hour (a quiet hour is 0)."""
    hourly = hourly.sort_index()
    if hourly.empty:
        return hourly
    full = pd.date_range(hourly.index.min(), hourly.index.max(), freq="h")
    return hourly.reindex(full, fill_value=0.0).astype(float)


def _daily_pattern(y):
    """Average per hour-of-day over the latest days; returns (pattern by hour, fitted values)."""
    frame = pd.DataFrame({"y": y.values, "hour": y.index.hour, "day": y.index.normalize()},
                         index=y.index)
    days = sorted(frame["day"].unique())[-MAX_PATTERN_DAYS:]
    recent = frame[frame["day"].isin(days)]
    pattern = recent.groupby("hour")["y"].mean().reindex(range(24))
    pattern = pattern.fillna(recent["y"].mean() if len(recent) else 0.0)
    return pattern, y.index.hour.map(pattern).to_numpy(dtype=float)


def _recent_level(y):
    """Smoothed level after each hour, and the level before each hour (for the misses)."""
    level = y.iloc[0]
    before = []
    for value in y.values:
        before.append(level)
        level = SMOOTHING * value + (1 - SMOOTHING) * level
    return level, np.array(before, dtype=float)


def forecast_hourly(y, horizon_hours=24, fill_gaps=True):
    """Forecast the next `horizon_hours` hours of a per-hour series.

    `y` holds COMPLETE hours only (the hour still running would look too low).
    fill_gaps=True  : an hour missing from `y` counts as 0.
    fill_gaps=False : `y` already holds exactly the hours the car park was operating, and
                      the gaps (simulator off) are left out instead of counted as zero.
    Returns (frame, info): frame has time, forecast, low, high; info says how it was made.
    """
    y = complete_hours(y) if fill_gaps else y.sort_index().astype(float)
    n = len(y)
    info = {"history_hours": n, "method": "none", "note": ""}
    if n < MIN_HOURS or horizon_hours < 1:
        info["note"] = f"Needs at least {MIN_HOURS} hours of data to estimate ({n} so far)."
        return pd.DataFrame(columns=["time", "forecast", "low", "high"]), info

    future = pd.date_range(y.index[-1] + pd.Timedelta(hours=1), periods=horizon_hours, freq="h")
    days_seen = len(set(y.index.normalize()))
    if n >= DAILY_PATTERN_HOURS and days_seen >= 2:
        pattern, fitted = _daily_pattern(y)
        last_day = y.iloc[-24:]
        expected_last_day = float(np.sum(fitted[-24:]))
        scale = float(last_day.sum() / expected_last_day) if expected_last_day > 0 else 1.0
        scale = min(max(scale, SCALE_LIMITS[0]), SCALE_LIMITS[1])
        point = np.array([pattern[h] * scale for h in future.hour], dtype=float)
        misses = y.values - fitted
        info.update(method="daily pattern",
                    note=f"Same hour on earlier days, scaled by the last 24 hours (x{scale:.2f}).")
    else:
        level, before = _recent_level(y)
        point = np.full(horizon_hours, level, dtype=float)
        misses = y.values - before
        info.update(method="recent level",
                    note="Not yet a full 2 days of history, so this is the recent level carried "
                         "forward. It will learn the daily rhythm as data builds up.")

    hour_sd = float(np.std(misses)) if len(misses) > 1 else 0.0
    info["hour_sd"] = hour_sd
    spread = BAND_Z * hour_sd
    frame = pd.DataFrame({
        "time": future,
        "forecast": np.clip(point, 0, None),
        "low": np.clip(point - spread, 0, None),
        "high": np.clip(point + spread, 0, None),
    })
    return frame, info


def total_range(frame, info):
    """(total, low, high) over the whole horizon. Hourly misses partly cancel out, so the
    range of a total grows with the square root of the hours, not with the hours."""
    if frame.empty:
        return 0.0, 0.0, 0.0
    total = float(frame["forecast"].sum())
    wiggle = BAND_Z * info.get("hour_sd", 0.0) * float(np.sqrt(len(frame)))
    return total, max(0.0, total - wiggle), total + wiggle
