"""HTML pieces for the dashboard. Each function returns ONE line of HTML (no blank lines, no indentation)
because Streamlit's markdown treats blank lines and 4-space indents specially."""
from html import escape as e

from styles import RISK_COLOR

ICON = {
    "alert": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3 2 21h20L12 3z"/><path d="M12 10v5"/><path d="M12 18h.01"/></svg>',
    "x": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/></svg>',
    "info": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/></svg>',
    "wrench": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a4 4 0 0 0-5.4 5.1L3 17.7 6.3 21l6.3-6.3a4 4 0 0 0 5.1-5.4l-2.6 2.6-2.4-.6-.6-2.4z"/></svg>',
    "check": '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12l5 5 9-10"/></svg>',
}
SEV_ICON = {"critical": "x", "warning": "alert", "info": "info"}
SEV_TAG = {"critical": "Critical", "warning": "Warning", "info": "Info"}


def header(title, subtitle, source, updated, ok=True, note=""):
    dot = "good" if ok else "crit"
    return (f'<div class="pk pk-head"><div><div class="pk-title">{e(title)}</div><div class="pk-sub">{e(subtitle)}</div></div>'
            f'<div style="display:flex;gap:8px;flex-wrap:wrap"><span class="pk-chip"><span class="pk-dot {dot}"></span>{e(source)}{(" · " + e(note)) if note else ""}</span>'
            f'<span class="pk-chip">Updated {e(updated)}</span></div></div>')


def alerts_block(alerts):
    if not alerts:
        return ('<div class="pk"><div class="pk-alerts"><div class="pk-alert info">' + ICON["check"] +
                '<div><div class="t">All clear</div><div class="d">No active alerts.</div></div></div></div></div>')
    items = "".join(
        f'<div class="pk-alert {e(a["severity"])}">{ICON[SEV_ICON.get(a["severity"], "info")]}'
        f'<div><div class="t">{e(a["title"])}</div><div class="d">{e(a.get("detail", ""))}</div></div>'
        f'<div class="tag">{e(SEV_TAG.get(a["severity"], ""))} · {e(a.get("kind", ""))}</div></div>' for a in alerts)
    return f'<div class="pk"><div class="pk-alerts">{items}</div></div>'


def kpis(spots, cars, stats):
    """The five headline numbers.

    These must agree with what the simulator itself reports. A spot that is
    *reserved* for a car that has not arrived is still physically empty, and a
    car that has been allocated a spot but is queued outside the barrier is not
    yet inside the car park. Counting either one as "taken" or "inside" makes the
    dashboard disagree with the car park in front of you, so both are shown
    separately instead of being folded into the headline.
    """
    total = len(spots)
    counts = {}
    for s in spots:
        counts[s.get("state")] = counts.get(s.get("state"), 0) + 1
    occupied = counts.get("occupied", 0)
    reserved = counts.get("reserved", 0)
    free = counts.get("available", 0)          # empty AND not held for anyone
    empty = free + reserved                    # what the simulator calls free
    pct_free = round(100 * free / total) if total else 0
    pct_used = round(100 * occupied / total) if total else 0

    tone, word = ("good", "plenty of space")
    if total and free == 0:
        tone, word = "crit", "none left to give out"
    elif total and free / total <= 0.15:
        tone, word = "warn", "nearly full"

    heading_in = sum(1 for c in cars if c.get("status") == "Heading to spot")
    leaving = sum(1 for c in cars if c.get("status") == "Heading to exit")
    inside = occupied + leaving                # cars actually in the car park
    held = f" · {reserved} held for cars on the way" if reserved else ""
    pen_n, pen_t = stats.get("penalty_count", 0), stats.get("penalty_total", 0)
    return (
        '<div class="pk pk-kpis">'
        f'<div class="pk-kpi hero"><div class="bar {tone}"></div><div class="lbl">Available spots</div>'
        f'<div class="val">{free}<small> / {total}</small></div>'
        f'<div class="sub"><span class="pk-dot {tone}"></span>{pct_free}% free · {word}</div></div>'
        f'<div class="pk-kpi"><div class="lbl">Occupancy</div><div class="val">{pct_used}<small>%</small></div>'
        f'<div class="sub">{occupied} parked · {empty} empty{held}</div></div>'
        f'<div class="pk-kpi"><div class="lbl">Cars inside</div><div class="val">{inside}</div>'
        f'<div class="sub">{heading_in} heading in · {leaving} leaving</div></div>'
        f'<div class="pk-kpi"><div class="lbl">Income</div><div class="val">{stats.get("revenue", 0):,.2f}</div>'
        f'<div class="sub">{stats.get("cars_served", 0)} cars served</div></div>'
        f'<div class="pk-kpi"><div class="bar {"warn" if pen_n else ""}"></div><div class="lbl">Penalties</div><div class="val">{pen_n}</div>'
        f'<div class="sub">−{pen_t:,.0f} credits</div></div>'
        '</div>')


SPOT_LABEL = {"available": "Free", "reserved": "Reserved", "broken": "Broken", "maintenance": "Under repair"}
BOLT = '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M13 2 4 14h6l-1 8 9-12h-6z"/></svg>'
WHEEL = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">'
         '<circle cx="12" cy="4" r="1.6"/><path d="M12 7v6h5l2 5"/><path d="M8 11a5 5 0 1 0 6 7"/></svg>')
TYPE_TAG = {"Electric": ("EV", BOLT), "Accessible": ("ACC", WHEEL)}
# a car seen from above, nose up. Solid for a parked car, outline only for a car still on its way.
CAR = ('<svg viewBox="0 0 40 64"><rect x="5" y="3" width="30" height="58" rx="11" fill="currentColor"/>'
       '<rect x="10" y="15" width="20" height="13" rx="4" fill="var(--pk-surface)" opacity=".6"/>'
       '<rect x="10" y="38" width="20" height="10" rx="3" fill="var(--pk-surface)" opacity=".45"/>'
       '<rect x="9" y="6" width="7" height="3.5" rx="1.5" fill="#fff" opacity=".85"/><rect x="24" y="6" width="7" height="3.5" rx="1.5" fill="#fff" opacity=".85"/></svg>')
CAR_GHOST = ('<svg viewBox="0 0 40 64" fill="none" stroke="currentColor" stroke-width="2.6" stroke-dasharray="4 3"><rect x="5" y="3" width="30" height="58" rx="11"/>'
             '<rect x="10" y="15" width="20" height="13" rx="4"/></svg>')


def parking_map(spots):
    zones = {}
    for s in spots:
        zones.setdefault(s.get("zone") or "-", []).append(s)
    legend = ('<div class="pk-legend">'
              '<span><i style="background:var(--pk-surface2);border-style:dashed"></i>Free</span>'
              '<span><i style="background:var(--pk-s1);border-color:var(--pk-s1)"></i>Occupied</span>'
              '<span><i style="background:transparent;border:2px dashed var(--pk-s1)"></i>Car on the way</span>'
              '<span><i style="background:var(--pk-crit);border-color:var(--pk-crit)"></i>Broken</span>'
              '<span><i style="background:var(--pk-warn);border-color:var(--pk-warn)"></i>Under repair</span></div>')
    out = [f'<div class="pk"><div class="pk-card"><div class="pk-card-h"><span class="pk-card-title">Parking map</span>{legend}</div>']
    for name, items in zones.items():
        n = len(items)
        free = sum(1 for s in items if s["state"] == "available")
        busy = sum(1 for s in items if s["state"] in ("occupied", "reserved"))
        tiles = []
        for s in items:
            st = s["state"]
            car = e(s["car"]) if s.get("car") else ""
            if st == "occupied":
                art, label = f'<div class="art">{CAR}</div>', car or "Occupied"
            elif st == "reserved":
                art, label = f'<div class="art">{CAR_GHOST}</div>', ("→ " + car) if car else "Reserved"
            elif st == "broken":
                art, label = f'<div class="art st">{ICON["x"]}</div>', "Broken"
            elif st == "maintenance":
                art, label = f'<div class="art st">{ICON["wrench"]}</div>', "Under repair"
            else:
                art, label = '<div class="art p">P</div>', "Free"
            tag = TYPE_TAG.get(s.get("type"))
            title = f'{s["name"]} · {s.get("type", "Any")} · {st}' + (f' · {s["car"]}' if s.get("car") else "")
            cls = f'pk-spot {st}' + (" rogue" if s.get("flag") == "rogue" else "")
            tiles.append(f'<div class="{cls}" title="{e(title)}"><div class="top"><span class="n">{e(s["name"])}</span>'
                         + (f'<span class="k">{tag[1]}{tag[0]}</span>' if tag else "")
                         + f'</div>{art}<span class="l">{label}</span></div>')
        out.append(f'<div class="pk-zone"><div class="pk-zone-h"><b>{e(name)}</b><span class="pk-meta">{free} free of {n}</span>'
                   f'<div class="pk-track"><span style="width:{(100 * busy / n) if n else 0:.0f}%"></span></div></div>'
                   f'<div class="pk-spots">{"".join(tiles)}</div></div>')
    out.append("</div></div>")
    return "".join(out)


def _pill(text, tone="", icon=""):
    return f'<span class="pk-pill {tone}">{icon}{e(text)}</span>'


def gates_block(gates, fans):
    rows = []
    for g in gates:
        health = g.get("health", "ok")
        state = g.get("state") or "Unknown"
        tone = {"Open": "good", "Opening": "", "Closing": "", "Closed": ""}.get(state, "")
        pills = _pill(state, tone)
        if health == "broken":
            pills += _pill("Broken", "crit", ICON["x"])
        elif health == "maintenance":
            pills += _pill("In repair", "warn", ICON["wrench"])
        if g.get("manual"):
            pills += _pill("Manual", "warn")
        role = g.get("role") or (g.get("zone") or "gate")
        rows.append(f'<div class="pk-row"><div><span class="nm">{e(g["name"])}</span> <span class="role">{e(role)}'
                    f'{(" · " + e(g["zone"])) if g.get("zone") else ""}</span></div><div class="right">{pills}</div></div>')
    for f in fans:
        health = f.get("health", "ok")
        speed = f.get("speed") or ("normal" if f.get("on") else "off")
        pills = _pill({"turbo": "Turbo", "normal": "Running", "off": "Off"}.get(speed, "Running"),
                      {"turbo": "warn", "normal": "good", "off": ""}.get(speed, "good"))
        if f.get("manual"):
            pills += _pill("Manual", "warn")
        if health == "broken":
            pills += _pill("Broken", "crit", ICON["x"])
        elif health == "maintenance":
            pills += _pill("In repair", "warn", ICON["wrench"])
        rows.append(f'<div class="pk-row"><div><span class="nm">{e(f["name"])}</span> <span class="role">exhaust fan · {e(f.get("zone", ""))}</span></div>'
                    f'<div class="right">{pills}</div></div>')
    body = "".join(rows) or '<div class="pk-empty">No components reported yet.</div>'
    return f'<div class="pk"><div class="pk-card"><div class="pk-card-h"><span class="pk-card-title">{"Gates and fans" if fans else "Gates"}</span></div>{body}</div></div>'


def system_block(source, ok, updated, spots, gates, fans, alerts):
    parts = list(gates) + list(fans)
    bad = [c for c in parts if c.get("health", "ok") != "ok"]
    crit = sum(1 for a in alerts if a.get("severity") == "critical")
    conn = _pill("Online", "good") if ok else _pill("Not answering", "crit", ICON["x"])
    comp = _pill(f"{len(parts) - len(bad)} of {len(parts)} working", "good" if not bad else "crit" if any(c.get("health") == "broken" for c in bad) else "warn")
    broken_spots = sum(1 for p in spots if p.get("state") in ("broken", "maintenance"))
    spot_pill = _pill("All working" if not broken_spots else f"{broken_spots} out of service", "good" if not broken_spots else "warn")
    al = _pill("None" if not alerts else f"{len(alerts)} active" + (f" ({crit} critical)" if crit else ""), "good" if not alerts else "crit" if crit else "warn")
    rows = [("Data source", _pill(source)), ("Connection", conn), ("Last update", _pill(updated or "-")),
            ("Gates and fans" if fans else "Gates", comp), ("Parking spots", spot_pill), ("Alerts", al)]
    body = "".join(f'<div class="pk-row"><span class="nm">{e(k)}</span><div class="right">{v}</div></div>' for k, v in rows)
    return f'<div class="pk"><div class="pk-card"><div class="pk-card-h"><span class="pk-card-title">System status</span></div>{body}</div></div>'


def co_block(zones, scale=150.0):
    rows = []
    for z in zones:
        ppm, risk = float(z.get("co_ppm", 0)), z.get("risk", "Safe")
        w = min(100.0, 100.0 * ppm / scale)
        tone = {"Safe": "good", "Mid": "warn", "High": "serious", "Critical": "crit"}.get(risk, "")
        rows.append(f'<div class="pk-co"><div class="pk-co-top"><b>{e(z["name"])}</b>{_pill(risk, tone)}'
                    f'<span class="v">{ppm:.0f} ppm</span></div><div class="pk-meter"><span style="width:{w:.1f}%;background:{RISK_COLOR.get(risk, "#888")}"></span>'
                    f'<i style="left:{100 * 50 / scale:.1f}%"></i></div></div>')
    scale_row = f'<div class="pk-scale"><span>0</span><span>50 = Mid</span><span>{scale:.0f} ppm</span></div>'
    body = "".join(rows) + scale_row if rows else '<div class="pk-empty">No CO readings yet.</div>'
    return f'<div class="pk"><div class="pk-card"><div class="pk-card-h"><span class="pk-card-title">Carbon monoxide</span><span class="pk-meta">per zone</span></div>{body}</div></div>'


KIND_LABEL = {"entry": ("ENTRY", "blue"), "park": ("PARK", "blue"), "exit": ("EXIT", ""), "payment": ("PAID", "good"),
              "penalty": ("PENALTY", "crit"), "rogue": ("ROGUE", "crit"), "refused": ("REFUSED", "warn"),
              "component": ("PART", "warn"), "co": ("CO", "warn"), "gate": ("GATE", "")}


def activity_feed(events, limit=14):
    if not events:
        return '<div class="pk"><div class="pk-empty">No activity yet.</div></div>'
    items = []
    for ev in events[:limit]:
        label, tone = KIND_LABEL.get(ev.get("kind"), (str(ev.get("kind", "")).upper(), ""))
        items.append(f'<div class="pk-feed-item"><span class="tm">{e(ev.get("t", ""))}</span>'
                     f'<span class="tg"><span class="pk-dot {tone}"></span>{e(label)}</span><span>{e(ev.get("text", ""))}</span></div>')
    return f'<div class="pk"><div class="pk-feed">{"".join(items)}</div></div>'


def card_title(title, meta=""):
    return (f'<div class="pk"><div class="pk-card-h" style="margin-bottom:6px"><span class="pk-card-title">{e(title)}</span>'
            f'<span class="pk-meta">{e(meta)}</span></div></div>')
