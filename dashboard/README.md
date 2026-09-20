# Business dashboard

A Streamlit page for the car park owner. It only **reads** the database the backend fills
(`database/parking.db`, or the file named in `PARKING_DB_PATH`). It never talks to the
simulator, so nothing here can move a car or open a gate.

```
Simulator <-> backend (run_all.py) --writes--> database --reads--> this dashboard
```

## Run it

```
pip install -U -r dashboard/requirements.txt
python backend/run_all.py --live                    # terminal 1 (simulator running)
python dashboard/create_user.py --username ks --role operator     # once per person
streamlit run dashboard/app.py                      # terminal 2, from the repo root
```

Run `streamlit` from the repo root so it picks up `.streamlit/config.toml` (the blue theme).

### Roles
| Role | Can do |
|---|---|
| `admin` | look at everything (read-only) |
| `operator` | look at everything, and control components (manual control has the highest priority) |

Passwords are stored only as salted PBKDF2 hashes in the `app_users` table.
`python dashboard/create_user.py --list` shows who exists.

### Demo data (for the presentation)
The real recording only covers a few hours. To show month views and the forecast:
```
python dashboard/make_demo_db.py                    # writes database/demo.db (30 made-up days)
python dashboard/create_user.py --db database/demo.db --username ks --role operator
PARKING_DB_PATH=database/demo.db streamlit run dashboard/app.py     # PowerShell: $env:PARKING_DB_PATH="database/demo.db"
```
The page shows a "Demo data" banner. The real database is never touched.

## What is on the screen (business first)
1. **Period**: Today / Yesterday / Last 7 days / Last 30 days / This month / Custom (any dates and hours). "Group by" picks 15 minutes, hour, day or month (Auto by default).
2. **Income** (the big number, with the change against the previous period; a period still running is compared with the same stretch of the previous one), **Occupancy now** (bays taken AND bays available) and **Penalties** (credits lost).
3. **Earnings over time**, then **Penalties**: a chart of credits lost and a table of every error (time, plate, what happened, credits lost).
4. **What to expect next**: estimate of income or arrivals for the next 6 / 12 / 24 hours, with a likely range.
5. **Usage**: busiest moment per period as % of the bays, and paying cars per period.
6. **Right now** (refreshes every 2 s): bay map with plates, gates, air quality, lights, alarms, latest activity.
7. **Gate control** (operators press, admins only look): per gate Hold open / Hold closed / Automatic, and Request repair (broken gate) or Send to maintenance (working gate, asks "are you sure"), plus a log of recent commands.
8. **Visit history**: every visit in the period, searchable by plate, downloadable as CSV.

## How the numbers are defined
* **Income** = bills the car logic sent (`charge_car`) whose payment it checked and accepted (`charges`, status `paid`). The simulator also sends fake payments on purpose and `payment_made` keeps every one, so `payment_made` is *not* used for income. Payments recorded before bill-checking existed are shown as unverified, with a note.
* **Penalties** = the simulator's `penalty` events. Each one costs **5 credits** (`PENALTY_CREDITS` in `biz_metrics.py`) whatever fine amount the simulator prints. It is a credit score: it is never subtracted from income.
* **Occupancy** = bays whose latest `car_spot_action` is a CarIn, counted per bay, so a missed event can never make it drift above the number of bays. "Now" comes from `list_parking_spots`.
* **Times** are the simulator's own clock (`ServerDateTime`).
* **Forecast** (`forecast.py`): with 2+ days of history, the same hour on earlier days scaled by the last 24 hours; with less, the recent level carried forward. Hours when the simulator was not running are left out, not counted as zero. It is an estimate, and the page says so.

## Files
| File | Job |
|---|---|
| `app.py` | the page (sign-in, layout, refresh) |
| `db_read.py` | read-only queries on the backend's tables |
| `biz_metrics.py` | periods, buckets, visits, occupancy, headline numbers |
| `forecast.py` | the estimate |
| `ui_theme.py`, `ui_charts.py` | colours (checked for colour-blind safety) and charts |
| `login.py`, `create_user.py` | password hashing, roles, adding users |
| `control_client.py` | the buttons' only way to act: a request to the backend (`/api/control/...`), which checks the key and the user's role and then sends the command |
| `make_demo_db.py` | the demo database |
| `test_*.py` | `python -m unittest discover -s dashboard` |

## Tests
```
cd dashboard && python -m unittest discover
```

## Gate control: how it works
The dashboard never talks to the simulator. A button sends a request to the backend (`python backend/run_all.py --live` must be running); the backend
1. accepts it only from this computer and only with the shared key (`database/control.token`, made by the backend; or `CONTROL_TOKEN`),
2. looks the user up in `app_users` and refuses anyone who is not an operator (the role is never taken from the request),
3. and then acts. **Held open / held closed is the final order**: the car logic's own open/close commands are dropped while a gate is held, and a check every 3 s puts a gate back if something else moved it. The order is saved in the database and survives a restart.
4. The one exception is the organisers' rule: a broken gate or one under maintenance is never operated (its buttons are off; a saved order is applied again once it works).
Every command, done or refused, is written to `control_log` and shown under "Recent commands". If the backend is not running the controls are switched off.
