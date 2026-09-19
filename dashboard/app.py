"""Smart parking dashboard (Streamlit).

Run from the repo root:   streamlit run dashboard/app.py
Uses mock data by default. See README.md for how to point it at the real backend.
"""
import datetime as dt

import pandas as pd
import streamlit as st

import charts
import data_source
import ui
from alerts import derive_alerts
from mock_data import MockWorld
from styles import css

st.set_page_config(page_title="Smart Parking Dashboard", page_icon="🅿️", layout="wide")


@st.cache_resource
def get_world():
    return MockWorld()


def detect_theme():
    choice = st.session_state.get("theme_choice", "Match browser")
    if choice == "Dark":
        return "dark"
    if choice == "Light":
        return "light"
    try:
        return "light" if st.context.theme.type == "light" else "dark"
    except Exception:
        return "dark"


def today_visits(snap):
    """Cars inside now plus everyone who already left since the dashboard started."""
    day = (snap.get("generated_at") or "")[:10]

    def entered(car):
        # the backend sends a full timestamp; the mock sends a bare time
        stamp = car.get("entered_at") or ""
        return stamp if stamp[:4].isdigit() else f"{day} {stamp}".strip()

    inside = [{"plate": c["plate"], "car_type": c["car_type"], "spot": c["spot"],
               "entered_at": entered(c), "left_at": "", "minutes": c["minutes_inside"],
               "charge": c["estimated_charge"],
               # the car's real state, not a flat "Inside": a car at the exit has
               # been billed and is waiting to pay, one still parked has not
               "status": c.get("status") or "Inside"} for c in snap["cars"]]
    return inside + snap.get("sessions", [])


def visits_table(rows, query, car_type):
    if query:
        rows = [r for r in rows if query in " ".join(str(v) for v in r.values()).lower()]
    if car_type != "All":
        rows = [r for r in rows if r.get("car_type") == car_type]
    cols = {"plate": "Plate", "car_type": "Type", "spot": "Spot", "entered_at": "Entered", "left_at": "Left",
            "minutes": "Minutes", "charge": "Charge", "status": "Status"}
    return pd.DataFrame(rows, columns=list(cols)).rename(columns=cols)


@st.cache_data(ttl=60, show_spinner=False)
def _api_history(url, date):
    return data_source.fetch_history(url, date)


def load_history(date):
    """All visits of one past day. Mock: from the fake archive. API: GET /api/history?date=..."""
    try:
        if data_source.mode() == "api":
            return _api_history(data_source.api_url(), date)
        return get_world().history(date)
    except Exception as exc:  # noqa: BLE001
        st.warning(f"Could not load {date}: {exc}")
        return []


def do_control(kind, name, action, role):
    """Runs when an operator button is pressed. Mock: changes the fake car park. API: POST /api/control/..."""
    ss = st.session_state
    try:
        if data_source.mode() == "api":
            res = data_source.send_control(data_source.api_url(), kind, name, action, role)
        else:
            res = get_world().control(kind, name, action, role)
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "message": f"Could not reach the backend: {exc}"}
    now = dt.datetime.now()
    ss["ctl_msg"] = {**res, "at": now}
    ss["ctl_log"] = ([{"Time": now.strftime("%H:%M:%S"), "Role": role, "Target": name, "Action": action,
                       "Result": "OK" if res.get("ok") else "Refused", "Message": res.get("message", "")}] + ss.get("ctl_log", []))[:100]


def controls_card(snap, role):
    with st.container(border=True):
        st.markdown(ui.card_title("Manual controls", f"signed in as {role}"), unsafe_allow_html=True)
        if role == "Viewer":
            st.caption("Read-only. Sign in as Operator or Admin (left sidebar) to open or close gates and control the fans.")
            return
        msg = st.session_state.get("ctl_msg")
        if msg and (dt.datetime.now() - msg["at"]).total_seconds() < 20:
            (st.success if msg.get("ok") else st.warning)(msg.get("message", ""))
        for g in snap["gates"]:
            health = g.get("health", "ok")
            note = {"broken": " · **Broken**", "maintenance": " · **Under repair**"}.get(
                health, f" · held {g.get('manual')} by hand" if g.get("manual") else " · automatic")
            st.markdown(f"**{g['name']}** {g.get('role') or ''} · {g.get('state') or '?'}{note}")
            b1, b2, b3, b4 = st.columns(4)
            manual = g.get("manual")
            b1.button("Open", key=f"c_{g['name']}_open", width="stretch", disabled=health != "ok" or manual == "open",
                      on_click=do_control, args=("gate", g["name"], "open", role))
            b2.button("Close", key=f"c_{g['name']}_close", width="stretch", disabled=health != "ok" or manual == "closed",
                      on_click=do_control, args=("gate", g["name"], "close", role))
            b3.button("Auto", key=f"c_{g['name']}_auto", width="stretch", disabled=health != "ok" or not manual,
                      on_click=do_control, args=("gate", g["name"], "auto", role))
            b4.button("Repair", key=f"c_{g['name']}_repair", width="stretch", disabled=health != "broken",
                      on_click=do_control, args=("gate", g["name"], "repair", role))
        if not snap["fans"]:
            st.caption("This car park has no exhaust fans to control.")
            return
        with st.expander(f"Exhaust fans ({len(snap['fans'])})"):
            for f in snap["fans"]:
                health = f.get("health", "ok")
                mode = "manual" if f.get("manual") else "automatic"
                speed = {"turbo": "TURBO", "normal": "running", "off": "off"}.get(f.get("speed") or ("normal" if f.get("on") else "off"), "running")
                note = {"broken": " · **Broken**", "maintenance": " · **Under repair**"}.get(health, f" · {speed}, {mode}")
                st.markdown(f"**{f['name']}** {f.get('zone', '')}{note}")
                b1, b2, b3, b4 = st.columns(4)
                for col, label, act in [(b1, "Turbo", "on"), (b2, "Off", "off"), (b3, "Auto", "auto")]:
                    col.button(label, key=f"c_{f['name']}_{act}", width="stretch", disabled=health != "ok",
                               on_click=do_control, args=("fan", f["name"], act, role))
                b4.button("Repair", key=f"c_{f['name']}_repair", width="stretch", disabled=health != "broken",
                          on_click=do_control, args=("fan", f["name"], "repair", role))


def load_snapshot():
    """Returns (snapshot, error_text). On failure keeps showing the last good snapshot."""
    ss = st.session_state
    try:
        if data_source.mode() == "api":
            snap = data_source.fetch_api(data_source.api_url())
        else:
            snap = data_source.normalise(get_world().snapshot())
            snap["source"] = "mock"
        ss["last_snapshot"], ss["last_ok"] = snap, dt.datetime.now()
        return snap, ""
    except Exception as exc:  # noqa: BLE001 - any failure should show a banner, not crash the page
        return ss.get("last_snapshot"), f"{type(exc).__name__}: {exc}"


# ------------------------------------------------------------------ sidebar
with st.sidebar:
    st.markdown("### Controls")
    st.selectbox("Theme", ["Match browser", "Dark", "Light"], key="theme_choice")
    st.selectbox("Signed in as", ["Viewer", "Operator", "Admin"], index=1, key="role")
    st.caption("Demo sign-in. The real login comes from the database/authentication teammate.")
    st.select_slider("Refresh every (seconds)", options=[1, 2, 3, 5, 10], value=2, key="refresh_s")
    st.caption(f"Data source: **{data_source.mode()}**" + (f" ({data_source.api_url()})" if data_source.mode() == "api" else ""))
    if data_source.mode() != "api":
        st.markdown("#### Demo the challenges")
        st.caption("Mock data only. Each button forces one scenario from the brief.")
        DEMOS = [("Traffic surge", "surge", "Traffic surge started. Watch the arrivals chart and alerts."),
                 ("CO buildup (ZONE2)", "co", "CO is rising in ZONE2. The warning appears once it passes 50 ppm (about 15 seconds)."),
                 ("Entrance gate breaks", "gate", "The entrance gate broke down. Cars are being turned away."),
                 ("Exhaust fan breaks (ZONE2)", "fan", "The ZONE2 exhaust fan broke down."),
                 ("Rogue car", "rogue", "A rogue car entered."),
                 ("Reset demo", "reset", "Demo reset.")]

        def run_demo(key, note):
            get_world().trigger(key)
            st.toast(note)

        for label, key, note in DEMOS:
            st.button(label, key=f"btn_{key}", width="stretch", on_click=run_demo, args=(key, note))

# ------------------------------------------------------------------ main (auto-refreshing fragment)


@st.fragment(run_every=f"{st.session_state.get('refresh_s', 2)}s")
def board():
    theme = detect_theme()
    st.markdown(css(theme), unsafe_allow_html=True)
    snap, err = load_snapshot()
    if snap is None:
        st.error(f"Cannot reach the data source yet. {err}")
        return
    if err:
        last = st.session_state.get("last_ok")
        st.warning(f"Data source not answering ({err}). Showing the last good data from "
                   f"{last.strftime('%H:%M:%S') if last else 'earlier'}.")

    spots, cars = snap["spots"], snap["cars"]
    alerts = snap.get("alerts") or derive_alerts(snap)

    zone_count = len({s.get("zone") for s in spots})
    subtitle = f"{len(spots)} spots in {zone_count} zone" + ("s" if zone_count != 1 else "")
    st.markdown(ui.header("Smart Parking Dashboard", subtitle,
                          snap.get("source", "?"), snap.get("generated_at") or "-", ok=not err,
                          note="stale" if err else ""), unsafe_allow_html=True)
    st.markdown(ui.alerts_block(alerts), unsafe_allow_html=True)
    st.markdown(ui.kpis(spots, cars, snap["stats"]), unsafe_allow_html=True)

    left, right = st.columns([2.1, 1], gap="medium")
    with left:
        st.markdown(ui.parking_map(spots), unsafe_allow_html=True)
        controls_card(snap, st.session_state.get("role", "Operator"))
    with right:
        st.markdown(ui.system_block(snap.get("source", "?"), not err, snap.get("generated_at"), spots, snap["gates"], snap["fans"], alerts),
                    unsafe_allow_html=True)
        if snap["zones"]:
            st.markdown(ui.co_block(snap["zones"]), unsafe_allow_html=True)
        st.markdown(ui.gates_block(snap["gates"], snap["fans"]), unsafe_allow_html=True)

    h = snap["history"]
    c1, c2, c3 = st.columns(3, gap="medium")
    for col, title, meta, fn, key in [
        (c1, "Occupancy", "last hour", charts.occupancy_chart, "occ"),
        (c2, "Carbon monoxide", "ppm per zone", charts.co_chart, "co"),
        (c3, "Arrivals", "per 5 minutes", charts.arrivals_chart, "arr"),
    ]:
        with col, st.container(border=True):
            st.markdown(ui.card_title(title, meta), unsafe_allow_html=True)
            st.plotly_chart(fn(h, theme), width="stretch", config={"displayModeBar": False}, key=f"chart_{key}")

    t1, t2 = st.columns([1.5, 1], gap="medium")
    with t1, st.container(border=True):
        st.markdown(ui.card_title("Cars inside", f"{len(cars)} right now"), unsafe_allow_html=True)
        if cars:
            df = pd.DataFrame(cars).rename(columns={"plate": "Plate", "car_type": "Type", "status": "Status", "spot": "Spot",
                                                    "entered_at": "Entered", "minutes_inside": "Min inside",
                                                    "estimated_charge": "Est. charge"})
            st.dataframe(df[["Plate", "Type", "Status", "Spot", "Entered", "Min inside", "Est. charge"]],
                         width="stretch", hide_index=True, height=min(430, 40 + 35 * len(df)))
        else:
            st.markdown('<div class="pk"><div class="pk-empty">The car park is empty.</div></div>', unsafe_allow_html=True)
    with t2, st.container(border=True):
        st.markdown(ui.card_title("Recent activity", "newest first"), unsafe_allow_html=True)
        st.markdown(ui.activity_feed(snap["events"]), unsafe_allow_html=True)

    with st.container(border=True):
        st.markdown(ui.card_title("History", "search what happened"), unsafe_allow_html=True)
        tab_today, tab_month = st.tabs(["Today", "Last 30 days"])
        today_rows = today_visits(snap)
        with tab_today:
            f1, f2, f3 = st.columns([2, 1, 1])
            q = f1.text_input("Search plate, spot or text", key="hist_q", placeholder="e.g. WBX, S12, paid").strip().lower()
            kind = f2.selectbox("Show", ["Visits", "Events"], key="hist_kind")
            typ = f3.selectbox("Type", ["All", "Normal", "Electric", "Accessible"], key="hist_type", disabled=kind == "Events")
            if kind == "Visits":
                df = visits_table(today_rows, q, typ)
            else:
                rows = snap["events"]
                if q:
                    rows = [r for r in rows if q in f"{r.get('kind', '')} {r.get('text', '')} {r.get('t', '')}".lower()]
                df = pd.DataFrame(rows, columns=["t", "kind", "text"]).rename(columns={"t": "Time", "kind": "Kind", "text": "What happened"})
            st.caption(f"{len(df)} result(s) since the dashboard started")
            st.dataframe(df, hide_index=True, width="stretch", height=min(380, 40 + 35 * max(len(df), 1)))

        with tab_month:
            days = snap.get("archive", [])
            if not days:
                st.markdown('<div class="pk"><div class="pk-empty">No daily records yet.</div></div>', unsafe_allow_html=True)
            else:
                st.caption(f"Every day is kept for up to 30 days ({len(days)} days available). Pick a day to open all of its visits.")
                st.plotly_chart(charts.daily_chart(days, theme), width="stretch", config={"displayModeBar": False}, key="chart_daily")
                sdf = pd.DataFrame(days).rename(columns={"date": "Date", "visits": "Visits", "cars_parked": "Parked", "drive_through": "Drove through",
                                                          "income": "Income", "penalties": "Penalties", "peak_pct": "Peak full %", "avg_minutes": "Avg minutes"})
                st.dataframe(sdf, hide_index=True, width="stretch", height=240)
                d1, d2 = st.columns([1, 2])
                day = d1.selectbox("Open a day", [d["date"] for d in days], key="arch_day")
                q2 = d2.text_input("Search that day", key="arch_q", placeholder="plate, spot, status...").strip().lower()
                day_rows = today_rows if day == days[0]["date"] else load_history(day)
                ddf = visits_table(day_rows, q2, "All")
                st.caption(f"{len(ddf)} of {len(day_rows)} visits on {day}")
                st.dataframe(ddf, hide_index=True, width="stretch", height=320)
                st.download_button("Download this day as CSV", ddf.to_csv(index=False), file_name=f"parking_{day}.csv", mime="text/csv", key="arch_dl")

    with st.expander("Table view of the chart data"):
        a, b, c = st.columns(3)
        occ = pd.DataFrame(h.get("occupancy", []))
        co = pd.DataFrame(h.get("co", []))
        arr = pd.DataFrame(h.get("arrivals", []))
        a.caption("Occupancy"); a.dataframe(occ, hide_index=True, width="stretch")
        b.caption("Carbon monoxide"); b.dataframe(co, hide_index=True, width="stretch")
        c.caption("Arrivals"); c.dataframe(arr, hide_index=True, width="stretch")
    if st.session_state.get("role") == "Admin":
        with st.expander(f"Control log ({len(st.session_state.get('ctl_log', []))} actions this session)"):
            log = st.session_state.get("ctl_log", [])
            if log:
                st.dataframe(pd.DataFrame(log), hide_index=True, width="stretch")
            else:
                st.caption("No manual control actions yet.")
    if snap.get("penalties"):
        with st.expander(f"Penalties ({snap['stats']['penalty_count']})"):
            st.dataframe(pd.DataFrame(snap["penalties"]), hide_index=True, width="stretch")


board()
