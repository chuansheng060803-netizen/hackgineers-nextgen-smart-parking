# Dashboard (Streamlit)

Live view of the smart car park: free/occupied spots, the S1-S30 map (6 zones with 5 slots each), cars inside, recent activity,
carbon monoxide per zone, gate and fan status, alerts for the challenge scenarios, and an Admin/Operator panel with manual
gate and fan buttons.

The dashboard never talks to the simulator. It reads data from the backend (`GET`), and the manual buttons send a control
request to the backend (`POST`, see "Manual controls"). Until the backend supports that, the buttons only work on the built-in
mock data.

## Run it

From the repo root:

```
pip install -r dashboard/requirements.txt
streamlit run dashboard/app.py
```

It opens at http://localhost:8501 with built-in **mock data**, so it works without the simulator or backend.
To run it against the real car park instead, see "Run it against the real simulator" below.
In the sidebar, the "Demo the challenges" buttons force a traffic surge, a CO buildup, a gate breakdown or a rogue car,
so you can see how each alert looks.

## Run it against the real simulator (integrated)

`backend/dashboard_api.py` serves these endpoints from the live system, on the
same port as the webhook listener, so there is nothing extra to start.

Three terminals:

```
1) the organisers' simulator            (listens on :9898)

2) cd backend
   python run_flow.py --live --db       (webhook + dashboard API on :5000)

3) $env:DASHBOARD_SOURCE = "api"
   $env:DASHBOARD_API_URL = "http://localhost:5000"
   streamlit run dashboard/app.py
```

Leave off `--live` for a dry run: the dashboard still shows everything, but the
manual gate buttons refuse instead of sending commands. Leave off `--db` and
today's data still works; only the 30-day history needs the database.

Optional: `DASHBOARD_API_TOKEN` (sent as `Authorization: Bearer <token>` on control requests).

The dashboard then calls `GET {DASHBOARD_API_URL}/api/snapshot` every few seconds. If the backend stops answering,
the page keeps showing the last good data with a warning banner instead of crashing.

## Show the real data from the SQLite database (`db` mode)

The backend (`python backend/run_flow.py --db`) writes parking sessions, payments, spots and events to SQLite. The dashboard
only reads that file; it never talks to the simulator.

```
$env:PARKING_DB_PATH = "database/runtime.db"      # optional; default is database/parking.db
python backend/create_user.py --username alice --role Admin      # once, asks for a password
$env:DASHBOARD_SOURCE = "db"
streamlit run dashboard/app.py
```

- Sign in with a database user (Admin or Operator). Nothing is shown before sign-in.
- Shown from the database: parking spots (free / on the way / occupied / broken / under repair, current car, zone), cars inside,
  visit history with payment status (pending or paid), the activity feed and gates (only if the gates table has rows).
- Not stored in the database, so those panels stay empty: exhaust fans, carbon monoxide, occupancy history, penalty amounts,
  the 30-day archive. Gate status shows as unavailable until real gate synchronisation exists.
- Manual gate/fan controls are read-only in this mode. Income counts only payments that are marked paid.
- `database/*.db` is git-ignored runtime data (it also holds password hashes): do not commit it. Use a fresh database for real
  runs: `database/test_database.py` and `test_authentication.py` write test data (a fake `gate0`, users `admin_test`, `operator_test`)
  into the default `database/parking.db`.

## What the backend needs to return (`/api/snapshot`)

One JSON object. Every key is optional; missing keys show as empty panels.

| key | shape |
|---|---|
| `generated_at` | `"2026-01-01 12:00:00"` (simulated time) |
| `spots` | `[{name:"S1", zone:"ZONE1", type:"Any\|Electric\|Accessible", state:"available\|reserved\|occupied\|broken\|maintenance", car:"ABC 123"\|null}]` |
| `cars` | `[{plate, car_type, status:"Heading to spot\|Parked\|Heading to exit", spot, entered_at, minutes_inside, estimated_charge, flag:"rogue"\|null, assigned_spot}]` |
| `events` | newest first: `[{t:"12:00:05", kind:"entry\|park\|exit\|payment\|penalty\|rogue\|refused\|component\|co\|gate", text}]` |
| `sessions` | finished visits, newest first, for the History search: `[{plate, car_type, spot, entered_at:"2026-01-01 12:00:00", left_at, minutes, charge, status:"Completed"}]` |
| `archive` | one summary row per day, newest first (today first), up to 30 days, for the 30-day history: `[{date:"2026-01-01", visits, cars_parked, drive_through, income, penalties, peak_pct, avg_minutes}]` |
| `gates` | `[{name, role, zone, state:"Open\|Closed\|Opening\|Closing", health:"ok\|broken\|maintenance", manual:"open"\|"closed"\|null}]` (`manual` is optional: `null`/missing = automatic) |
| `fans` | `[{name, zone, on:true, speed:"normal\|turbo\|off", health:"ok\|broken\|maintenance", manual:"on"\|"off"\|null}]` (`speed` and `manual` are optional. Fans run all the time at `normal` and go `turbo` while CO builds up; `on` = running, i.e. `speed` is not `off`; `manual` `null`/missing = automatic, `"on"` = forced turbo) |
| `zones` | `[{name:"ZONE1", co_ppm:12.3, risk:"Safe\|Mid\|High\|Critical"}]` (50 ppm and up counts as Mid) |
| `history.occupancy` | `[{t:ISO time, occupied:int, total:int}]` |
| `history.co` | `[{t:ISO time, zone, ppm}]` |
| `history.arrivals` | `[{t:ISO time, count}]` in 5-minute buckets |
| `stats` | `{revenue, cars_served, penalty_count, penalty_total, refused_last_10min}` |
| `penalties` | `[{t, reason, amount, component}]` |
| `alerts` | optional. If present it is shown as-is: `[{severity:"critical\|warning\|info", kind, title, detail}]`. If absent the dashboard derives alerts itself. |

**One more endpoint for the 30-day history:** when someone picks a day, the dashboard calls
`GET {DASHBOARD_API_URL}/api/history?date=2026-01-01` and expects the list of that day's visits, same fields as `sessions`.
The backend should keep 30 days and delete anything older.

`mock_data.py` produces exactly this shape and is the reference example, and
`backend/dashboard_api.py` produces it from the live simulator.

### What level 1 actually looks like

The dashboard draws whatever the backend sends, so it fits both. For the record,
the organisers' level 1 is: **30 parking spots (S1-S30), all in one zone (ZONE1),
all of type `Any`; three barrier gates (gateA, gateB, gateC); no exhaust fans**
(`/api/v1/list-exhausts` is 404). Carbon monoxide comes from
`/api/v1/list-zones` (`gasCarbonMonoxideLevel` and `risk`). When a snapshot has
no fans the dashboard hides the fan panel, and it hides the CO panel when there
are no zones.

## Manual controls (Admin / Operator)

The sidebar has a "Signed in as" selector (Viewer, Operator, Admin). It is a **demo sign-in only**; the real login has to come
from the backend. The "Manual controls" card under the parking map then offers:

- Gates: **Open**, **Close**, **Auto**, **Repair**  (Open/Close hold the gate that way until **Auto** gives it back to normal operation)
- Exhaust fans: **Turbo**, **Off**, **Auto**, **Repair**  (Auto = always running, turbo automatically while CO builds up)

Viewer sees the card read-only. Admin also gets a "Control log" of what was pressed.

When `DASHBOARD_SOURCE=api`, each button sends:

```
POST {DASHBOARD_API_URL}/api/control/{gate|fan}/{name}/{action}
body:  {"role": "operator"}            (or "admin")
reply: {"ok": true, "message": "gate0 opened."}
```

- `name` is the gate or fan name from the snapshot (`gate0`, `fan1`, ...).
- `action` for gates: `open` (held open), `close` (held shut, cars cannot pass), `auto` (back to normal), `repair`. For fans: `on` (forced turbo), `off` (stopped), `auto` (normal speed, automatic turbo), `repair`.
- The **backend must enforce the rules**, the dashboard buttons are only a convenience:
  - `viewer` is refused (reply `{"ok": false, "message": "..."}`).
  - `repair` is only allowed if the component is `broken`; it then becomes `maintenance` until fixed.
  - A component whose `health` is not `ok` must never be operated (organiser rule). Refuse with `ok: false`.
  - Keep the dashboard's view consistent: the next `/api/snapshot` should show the new gate `state`/`manual` or fan `on`/`manual`. `auto` clears `manual` back to `null`.
- The reply `message` is shown to the user as-is, so keep it short and readable.

## Files

- `app.py`: page layout, auto-refresh, sidebar (role, demo buttons) and the Manual controls card
- `ui.py`: the HTML pieces (KPIs, parking map, gates, CO meters, system status, feed)
- `charts.py`: Plotly charts (occupancy, CO, arrivals, daily visits)
- `alerts.py`: rules that turn a snapshot into alerts
- `styles.py`: colours (light and dark) and CSS
- `data_source.py`: mock or API switch (`fetch_api`, `fetch_history`, `send_control`)
- `mock_data.py`: the fake car park, including `control()` for the manual buttons
- `db_source.py`: builds the snapshot from the SQLite database (read-only, `DASHBOARD_SOURCE=db`)
- `auth.py`: database login for `db` mode (Admin / Operator)
- `test_db_source.py`: offline tests for `db` mode (`python -B -m unittest test_db_source` from `dashboard/`)

Backend side (not in this folder):

- `backend/dashboard_api.py`: builds the snapshot above from CarFlow's live car
  state, the simulator client and the database, and serves the three endpoints.
  Wired in by `backend/run_flow.py`.
- `backend/test_dashboard_api.py`: its tests, including a set that runs against
  JSON captured from the real simulator (`backend/real_simulator_sample.json`).
