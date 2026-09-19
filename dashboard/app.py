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
    st.select_slider("Refresh every (seconds)", options=[1, 2, 3, 5, 10], value=2, key="refresh_s")
    st.caption(f"Data source: **{data_source.mode()}**" + (f" ({data_source.api_url()})" if data_source.mode() == "api" else ""))
    if data_source.mode() != "api":
        st.markdown("#### Demo the challenges")
        st.caption("Mock data only. Each button forces one scenario from the brief.")
        for label, key in [("Traffic surge", "surge"), ("CO buildup (ZONE2)", "co"), ("Entrance gate breaks", "gate"),
                           ("Rogue car", "rogue"), ("Reset demo", "reset")]:
            st.button(label, key=f"btn_{key}", width="stretch", on_click=lambda k=key: get_world().trigger(k))

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

    st.markdown(ui.header("Smart Parking Dashboard", f"{len(spots)} spots in {len({s.get('zone') for s in spots})} zones",
                          snap.get("source", "?"), snap.get("generated_at") or "-", ok=not err,
                          note="stale" if err else ""), unsafe_allow_html=True)
    st.markdown(ui.alerts_block(alerts), unsafe_allow_html=True)
    st.markdown(ui.kpis(spots, cars, snap["stats"]), unsafe_allow_html=True)

    left, right = st.columns([2.1, 1], gap="medium")
    with left:
        st.markdown(ui.parking_map(spots), unsafe_allow_html=True)
    with right:
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
        st.markdown(ui.card_title("History search", "parked cars and past visits"), unsafe_allow_html=True)
        f1, f2, f3 = st.columns([2, 1, 1])
        q = f1.text_input("Search plate, spot or text", key="hist_q", placeholder="e.g. WBX, S12, paid").strip().lower()
        kind = f2.selectbox("Show", ["Visits", "Events"], key="hist_kind")
        typ = f3.selectbox("Type", ["All", "Normal", "Electric", "Accessible"], key="hist_type", disabled=kind == "Events")
        if kind == "Visits":
            inside = [{"plate": c["plate"], "car_type": c["car_type"], "spot": c["spot"], "entered_at": f'{(snap.get("generated_at") or "")[:10]} {c["entered_at"]}'.strip(),
                       "left_at": "", "minutes": c["minutes_inside"], "charge": c["estimated_charge"], "status": "Inside"} for c in cars]
            rows = inside + snap.get("sessions", [])
            if q:
                rows = [r for r in rows if q in " ".join(str(v) for v in r.values()).lower()]
            if typ != "All":
                rows = [r for r in rows if r.get("car_type") == typ]
            cols = {"plate": "Plate", "car_type": "Type", "spot": "Spot", "entered_at": "Entered", "left_at": "Left",
                    "minutes": "Minutes", "charge": "Charge", "status": "Status"}
            df = pd.DataFrame(rows, columns=list(cols)).rename(columns=cols)
        else:
            rows = snap["events"]
            if q:
                rows = [r for r in rows if q in f"{r.get('kind', '')} {r.get('text', '')} {r.get('t', '')}".lower()]
            df = pd.DataFrame(rows, columns=["t", "kind", "text"]).rename(columns={"t": "Time", "kind": "Kind", "text": "What happened"})
        st.caption(f"{len(df)} result(s)")
        st.dataframe(df, hide_index=True, width="stretch", height=min(380, 40 + 35 * max(len(df), 1)))

    with st.expander("Table view of the chart data"):
        a, b, c = st.columns(3)
        occ = pd.DataFrame(h.get("occupancy", []))
        co = pd.DataFrame(h.get("co", []))
        arr = pd.DataFrame(h.get("arrivals", []))
        a.caption("Occupancy"); a.dataframe(occ, hide_index=True, width="stretch")
        b.caption("Carbon monoxide"); b.dataframe(co, hide_index=True, width="stretch")
        c.caption("Arrivals"); c.dataframe(arr, hide_index=True, width="stretch")
    if snap.get("penalties"):
        with st.expander(f"Penalties ({snap['stats']['penalty_count']})"):
            st.dataframe(pd.DataFrame(snap["penalties"]), hide_index=True, width="stretch")


board()
