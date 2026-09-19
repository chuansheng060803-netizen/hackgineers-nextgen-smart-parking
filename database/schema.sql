CREATE TABLE IF NOT EXISTS parking_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    plate TEXT NOT NULL,
    car_type TEXT,

    entry_gate_name TEXT,
    arrival_time TEXT,

    spot_name TEXT,
    start_park TEXT,
    end_park TEXT,

    exit_gate_name TEXT,
    exit_time TEXT,

    status TEXT NOT NULL DEFAULT 'ACTIVE'
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    session_id INTEGER NOT NULL,

    parking_cost REAL NOT NULL DEFAULT 0,
    charging_cost REAL NOT NULL DEFAULT 0,

    status TEXT NOT NULL DEFAULT 'PENDING',

    paid_time TEXT,
    completed_time TEXT,

    FOREIGN KEY (session_id)
        REFERENCES parking_sessions(id)
);

CREATE TABLE IF NOT EXISTS parking_spots (
    name TEXT PRIMARY KEY,

    zone TEXT,

    status TEXT NOT NULL DEFAULT 'FREE',

    current_plate TEXT,

    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS gates (
    name TEXT PRIMARY KEY,

    status TEXT NOT NULL,

    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    event_id TEXT,
    event_class TEXT,

    plate TEXT,

    event_time TEXT,

    raw_data TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    username TEXT NOT NULL UNIQUE,

    password_hash TEXT NOT NULL,

    role TEXT NOT NULL
        CHECK (role IN ('ADMIN', 'OPERATOR'))
);