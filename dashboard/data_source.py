"""Where the dashboard gets its data.

DASHBOARD_SOURCE=mock  (default)  -> built-in fake car park, no backend needed
DASHBOARD_SOURCE=api              -> GET {DASHBOARD_API_URL}/api/snapshot  (default http://localhost:8000)

The dashboard only ever *reads*. It never talks to the simulator and never changes the backend.
Missing keys in the backend's reply are filled with empty values so the page never crashes.
"""
import os

import requests

DEFAULTS = {"spots": [], "cars": [], "events": [], "sessions": [], "archive": [], "gates": [], "fans": [], "zones": [], "penalties": [],
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


def fetch_api(url, timeout=6):
    r = requests.get(url.rstrip("/") + "/api/snapshot", timeout=timeout)
    r.raise_for_status()
    return normalise(r.json())


def fetch_history(url, date, timeout=5):
    """All visits of one past day: GET {url}/api/history?date=YYYY-MM-DD  ->  [{plate, car_type, spot, entered_at, left_at, minutes, charge, status}]"""
    r = requests.get(url.rstrip("/") + "/api/history", params={"date": date}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def send_control(url, kind, name, action, role, timeout=5):
    """Manual control: POST {url}/api/control/{gate|fan}/{name}/{action}  body {"role": "operator"}  ->  {"ok": true, "message": "..."}
    The backend decides whether the role is allowed. Optional bearer token in DASHBOARD_API_TOKEN."""
    headers = {"Authorization": "Bearer " + os.environ["DASHBOARD_API_TOKEN"]} if os.environ.get("DASHBOARD_API_TOKEN") else {}
    r = requests.post(f"{url.rstrip('/')}/api/control/{kind}/{name}/{action}", json={"role": role.lower()}, headers=headers, timeout=timeout)
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code >= 400 and "ok" not in data:
        return {"ok": False, "message": data.get("message") or data.get("detail") or f"Backend answered {r.status_code}"}
    return {"ok": bool(data.get("ok", True)), "message": data.get("message", "Done.")}


def mode():
    return os.environ.get("DASHBOARD_SOURCE", "mock").lower()


def api_url():
    # "localhost" costs ~2 s per request on Windows (it tries IPv6 first), so use
    # the IPv4 address instead.
    return os.environ.get("DASHBOARD_API_URL", "http://localhost:8000").replace(
        "//localhost", "//127.0.0.1", 1)
