"""Where the dashboard gets its data.

DASHBOARD_SOURCE=mock  (default)  -> built-in fake car park, no backend needed
DASHBOARD_SOURCE=api              -> GET {DASHBOARD_API_URL}/api/snapshot  (default http://localhost:8000)

The dashboard only ever *reads*. It never talks to the simulator and never changes the backend.
Missing keys in the backend's reply are filled with empty values so the page never crashes.
"""
import os

import requests

DEFAULTS = {"spots": [], "cars": [], "events": [], "gates": [], "fans": [], "zones": [], "penalties": [],
            "history": {"occupancy": [], "co": [], "arrivals": []},
            "stats": {"revenue": 0, "cars_served": 0, "penalty_count": 0, "penalty_total": 0, "refused_last_10min": 0}}


def normalise(data):
    out = {k: (v.copy() if isinstance(v, (dict, list)) else v) for k, v in DEFAULTS.items()}
    out.update({k: v for k, v in data.items() if v is not None})
    out["history"] = {**DEFAULTS["history"], **(data.get("history") or {})}
    out["stats"] = {**DEFAULTS["stats"], **(data.get("stats") or {})}
    out.setdefault("generated_at", "")
    out.setdefault("source", "api")
    return out


def fetch_api(url, timeout=3):
    r = requests.get(url.rstrip("/") + "/api/snapshot", timeout=timeout)
    r.raise_for_status()
    return normalise(r.json())


def mode():
    return os.environ.get("DASHBOARD_SOURCE", "mock").lower()


def api_url():
    return os.environ.get("DASHBOARD_API_URL", "http://localhost:8000")
