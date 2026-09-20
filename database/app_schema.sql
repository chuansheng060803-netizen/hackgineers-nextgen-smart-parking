-- Our OWN records: things the backend decides or the dashboard needs, as opposed to
-- simulator_schema.sql, which holds only what the simulator sends (in its names).
-- Created automatically by backend/store.py when it starts; safe to run again.

-- One row per bill the car logic sent to the simulator (charge_car), and what became
-- of it. The simulator also sends FAKE payments on purpose, and payment_made keeps
-- every one of them, so income must be read from here: status 'paid' means the car
-- logic checked the paid amount against this bill and accepted it.
CREATE TABLE IF NOT EXISTS charges (
    charge_id       INTEGER PRIMARY KEY,
    CarPlateNumber  TEXT NOT NULL,
    CarType         TEXT,
    billed_parking  REAL NOT NULL,
    billed_charging REAL NOT NULL,
    billed_total    REAL NOT NULL,
    billed_at       TEXT NOT NULL,          -- local time, same clock as ServerDateTime
    status          TEXT NOT NULL DEFAULT 'billed' CHECK (status IN ('billed', 'paid')),
    paid_at         TEXT                    -- local time; NULL until the payment was accepted
);
CREATE INDEX IF NOT EXISTS idx_charges_paid_at ON charges (paid_at);
CREATE INDEX IF NOT EXISTS idx_charges_plate ON charges (CarPlateNumber);

-- Dashboard sign-in. Passwords are stored only as a salted hash. Two roles:
--   admin    can look at everything (read-only)
--   operator can look at everything AND control components (gates); manual control
--            has the highest priority in the backend.
CREATE TABLE IF NOT EXISTS app_users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'operator')),
    created_at    TEXT NOT NULL
);

-- Manual gate control. One row per gate: who last set it and to what.
--   auto          the car logic decides (normal)
--   forced_open   held open by an operator; nothing overrides it (except a broken /
--                 under-maintenance gate, which nobody may operate)
--   forced_closed held closed by an operator; same rule
-- The backend keeps this table current and re-reads it on start, so a restart
-- does not silently drop an operator's order.
CREATE TABLE IF NOT EXISTS gate_control (
    gate    TEXT PRIMARY KEY,
    mode    TEXT NOT NULL CHECK (mode IN ('auto', 'forced_open', 'forced_closed')),
    set_by  TEXT NOT NULL,
    set_at  TEXT NOT NULL                   -- local time, same clock as ServerDateTime
);

-- Every manual (or enforcing) gate command, newest last: who, what, and what came of it.
CREATE TABLE IF NOT EXISTS control_log (
    log_id   INTEGER PRIMARY KEY,
    at       TEXT NOT NULL,
    username TEXT NOT NULL,                 -- 'system' for the backend correcting a gate
    role     TEXT,
    gate     TEXT NOT NULL,
    mode     TEXT NOT NULL,                 -- the mode that was asked for
    result   TEXT NOT NULL,                 -- 'done' or 'refused'
    detail   TEXT
);
CREATE INDEX IF NOT EXISTS idx_control_log_at ON control_log (at);
