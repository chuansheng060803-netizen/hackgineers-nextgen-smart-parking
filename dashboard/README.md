# Dashboard (Streamlit)

Live view of the smart car park: free/occupied spots, the S1-S30 map, cars inside, recent activity,
carbon monoxide per zone, gate and fan status, and alerts for the challenge scenarios.

The dashboard only **reads** data. It never talks to the simulator and never changes the backend.

## Run it

From the repo root:

```
pip install -r dashboard/requirements.txt
streamlit run dashboard/app.py
```

It opens at http://localhost:8501 with built-in **mock data**, so it works without the simulator or backend.
In the sidebar, the "Demo the challenges" buttons force a traffic surge, a CO buildup, a gate breakdown or a rogue car,
so you can see how each alert looks.

## Connect it to the real backend later

Set two environment variables before starting (PowerShell):

```
$env:DASHBOARD_SOURCE = "api"
$env:DASHBOARD_API_URL = "http://localhost:8000"
streamlit run dashboard/app.py
```

The dashboard then calls `GET {DASHBOARD_API_URL}/api/snapshot` every few seconds. If the backend stops answering,
the page keeps showing the last good data with a warning banner instead of crashing.

## What the backend needs to return (`/api/snapshot`)

One JSON object. Every key is optional; missing keys show as empty panels.

| key | shape |
|---|---|
| `generated_at` | `"2026-01-01 12:00:00"` (simulated time) |
| `spots` | `[{name:"S1", zone:"ZONE1", type:"Any\|Electric\|Accessible", state:"available\|reserved\|occupied\|broken\|maintenance", car:"ABC 123"\|null}]` |
| `cars` | `[{plate, car_type, status:"Heading to spot\|Parked\|Heading to exit", spot, entered_at, minutes_inside, estimated_charge, flag:"rogue"\|null, assigned_spot}]` |
| `events` | newest first: `[{t:"12:00:05", kind:"entry\|park\|exit\|payment\|penalty\|rogue\|refused\|component\|co\|gate", text}]` |
| `sessions` | finished visits, newest first, for the History search: `[{plate, car_type, spot, entered_at:"2026-01-01 12:00:00", left_at, minutes, charge, status:"Completed"}]` |
| `gates` | `[{name, role, zone, state:"Open\|Closed\|Opening\|Closing", health:"ok\|broken\|maintenance"}]` |
| `fans` | `[{name, zone, on:true, health:"ok\|broken\|maintenance"}]` |
| `zones` | `[{name:"ZONE1", co_ppm:12.3, risk:"Safe\|Mid\|High\|Critical"}]` (50 ppm and up counts as Mid) |
| `history.occupancy` | `[{t:ISO time, occupied:int, total:int}]` |
| `history.co` | `[{t:ISO time, zone, ppm}]` |
| `history.arrivals` | `[{t:ISO time, count}]` in 5-minute buckets |
| `stats` | `{revenue, cars_served, penalty_count, penalty_total, refused_last_10min}` |
| `penalties` | `[{t, reason, amount, component}]` |
| `alerts` | optional. If present it is shown as-is: `[{severity:"critical\|warning\|info", kind, title, detail}]`. If absent the dashboard derives alerts itself. |

`mock_data.py` produces exactly this shape and is the reference example.

## Files

- `app.py`: page layout and auto-refresh
- `ui.py`: the HTML pieces (KPIs, parking map, gates, CO meters, feed)
- `charts.py`: Plotly charts (occupancy, CO, arrivals)
- `alerts.py`: rules that turn a snapshot into alerts
- `styles.py`: colours (light and dark) and CSS
- `data_source.py`: mock or API switch
- `mock_data.py`: the fake car park
