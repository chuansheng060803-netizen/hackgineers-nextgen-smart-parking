-- =========================================================
-- SIMULATOR DATA  (written by backend/store.py, read by the dashboard)
--
-- Every table and column keeps the name the simulator gave it:
--   * one table per REST endpoint  (GET /api/v1/list-parking-spots -> list_parking_spots)
--   * one table per webhook EventClass (car_spot_action, payment_made, ...)
--   * columns are the simulator's own field names (parkingForCarType,
--     isUnderMaintenance, CarPlateNumber, FineAmount, ...), and values are stored
--     as the simulator sent them (Amount stays the text "1.03").
-- Nothing here replaces the older tables in schema.sql; they live side by side.
-- Anything not listed as a column is still kept in raw_json, so no field is lost.
-- =========================================================

-- ---------------------------------------------------------
-- REST: current state (one row per item, updated only when something changed)
-- ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS list_parking_spots (
    "name"               TEXT PRIMARY KEY,
    "purpose"            TEXT,
    "parkingForCarType"  TEXT,
    "zoneParent"         TEXT,
    "detectedCars"       INTEGER,
    "broken"             INTEGER,
    "isUnderMaintenance" INTEGER,
    raw_json             TEXT NOT NULL,
    first_seen_at        TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS list_barriers (
    "name"               TEXT PRIMARY KEY,
    "zoneParent"         TEXT,
    "state"              TEXT,
    "broken"             INTEGER,
    "isUnderMaintenance" INTEGER,
    raw_json             TEXT NOT NULL,
    first_seen_at        TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS list_zones (
    "name"                   TEXT PRIMARY KEY,
    "gasCarbonMonoxideLevel" REAL,
    "risk"                   TEXT,
    raw_json                 TEXT NOT NULL,
    first_seen_at            TEXT NOT NULL,
    updated_at               TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS list_lights (
    "name"       TEXT PRIMARY KEY,
    "group"      TEXT,
    "zoneParent" TEXT,
    "isOn"       INTEGER,
    raw_json      TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- The alarm list was empty in every sample, so its fields are not known yet.
-- Each alarm is kept whole in raw_json; "name" is the alarm's own name/Name/id,
-- or a short hash of its content when it has none.
CREATE TABLE IF NOT EXISTS list_alarms (
    "name"        TEXT PRIMARY KEY,
    raw_json      TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- GET /api/v1/status is one object, so this table always has at most one row (id = 1).
CREATE TABLE IF NOT EXISTS status (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    "isActive"    INTEGER,
    "cars"        INTEGER,
    raw_json      TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);


-- ---------------------------------------------------------
-- REST: change log.  One row per field that changed, with the old and the new
-- value (first sight of an item logs its starting values, old_value NULL).
-- Occupancy and CO history are rebuilt from this table.
-- ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS state_changes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at TEXT NOT NULL,
    endpoint   TEXT NOT NULL,      -- e.g. list-parking-spots
    "name"     TEXT NOT NULL,      -- the item, e.g. S3
    field      TEXT NOT NULL,      -- e.g. detectedCars
    old_value  TEXT,
    new_value  TEXT
);

CREATE INDEX IF NOT EXISTS idx_state_changes_item
ON state_changes(endpoint, "name", field, changed_at);

CREATE INDEX IF NOT EXISTS idx_state_changes_time
ON state_changes(changed_at);


-- ---------------------------------------------------------
-- Webhooks: every event, once.  EventId is the primary key, so an event that
-- arrives twice is stored once.  Order events by SequenceId, not by arrival.
-- ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS webhook_events (
    "EventId"        TEXT PRIMARY KEY,
    "EventClass"     TEXT NOT NULL,
    "SequenceId"     INTEGER,
    "Signature"      TEXT,
    "ServerDateTime" TEXT,          -- simulator local time, text, as sent
    received_at      TEXT NOT NULL, -- when we received it (UTC)
    raw_json         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_webhook_events_seq
ON webhook_events("SequenceId");

CREATE INDEX IF NOT EXISTS idx_webhook_events_class
ON webhook_events("EventClass", "SequenceId");


-- ---------------------------------------------------------
-- Webhooks: one table per EventClass, with the simulator's own field names.
-- ---------------------------------------------------------

CREATE TABLE IF NOT EXISTS car_spot_action (
    "EventId"                          TEXT PRIMARY KEY,
    "SequenceId"                       INTEGER,
    "ServerDateTime"                   TEXT,
    "CarPlateNumber"                   TEXT,
    "CarType"                          TEXT,
    "SpotName"                         TEXT,
    "SpotType"                         TEXT,   -- EntrySpot / Park / ExitSpot
    "Direction"                        TEXT,   -- CarIn / CarOut
    "PlannedParkingDurationInMinutes"  TEXT,   -- text, "0" on exit events
    raw_json                           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_car_spot_action_car
ON car_spot_action("CarPlateNumber", "SequenceId");

CREATE INDEX IF NOT EXISTS idx_car_spot_action_spot
ON car_spot_action("SpotName", "SequenceId");

CREATE TABLE IF NOT EXISTS payment_made (
    "EventId"        TEXT PRIMARY KEY,
    "SequenceId"     INTEGER,
    "ServerDateTime" TEXT,
    "CarPlateNumber" TEXT,
    "Amount"         TEXT,      -- text, e.g. "1.03"
    "Reason"         TEXT,
    raw_json         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_payment_made_car
ON payment_made("CarPlateNumber", "SequenceId");

-- The car's plate number is in ComponentName (there is no CarPlateNumber field).
CREATE TABLE IF NOT EXISTS penalty (
    "EventId"        TEXT PRIMARY KEY,
    "SequenceId"     INTEGER,
    "ServerDateTime" TEXT,
    "Reason"         TEXT,
    "FineAmount"     TEXT,      -- text, e.g. "10"
    "Type"           TEXT,      -- e.g. Car
    "ComponentName"  TEXT,
    raw_json         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_penalty_component
ON penalty("ComponentName", "SequenceId");

CREATE TABLE IF NOT EXISTS gate_action (
    "EventId"        TEXT PRIMARY KEY,
    "SequenceId"     INTEGER,
    "ServerDateTime" TEXT,
    "Name"           TEXT,      -- e.g. gateA
    "Action"         TEXT,      -- Open / Closed
    raw_json         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gate_action_name
ON gate_action("Name", "SequenceId");

-- The next three do not occur in Level 1, so their extra fields are unconfirmed:
-- only the common fields have columns; everything else is in raw_json.
CREATE TABLE IF NOT EXISTS component_broken (
    "EventId"        TEXT PRIMARY KEY,
    "SequenceId"     INTEGER,
    "ServerDateTime" TEXT,
    raw_json         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS component_fixed (
    "EventId"        TEXT PRIMARY KEY,
    "SequenceId"     INTEGER,
    "ServerDateTime" TEXT,
    raw_json         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS carbon_monoxide_event (
    "EventId"        TEXT PRIMARY KEY,
    "SequenceId"     INTEGER,
    "ServerDateTime" TEXT,
    raw_json         TEXT NOT NULL
);


-- ---------------------------------------------------------
-- Freshness: how old is the data?  (the dashboard shows "data N s old")
-- ---------------------------------------------------------

-- One row per polled endpoint.
CREATE TABLE IF NOT EXISTS poll_status (
    endpoint         TEXT PRIMARY KEY,   -- e.g. list-parking-spots
    last_ok_at       TEXT,
    last_error_at    TEXT,
    last_error       TEXT,
    ok_count         INTEGER NOT NULL DEFAULT 0,
    error_count      INTEGER NOT NULL DEFAULT 0,
    item_count       INTEGER,
    last_duration_ms INTEGER
);

-- Small key/value facts: last_event_at (last webhook received), writer_started_at.
CREATE TABLE IF NOT EXISTS ingest_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL
);
