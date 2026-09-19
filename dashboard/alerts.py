"""Turn a snapshot into alerts for the challenge scenarios in the brief.

If the backend already sends its own "alerts" list we show that. Otherwise these simple rules run on
the data the dashboard already has, so the alerts panel works with any data source.
"""

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _fan_note(snapshot, zone, running):
    """What to say about extraction for this zone."""
    if running:
        turbo = any(f.get("speed") == "turbo" for f in snapshot.get("fans", [])
                    if f["name"] in running)
        return f"Exhaust fan {', '.join(running)} is running" + (" at turbo." if turbo else ".")
    if not snapshot.get("fans"):
        return "This car park has no exhaust fans: ventilate it another way."
    in_zone = [f for f in snapshot.get("fans", []) if f.get("zone") == zone.get("name")]
    if in_zone and all(f.get("health") != "ok" for f in in_zone):
        return "The exhaust fan here is out of service!"
    return "No exhaust fan running here!"


def derive_alerts(s):
    alerts = []
    spots = s.get("spots", [])
    total = len(spots)
    free = sum(1 for p in spots if p.get("state") == "available")

    # CO buildup ---------------------------------------------------------------
    for z in s.get("zones", []):
        risk = z.get("risk", "Safe")
        if risk in ("Mid", "High", "Critical"):
            fans = [f["name"] for f in s.get("fans", []) if f.get("zone") == z.get("name") and f.get("on")]
            alerts.append({
                "severity": "warning" if risk == "Mid" else "critical", "kind": "CO buildup",
                "title": f"Carbon monoxide {risk.lower()} in {z['name']}",
                "detail": f"{z.get('co_ppm', 0):.0f} ppm (Mid starts at 50). "
                          + _fan_note(s, z, fans)})

    # Component breakdown -------------------------------------------------------
    for g in s.get("gates", []):
        if g.get("health") == "broken":
            alerts.append({"severity": "critical", "kind": "Breakdown", "title": f"{g['name']} ({g.get('role') or 'gate'}) is broken",
                           "detail": "Cars cannot pass and operating it costs a penalty. Repair needed."})
        elif g.get("health") == "maintenance":
            alerts.append({"severity": "warning", "kind": "Repair", "title": f"{g['name']} is under repair",
                           "detail": "Do not operate it until the repair finishes."})
    for f in s.get("fans", []):
        if f.get("health") == "broken":
            alerts.append({"severity": "critical", "kind": "Breakdown", "title": f"Exhaust fan {f['name']} is broken",
                           "detail": f"{f.get('zone')} has no ventilation until it is repaired."})
    broken_spots = [p["name"] for p in spots if p.get("state") == "broken"]
    if broken_spots:
        alerts.append({"severity": "warning", "kind": "Breakdown", "title": f"{len(broken_spots)} parking spot(s) out of service",
                       "detail": ", ".join(broken_spots[:8])})

    # Rogue cars ----------------------------------------------------------------
    for c in s.get("cars", []):
        if c.get("flag") == "rogue":
            alerts.append({"severity": "critical", "kind": "Rogue car",
                           "title": f"{c['plate']} parked in {c.get('spot')}, not {c.get('assigned_spot') or 'where it was sent'}",
                           "detail": "The car ignored its instructions. Check it and keep the spot marked."})

    # Traffic surge / congestion ------------------------------------------------
    buckets = [b.get("count", 0) for b in s.get("history", {}).get("arrivals", [])]
    while buckets and buckets[0] == 0:          # ignore the empty stretch before the car park had any data
        buckets.pop(0)
    if len(buckets) >= 4:
        latest, earlier = buckets[-1], buckets[:-1]
        avg = sum(earlier) / max(1, len(earlier))
        if latest >= max(14, 2.2 * avg):
            alerts.append({"severity": "warning", "kind": "Traffic surge", "title": "Traffic surge at the entrance",
                           "detail": f"{latest} arrivals in the last 5 minutes (normal is about {avg:.0f})."})
    refused = s.get("stats", {}).get("refused_last_10min", 0)
    if refused >= 5:
        alerts.append({"severity": "warning", "kind": "Congestion", "title": "Cars are being turned away",
                       "detail": f"{refused} cars could not get a spot in the last 10 minutes."})
    if total and free == 0:
        alerts.append({"severity": "critical", "kind": "Full", "title": "Car park is full",
                       "detail": "New cars are being sent away." + (f" {refused} refused in 10 min." if refused else "")})
    elif total and free / total <= 0.15:
        alerts.append({"severity": "warning", "kind": "Congestion", "title": f"Nearly full: only {free} spots left",
                       "detail": "Expect a queue at the entrance."})

    alerts.sort(key=lambda a: SEVERITY_ORDER.get(a["severity"], 9))
    return alerts
