"""Grand Park Auto - business dashboard (Streamlit).

    streamlit run dashboard/app.py

The dashboard only READS the backend's database (database/parking.db, or PARKING_DB_PATH).
It never talks to the simulator. Start the backend first (python backend/run_all.py --live).
First time only: create a user with  python dashboard/create_user.py --username NAME --role operator

Screen order is business first: earnings and occupancy for any day, hours or month, the
forecast, then the live car park and the visit history.
"""
import html
import sys
import time
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

import biz_metrics as bm  # noqa: E402
import control_client  # noqa: E402
import db_read  # noqa: E402
import forecast as fc  # noqa: E402
import login  # noqa: E402
import ui_charts  # noqa: E402
import ui_theme  # noqa: E402

st.set_page_config(page_title="Grand Park Auto - Business dashboard", page_icon=":car:", layout="wide")
T = ui_theme.tokens(st)
st.markdown(ui_theme.css(T), unsafe_allow_html=True)

DB_PATH = db_read.default_db_path()
LIVE_REFRESH_S = 2
BUSINESS_REFRESH_S = 10
MAX_BUCKETS = 1500


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def credits(x):
    return f"{x:,.0f}" if abs(x) >= 1000 else f"{x:,.2f}"


def esc(x):
    return html.escape(str(x))


def describe_period(start, end):
    last = end - pd.Timedelta(seconds=1)
    if start.normalize() == last.normalize():
        text = start.strftime("%a %d %b %Y")
        if (end - start) < pd.Timedelta(days=1):
            text += f", {start:%H:%M} to {end:%H:%M}"
        return text
    return f"{start:%d %b %Y %H:%M} to {end:%d %b %Y %H:%M}"


def card(label, body_html, foot=""):
    foot_html = f'<div class="k-foot">{foot}</div>' if foot else ""
    return f'<div class="k-card k-hero-card"><div class="k-label">{label}</div>{body_html}{foot_html}</div>'


@st.cache_data(ttl=5, show_spinner=False)
def load_history(path):
    """Everything historic in one read. Cached for 5 s so several viewers do not repeat the work."""
    conn = db_read.open_db(path)
    try:
        events = db_read.car_events(conn)
        return {
            "events": events,
            "visits": bm.build_visits(events),
            "income": db_read.income(conn),
            "penalties": db_read.penalties(conn),
            "charges": db_read.charges(conn),
            "curve": bm.occupancy_curve(events),
            "event_times": bm.to_time(events["ServerDateTime"]) if not events.empty else pd.Series(dtype="datetime64[ns]"),
            "span": db_read.data_span(conn),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# sign-in
# ---------------------------------------------------------------------------

def require_login():
    """Show the sign-in form until someone is signed in. Returns {"username", "role"}."""
    user = st.session_state.get("user")
    if user:
        return user
    conn = db_read.open_db(DB_PATH)
    try:
        has_users = db_read.any_users(conn)
    finally:
        conn.close()

    _, middle, _ = st.columns([1, 1.2, 1])
    with middle:
        st.markdown('<div class="k-eyebrow">Grand Park Auto</div><div class="k-title">Sign in</div>', unsafe_allow_html=True)
        if not has_users:
            st.info("No users exist yet. In a terminal run:\n\n"
                    "`python dashboard/create_user.py --username YOUR_NAME --role operator`\n\n"
                    "then refresh this page.")
            st.stop()
        with st.form("signin"):
            name = st.text_input("Username", key="signin_name")
            password = st.text_input("Password", type="password", key="signin_password")
            go = st.form_submit_button("Sign in", type="primary")
        if go:
            failures = st.session_state.get("failures", 0)
            if failures:
                time.sleep(min(2 ** failures, 8))          # each wrong try makes the next one slower
            conn = db_read.open_db(DB_PATH)
            try:
                role = login.authenticate(db_read.user_row(conn, name.strip()), password)
            finally:
                conn.close()
            if role:
                st.session_state["user"] = {"username": name.strip(), "role": role}
                st.session_state["failures"] = 0
                st.rerun()
            st.session_state["failures"] = failures + 1
            st.error("Wrong username or password.")
    st.stop()


# ---------------------------------------------------------------------------
# header
# ---------------------------------------------------------------------------

def _health():
    conn = db_read.open_db(DB_PATH)
    try:
        return db_read.health(conn)
    finally:
        conn.close()


@st.fragment(run_every=5)
def connection_pill():
    level, _message, _age = _health()
    kind = {"live": "good", "delayed": "warning", "offline": "critical", "empty": "warning", "demo": "neutral"}[level]
    label = {"live": "Live", "delayed": "Delayed", "offline": "Backend offline", "empty": "No data yet", "demo": "Demo data"}[level]
    st.markdown(f'<div style="margin-top:8px;white-space:nowrap">{ui_theme.pill(kind, label)}</div>', unsafe_allow_html=True)


@st.fragment(run_every=5)
def connection_banner():
    level, message, _age = _health()
    if level not in ("live", "demo"):
        st.markdown(f'<div class="k-banner">{esc(message)}</div>', unsafe_allow_html=True)


def header(user):
    left, pill, who, out = st.columns([4, 1.5, 2.2, 1])
    with left:
        st.markdown('<div class="k-eyebrow">Grand Park Auto</div><div class="k-title">Car park business dashboard</div>',
                    unsafe_allow_html=True)
    with pill:
        connection_pill()
    with who:
        role = "Operator" if user["role"] == "operator" else "Admin (view only)"
        st.markdown(f'<div class="k-foot" style="text-align:right;margin-top:10px">{esc(user["username"])} &middot; {role}</div>',
                    unsafe_allow_html=True)
    with out:
        if st.button("Sign out", key="signout"):
            st.session_state.pop("user", None)
            st.rerun()
    connection_banner()
    conn = db_read.open_db(DB_PATH)
    try:
        demo = db_read.is_demo(conn)
    finally:
        conn.close()
    if demo:
        st.markdown('<div class="k-banner"><b>Demo data.</b> These numbers are generated for the presentation, '
                    'not taken from the simulator.</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# period controls
# ---------------------------------------------------------------------------

def period_controls(latest):
    presets = bm.presets(latest)
    choice = st.segmented_control("Period", list(presets) + ["Custom"], default="Today", key="period") or "Today"
    if choice == "Custom":
        c1, c2, c3, c4 = st.columns(4)
        day = latest.date()
        d1 = c1.date_input("From date", value=day, key="d1")
        h1 = c2.selectbox("From hour", list(range(24)), index=0, format_func=lambda h: f"{h:02d}:00", key="h1")
        d2 = c3.date_input("To date", value=day, key="d2")
        h2 = c4.selectbox("To hour", list(range(1, 25)), index=23, format_func=lambda h: f"{h % 24:02d}:00" + (" (end of day)" if h == 24 else ""), key="h2")
        start = pd.Timestamp(d1) + pd.Timedelta(hours=h1)
        end = pd.Timestamp(d2) + pd.Timedelta(hours=h2)
        if end <= start:
            st.warning("The end must be after the start.")
            st.stop()
    else:
        start, end = presets[choice]
    pick = st.segmented_control("Group by", ["Auto"] + list(bm.GROUP_BY), default="Auto", key="group") or "Auto"
    name = bm.auto_group(start, end) if pick == "Auto" else pick
    if len(bm.bucket_starts(start, end, bm.GROUP_BY[name])) > MAX_BUCKETS:
        name = bm.auto_group(start, end)
        st.caption(f"That grouping would draw too many bars for this period, so it was set to {name.lower()}.")
    return start, end, name


# ---------------------------------------------------------------------------
# business section
# ---------------------------------------------------------------------------

def capacity_and_now(conn):
    """(number of bays, bays taken right now, bays out of service) from the live list, or Nones."""
    state = db_read.live_state(conn)
    bays = db_read.car_park_spots(state)
    if bays.empty:
        return None, None, 0
    taken = bays["detectedCars"].fillna(0) > 0
    out = (bays["broken"].fillna(0).astype(bool) | bays["isUnderMaintenance"].fillna(0).astype(bool)) & ~taken
    return len(bays), int(taken.sum()), int(out.sum())


def hero_row(hist, start, end, capacity, taken_now, live=True, out_of_service=0):
    s = bm.summarize(hist["income"], hist["penalties"], hist["visits"], start, end)
    ps, pe = bm.previous_period(start, end)
    latest = hist["span"][1]
    compare_with = "the previous period"
    if start <= latest < end:                      # still running: compare the same stretch, not a whole earlier day
        pe = ps + (latest - start) + pd.Timedelta(seconds=1)
        compare_with = "the same stretch of the previous period"
    before = bm.summarize(hist["income"], hist["penalties"], hist["visits"], ps, pe)
    change = bm.change_percent(s["income"], before["income"])
    if change is None:
        change_html = '<span style="color:var(--k-muted)">no earlier period to compare</span>'
    else:
        arrow, cls = ("&#9650;", "k-up") if change >= 0 else ("&#9660;", "k-down")
        change_html = f'<span class="{cls}">{arrow} {abs(change):.0f}%</span> vs {compare_with}'

    income = hist["income"]
    unverified = 0
    if not income.empty:
        inside = income[(income["time"] >= start) & (income["time"] < end)]
        unverified = int((~inside["verified"]).sum())
    warn = ""
    if unverified:
        warn = (f'<br><span style="color:var(--k-muted)">{unverified} of these payments were recorded before bill-checking '
                'started, so fake payments may be included.</span>')
    left, mid, right = st.columns([2.2, 1.3, 1.3])
    with left:
        st.markdown(card("Income", f'<div class="k-hero">{credits(s["income"])}<small>credits</small></div>',
                         f'{change_html}<br>{s["paying_cars"]:,} paying {"car" if s["paying_cars"] == 1 else "cars"} &middot; {credits(s["income_per_car"])} per car{warn}'),
                    unsafe_allow_html=True)
    with mid:
        if capacity and taken_now is not None:
            free = max(capacity - taken_now - out_of_service, 0)
            pct = 100.0 * taken_now / capacity
            body = ('<div style="display:flex;gap:26px">'
                    f'<div><div class="k-value">{taken_now}</div><div class="k-foot" style="margin:2px 0 0 0">taken</div></div>'
                    f'<div><div class="k-value">{free}</div><div class="k-foot" style="margin:2px 0 0 0">available</div></div></div>'
                    f'<div class="k-meter"><span style="width:{min(pct, 100):.0f}%"></span></div>')
            foot = f"{pct:.0f}% of {capacity} bays taken"
            if out_of_service:
                foot += f" &middot; {out_of_service} out of service"
            if not live:
                foot += " (as of the last recorded event)"
        else:
            body, foot = '<div class="k-value">-</div>', "Waiting for the bay list from the backend"
        st.markdown(card("Occupancy now", body, foot), unsafe_allow_html=True)
    with right:
        n = s["penalty_count"]
        lost = s["credits_lost"]
        st.markdown(card("Penalties", f'<div class="k-value">{"-" if lost else ""}{lost:,}<small style="font-size:14px;color:var(--k-muted);margin-left:6px">credits</small></div>',
                         f'{n:,} {"error" if n == 1 else "errors"} &times; {bm.PENALTY_CREDITS} credits each. '
                         'Taken from our credit score, not from income.'),
                    unsafe_allow_html=True)
    return s


def income_section(hist, start, end, freq, s):
    st.markdown('<div class="k-section">Earnings over time</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="k-sub">{describe_period(start, end)}. Each bar is the income of one {ui_label(freq)}. '
                'Times are the simulator\'s clock.</div>', unsafe_allow_html=True)
    earned = bm.sum_by_bucket(hist["income"], "time", "amount", start, end, freq)
    ui_charts.show(st, ui_charts.income_chart(earned, freq, T))
    best = bm.best_bucket(earned)
    bits = []
    if best:
        when = best[0].strftime("%d %b %H:%M" if freq in ("15min", "h") else "%d %b %Y")
        bits.append(f"Best {ui_label(freq)}: <b>{when}</b> with {credits(best[1])} credits")
    if s["avg_stay_minutes"]:
        bits.append(f"Average time parked: <b>{s['avg_stay_minutes']:.1f} min</b>")
    if bits:
        st.markdown('<div class="k-sub">' + " &nbsp;&middot;&nbsp; ".join(bits) + "</div>", unsafe_allow_html=True)


def penalty_section(hist, start, end, freq):
    st.markdown('<div class="k-section">Penalties</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="k-sub">{describe_period(start, end)}. Every error costs {bm.PENALTY_CREDITS} credits. '
                'This is a credit score, so it never reduces the income above.</div>', unsafe_allow_html=True)
    pen = hist["penalties"]
    inside = pen[(pen["time"] >= start) & (pen["time"] < end)] if not pen.empty else pen
    if inside.empty:
        st.markdown('<div class="k-card" style="height:auto">No errors in this period. Nothing was deducted.</div>', unsafe_allow_html=True)
        return
    counts = bm.count_by_bucket(inside, "time", start, end, freq)
    ui_charts.show(st, ui_charts.lost_chart(counts, freq, T, bm.PENALTY_CREDITS))
    table = pd.DataFrame({"Time": inside["time"], "Plate": inside["plate"], "What happened": inside["reason"],
                          "Credits lost": bm.PENALTY_CREDITS}).sort_values("Time", ascending=False)
    st.caption(f"{len(table):,} {'error' if len(table) == 1 else 'errors'} = {len(table) * bm.PENALTY_CREDITS:,} credits lost"
               + (" (newest 500 shown)" if len(table) > 500 else ""))
    shown = table.head(500).style.format({"Time": "{:%d %b %H:%M:%S}"}, na_rep="-")
    st.dataframe(shown, hide_index=True, height=min(60 + 35 * len(table), 300))
    st.download_button("Download penalties as CSV", table.to_csv(index=False).encode("utf-8"), "penalties.csv", "text/csv", key="pen_csv")


def ui_label(freq):
    return {"15min": "15 minutes", "h": "hour", "D": "day", "MS": "month"}[freq]


def forecast_section(hist):
    st.markdown('<div class="k-section">What to expect next</div>', unsafe_allow_html=True)
    span = hist["span"]
    events = hist["event_times"]
    latest = span[1]
    last_full_hour = latest.floor("h")             # the hour still running is not complete yet
    active = bm.active_hours(events)
    active = active[active < last_full_hour]
    a, b = st.columns([1, 1])
    metric = a.segmented_control("Estimate", ["Income", "Cars arriving"], default="Income", key="fc_metric") or "Income"
    have_days = len(set(active.normalize())) if len(active) else 0
    default_h = "24 hours" if len(active) >= fc.DAILY_PATTERN_HOURS and have_days >= 2 else "6 hours"
    horizon_label = b.segmented_control("Look ahead", ["6 hours", "12 hours", "24 hours"], default=default_h, key="fc_h") or default_h
    horizon = int(horizon_label.split()[0])

    if len(active) < fc.MIN_HOURS:
        st.info(f"Not enough history yet ({len(active)} hours of operation so far). The estimate appears after "
                f"{fc.MIN_HOURS} hours of data.")
        return
    first, stop = active.min(), last_full_hour
    if metric == "Income":
        series = bm.sum_by_bucket(hist["income"], "time", "amount", first, stop, "h")
        unit = "credits per hour"
    else:
        arrivals = hist["visits"].dropna(subset=["arrived"])
        series = bm.count_by_bucket(arrivals, "arrived", first, stop, "h")
        unit = "cars per hour"
    series = series[series.index.isin(active)]
    estimate, info = fc.forecast_hourly(series, horizon, fill_gaps=False)
    if estimate.empty:
        st.info(info["note"])
        return
    total, low, high = fc.total_range(estimate, info)
    word = "income" if metric == "Income" else "arrivals"
    what = f"{credits(total)} credits" if metric == "Income" else f"{total:,.0f} cars"
    rng = f"{credits(low)} to {credits(high)}" if metric == "Income" else f"{low:,.0f} to {high:,.0f}"
    st.markdown(f'<div class="k-sub">Estimated {word} for the next {horizon_label} (from {estimate["time"].iloc[0]:%d %b %H:%M}): '
                f'<b>{what}</b> (likely {rng}).</div>', unsafe_allow_html=True)
    ui_charts.show(st, ui_charts.forecast_chart(series.tail(24), estimate, unit, T))
    st.caption(f"Method: {info['method']}, from {info['history_hours']} hours when the car park was operating. "
               f"{info['note']} The shaded band is the likely range (about 80%). It is an estimate from past data, not a promise.")


def usage_section(hist, start, end, freq, capacity):
    st.markdown('<div class="k-section">Usage</div>', unsafe_allow_html=True)
    left, right = st.columns(2)
    with left:
        st.markdown('<div class="k-sub"><b>Occupancy</b>: busiest moment in each period, as a share of the bays (hover to see bays taken and available)</div>', unsafe_allow_html=True)
        peak = bm.occupancy_by_bucket(hist["curve"], capacity, start, end, freq, until=hist["span"][1])
        chart = ui_charts.occupancy_chart(peak, capacity, freq, T)
        if chart is None:
            st.info("No occupancy recorded in this period.")
        ui_charts.show(st, chart)
    with right:
        st.markdown('<div class="k-sub"><b>Paying cars</b> per period</div>', unsafe_allow_html=True)
        counts = bm.count_by_bucket(hist["income"], "time", start, end, freq)
        ui_charts.show(st, ui_charts.count_chart(counts, freq, T, "cars"))


@st.fragment(run_every=BUSINESS_REFRESH_S)
def business_view(start, end, freq):
    hist = load_history(DB_PATH)
    if hist["span"] is None:
        st.info("No cars have been recorded yet. Start the backend and the simulator, and this page fills in by itself.")
        return
    conn = db_read.open_db(DB_PATH)
    try:
        capacity, taken_now, out_of_service = capacity_and_now(conn)
        level = db_read.health(conn)[0]
    finally:
        conn.close()
    if level in ("offline", "empty", "demo") and not hist["curve"].empty:
        taken_now, out_of_service = int(hist["curve"].iloc[-1]), 0     # the live list is old; use the last recorded events
    if not capacity:
        capacity = int(hist["events"].loc[hist["events"]["SpotType"] == "Park", "SpotName"].nunique()) or None
    s = hero_row(hist, start, end, capacity, taken_now, live=level == "live", out_of_service=out_of_service)
    income_section(hist, start, end, freq, s)
    penalty_section(hist, start, end, freq)
    forecast_section(hist)
    usage_section(hist, start, end, freq, capacity)


# ---------------------------------------------------------------------------
# live car park
# ---------------------------------------------------------------------------

GATE_LOOK = {"open": ("good", "Open"), "closed": ("neutral", "Closed"), "opening": ("warning", "Opening"),
             "closing": ("warning", "Closing")}
RISK_LOOK = {"low": "good", "safe": "good", "none": "good", "ok": "good", "normal": "good",
             "mid": "warning", "medium": "warning", "moderate": "warning",
             "high": "critical", "critical": "critical", "danger": "critical"}


def gate_pill(row):
    if row["broken"]:
        return ui_theme.pill("critical", "Broken")
    if row["isUnderMaintenance"]:
        return ui_theme.pill("serious", "Under maintenance")
    kind, label = GATE_LOOK.get(str(row["state"]).lower(), ("neutral", str(row["state"])))
    return ui_theme.pill(kind, label)


def bays_html(bays, plates, from_events=False):
    tiles = []
    for row in bays.itertuples(index=False):
        taken = (row.detectedCars or 0) > 0 or (from_events and row.name in plates)
        cls = "broken" if row.broken else "maint" if row.isUnderMaintenance else "taken" if taken else ""
        plate = esc(plates.get(row.name, "")) if taken else ("broken" if row.broken else "repair" if row.isUnderMaintenance else "")
        tiles.append(f'<div class="k-bay {cls}"><span class="n">{esc(row.name)}</span><span class="p">{plate or "&nbsp;"}</span></div>')
    legend = ('<div class="k-legend">'
              '<span><span class="k-sw" style="background:var(--k-surface)"></span>Free</span>'
              '<span><span class="k-sw" style="background:var(--k-wash)"></span>Taken (plate shown)</span>'
              f'<span><span class="k-sw" style="background:{ui_theme.STATUS["serious"]}"></span>Under maintenance</span>'
              f'<span><span class="k-sw" style="background:{ui_theme.STATUS["critical"]}"></span>Broken</span></div>')
    return '<div class="k-bays">' + "".join(tiles) + "</div>" + legend


@st.fragment(run_every=LIVE_REFRESH_S)
def live_view():
    conn = db_read.open_db(DB_PATH)
    try:
        state = db_read.live_state(conn)
        plates = db_read.plates_in_spots(conn)
        feed = db_read.recent_activity(conn, 25)
        level = db_read.health(conn)[0]
    finally:
        conn.close()
    bays = db_read.car_park_spots(state)
    left, right = st.columns([2.1, 1])
    with left:
        st.markdown('<div class="k-sub"><b>Parking bays</b></div>', unsafe_allow_html=True)
        if bays.empty:
            st.info("Waiting for the first bay list from the backend.")
        else:
            st.markdown(bays_html(bays, plates, from_events=level in ("offline", "empty", "demo")), unsafe_allow_html=True)
    with right:
        st.markdown('<div class="k-sub"><b>Gates</b></div>', unsafe_allow_html=True)
        rows = "".join(f'<div class="k-row"><span>{esc(r.name)}</span>{gate_pill(r._asdict() if hasattr(r, "_asdict") else r)}</div>'
                       for r in state["barriers"].itertuples(index=False)) if not state["barriers"].empty else \
            '<div class="k-row"><span>No gate data yet</span></div>'
        st.markdown(f'<div class="k-card">{rows}</div>', unsafe_allow_html=True)

    st.markdown('<div class="k-gap"></div>', unsafe_allow_html=True)
    a, b, c = st.columns([1, 1, 1.4])
    with a:
        zones = state["zones"]
        rows = ""
        for z in zones.itertuples(index=False):
            kind = RISK_LOOK.get(str(z.risk).lower(), "neutral")
            rows += (f'<div class="k-row"><span>{esc(z.name)} &middot; CO {z.gasCarbonMonoxideLevel:g} ppm</span>'
                     f'{ui_theme.pill(kind, esc(z.risk))}</div>')
        st.markdown('<div class="k-sub"><b>Air quality</b></div><div class="k-card">' + (rows or '<div class="k-row">No zone data yet</div>') + "</div>",
                    unsafe_allow_html=True)
    with b:
        lights = state["lights"]
        on = int(lights["isOn"].fillna(0).astype(bool).sum()) if not lights.empty else 0
        alarms = len(state["alarms"])
        cars = state["status"]["cars"].iloc[0] if not state["status"].empty else None
        rows = (f'<div class="k-row"><span>Lights on</span><b>{on} of {len(lights)}</b></div>'
                f'<div class="k-row"><span>Active alarms</span><b>{alarms}</b></div>'
                f'<div class="k-row"><span>Cars in the simulation</span><b>{"-" if cars is None else int(cars)}</b></div>')
        st.markdown('<div class="k-sub"><b>Other</b></div><div class="k-card">' + rows + "</div>", unsafe_allow_html=True)
    with c:
        lines = "".join(f'<div><span class="t">{esc(t)}</span>{esc(text)}</div>' for t, _kind, text in feed) or "Nothing yet."
        st.markdown(f'<div class="k-sub"><b>Latest activity</b></div><div class="k-card"><div class="k-feed">{lines}</div></div>',
                    unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# gate control (operators only; everyone can see the state and the log)
# ---------------------------------------------------------------------------

MODE_LOOK = {"auto": ("neutral", "Automatic"), "forced_open": ("warning", "Held open"), "forced_closed": ("warning", "Held closed")}
ACTION_TEXT = {"auto": "Automatic", "forced_open": "Hold open", "forced_closed": "Hold closed", "repair": "Repair"}


def _do_mode(gate, mode):
    ok, text = control_client.set_mode(DB_PATH, gate, mode, st.session_state["user"]["username"])
    st.session_state["gate_msg"] = {"at": time.time(), "ok": ok, "text": text}


def _do_repair(gate, confirm):
    ok, text = control_client.request_repair(DB_PATH, gate, st.session_state["user"]["username"], confirm)
    st.session_state["gate_msg"] = {"at": time.time(), "ok": ok, "text": text}


def _ask_repair(gate):
    st.session_state["confirm_repair"] = gate


def _cancel_repair():
    st.session_state.pop("confirm_repair", None)


def _confirm_repair(gate):
    st.session_state.pop("confirm_repair", None)
    _do_repair(gate, True)


def _gate_card(row, order, can_press):
    """One gate on one row: its state and who holds it on the left, the buttons (operators) on the right."""
    name = row["name"]
    mode = order.get("mode", "auto")
    broken, maint = bool(row["broken"]), bool(row["isUnderMaintenance"])
    blocked = broken or maint
    kind, label = MODE_LOOK.get(mode, MODE_LOOK["auto"])
    since = f' &middot; by {esc(order["set_by"])} at {esc(str(order["set_at"])[-8:])}' if mode != "auto" and order.get("set_by") else ""
    note = ""
    if blocked:
        why = "broken" if broken else "under maintenance"
        note = f'<br>{esc(name)} is {why}, so nobody may operate it.' + (" The saved order is applied again once it works." if mode != "auto" else "")
    with st.container(border=True):
        info, a, b, c, d = st.columns([2.6, 1, 1, 1, 1.25]) if can_press else (st.container(), None, None, None, None)
        with info:
            st.markdown(f'<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap"><b>{esc(name)}</b>'
                        f'{gate_pill(row)}{ui_theme.pill(kind, label)}</div>'
                        f'<div class="k-foot" style="margin:6px 0 0 0">Order: {label.lower()}{since}{note}</div>', unsafe_allow_html=True)
        if not can_press:
            return
        a.button("Hold open", key=f"open_{name}", use_container_width=True, disabled=blocked or mode == "forced_open",
                 type="primary" if mode == "forced_open" else "secondary", on_click=_do_mode, args=(name, "forced_open"))
        b.button("Hold closed", key=f"close_{name}", use_container_width=True, disabled=blocked or mode == "forced_closed",
                 type="primary" if mode == "forced_closed" else "secondary", on_click=_do_mode, args=(name, "forced_closed"))
        c.button("Automatic", key=f"auto_{name}", use_container_width=True, disabled=mode == "auto",
                 type="primary" if mode == "auto" else "secondary", on_click=_do_mode, args=(name, "auto"))
        if broken:
            d.button("Request repair", key=f"repair_{name}", use_container_width=True, on_click=_do_repair, args=(name, False))
        elif maint:
            d.button("Being repaired", key=f"repair_{name}", use_container_width=True, disabled=True)
        else:
            d.button("Send to maintenance", key=f"repair_{name}", use_container_width=True, on_click=_ask_repair, args=(name,))
        if st.session_state.get("confirm_repair") == name and not blocked:
            x, y, z = st.columns([3.6, 1, 1])
            x.markdown(f'<div class="k-foot" style="margin:6px 0 0 0"><b>{esc(name)} is working.</b> Sending it to maintenance takes it out of use '
                       'until the repair is done, and cars may wait at it meanwhile. Send it?</div>', unsafe_allow_html=True)
            y.button("Yes, send it", key=f"repair_yes_{name}", type="primary", use_container_width=True, on_click=_confirm_repair, args=(name,))
            z.button("Cancel", key=f"repair_no_{name}", use_container_width=True, on_click=_cancel_repair)


@st.fragment(run_every=3)
def gate_control_view(role):
    conn = db_read.open_db(DB_PATH)
    try:
        barriers = db_read.live_state(conn)["barriers"]
        orders = db_read.gate_modes(conn)
        log = db_read.control_log(conn, 12)
        level = db_read.health(conn)[0]
    finally:
        conn.close()

    can = login.can_control(role)
    if not can:
        st.markdown('<div class="k-banner">You are signed in as admin (view only). Ask an operator to control the gates.</div>', unsafe_allow_html=True)
    elif level in ("offline", "empty", "demo"):
        why = ("These are demo data, so the controls are switched off." if level == "demo"
               else "The backend is not running, so the controls are switched off.")
        st.markdown(f'<div class="k-banner">{why}</div>', unsafe_allow_html=True)
    can_press = can and level in ("live", "delayed")

    if barriers.empty:
        st.info("Waiting for the gate list from the backend.")
    else:
        for row in barriers.to_dict("records"):
            _gate_card(row, orders.get(row["name"], {}), can_press)

    msg = st.session_state.get("gate_msg")
    if msg and time.time() - msg["at"] < 12:
        (st.success if msg["ok"] else st.error)(msg["text"])

    st.markdown('<div class="k-sub" style="margin-top:14px"><b>Recent commands</b></div>', unsafe_allow_html=True)
    if log.empty:
        st.markdown('<div class="k-card" style="height:auto">No manual commands yet.</div>', unsafe_allow_html=True)
    else:
        table = pd.DataFrame({"Time": log["at"].astype(str).str[-8:], "Who": log["username"], "Gate": log["gate"],
                              "Command": log["mode"].map(ACTION_TEXT).fillna(log["mode"]),
                              "Result": log["result"].map({"done": "Done", "refused": "Refused"}).fillna(log["result"]),
                              "Details": log["detail"].fillna("")})
        st.dataframe(table, hide_index=True, height=min(60 + 35 * len(table), 380))


# ---------------------------------------------------------------------------
# visit history
# ---------------------------------------------------------------------------

def history_section(start, end):
    st.markdown('<div class="k-section">Visit history</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="k-sub">Every car that arrived or left during {describe_period(start, end)}. Search by plate.</div>',
                unsafe_allow_html=True)
    hist = load_history(DB_PATH)
    visits = bm.attach_charges(hist["visits"], hist["charges"])
    if visits.empty:
        st.info("No visits recorded yet.")
        return
    seen = visits[((visits["arrived"] >= start) & (visits["arrived"] < end)) | ((visits["left"] >= start) & (visits["left"] < end))]
    a, b = st.columns([1, 2])
    query = a.text_input("Search plate", key="plate_search", placeholder="e.g. TLT 388").strip().upper()
    states = sorted(seen["state"].unique())
    chosen = b.multiselect("Show", states, default=states, key="visit_states")
    seen = seen[seen["state"].isin(chosen)]
    if query:
        seen = seen[seen["plate"].str.upper().str.contains(query, regex=False, na=False)]
    seen = seen.sort_values("arrived", ascending=False)
    table = pd.DataFrame({
        "Plate": seen["plate"], "Arrived": seen["arrived"], "Bay": seen["spot"], "Parked (min)": seen["minutes_parked"].round(1),
        "Left": seen["left"], "Billed": seen["billed"], "Payment": seen["payment"], "Status": seen["state"],
    })
    st.caption(f"{len(table):,} visits" + (" (showing the newest 1,000)" if len(table) > 1000 else ""))
    shown = table.head(1000).style.format({"Arrived": "{:%d %b %H:%M:%S}", "Left": "{:%d %b %H:%M:%S}",
                                           "Parked (min)": "{:.1f}", "Billed": "{:.2f}"}, na_rep="-")
    st.dataframe(shown, hide_index=True, height=380)
    st.download_button("Download as CSV", table.to_csv(index=False).encode("utf-8"), "visits.csv", "text/csv", key="csv")


# ---------------------------------------------------------------------------
# page
# ---------------------------------------------------------------------------

def main():
    try:
        db_read.open_db(DB_PATH).close()
    except db_read.DatabaseMissing:
        st.markdown('<div class="k-eyebrow">Grand Park Auto</div><div class="k-title">Car park business dashboard</div>', unsafe_allow_html=True)
        st.warning(f"The database does not exist yet: `{DB_PATH}`. Start the backend "
                   "(`python backend/run_all.py --live`) and reload this page.")
        st.stop()
    user = require_login()
    header(user)
    hist = load_history(DB_PATH)
    latest = hist["span"][1] if hist["span"] else pd.Timestamp.now().floor("min")
    start, end, freq_name = period_controls(latest)
    freq = bm.GROUP_BY[freq_name]
    business_view(start, end, freq)
    st.markdown('<div class="k-section">Right now</div><div class="k-sub">The live car park. Refreshes every '
                f'{LIVE_REFRESH_S} seconds straight from the database.</div>', unsafe_allow_html=True)
    live_view()
    st.markdown('<div class="k-section">Gate control</div><div class="k-sub">Hold a gate open or closed, or give it back to '
                'the automatic car logic. A held gate is a final order: the car logic cannot change it. A broken gate or one '
                'under maintenance is never operated.</div>', unsafe_allow_html=True)
    gate_control_view(user["role"])
    history_section(start, end)


main()
