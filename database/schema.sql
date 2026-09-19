PRAGMA foreign_keys = ON;

-- =========================================================
-- USERS
-- Admin / Operator accounts for the web system
-- =========================================================

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('Admin', 'Operator')),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);


-- =========================================================
-- PARKING SPOTS
-- Current state of every parking spot
-- =========================================================

CREATE TABLE IF NOT EXISTS parking_spots (
    name TEXT PRIMARY KEY,
    zone TEXT,
    purpose TEXT,
    parking_for_car_type TEXT,

    status TEXT NOT NULL DEFAULT 'available',
    current_car TEXT,

    broken INTEGER NOT NULL DEFAULT 0,
    under_maintenance INTEGER NOT NULL DEFAULT 0,

    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);


-- =========================================================
-- GATES
-- Current state of barrier gates
-- =========================================================

CREATE TABLE IF NOT EXISTS gates (
    name TEXT PRIMARY KEY,

    state TEXT NOT NULL DEFAULT 'Closed',

    broken INTEGER NOT NULL DEFAULT 0,
    under_maintenance INTEGER NOT NULL DEFAULT 0,

    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);


-- =========================================================
-- PARKING SESSIONS
-- One record for each car visit
-- =========================================================

CREATE TABLE IF NOT EXISTS parking_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    car_name TEXT NOT NULL,
    car_type TEXT,

    spot_name TEXT,

    arrival_time TEXT NOT NULL,
    parked_time TEXT,
    departure_time TEXT,

    status TEXT NOT NULL DEFAULT 'active',

    FOREIGN KEY (spot_name)
        REFERENCES parking_spots(name)
        ON UPDATE CASCADE
        ON DELETE SET NULL
);


-- =========================================================
-- PAYMENTS
-- Charges associated with a parking session
-- =========================================================

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    session_id INTEGER NOT NULL,

    parking_cost REAL NOT NULL DEFAULT 0,
    charging_cost REAL NOT NULL DEFAULT 0,

    status TEXT NOT NULL DEFAULT 'pending',
    paid_at TEXT,

    FOREIGN KEY (session_id)
        REFERENCES parking_sessions(id)
        ON DELETE CASCADE
);


-- =========================================================
-- EVENTS
-- Raw simulator/backend events for searching and dashboard
-- =========================================================

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    event_type TEXT NOT NULL,

    car_name TEXT,
    component_name TEXT,

    payload_json TEXT NOT NULL,

    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);


-- =========================================================
-- INDEXES
-- Makes event/session searching faster
-- =========================================================

CREATE INDEX IF NOT EXISTS idx_events_type
ON events(event_type);

CREATE INDEX IF NOT EXISTS idx_events_created_at
ON events(created_at);

CREATE INDEX IF NOT EXISTS idx_events_car
ON events(car_name);

CREATE INDEX IF NOT EXISTS idx_sessions_car
ON parking_sessions(car_name);

CREATE INDEX IF NOT EXISTS idx_sessions_status
ON parking_sessions(status);