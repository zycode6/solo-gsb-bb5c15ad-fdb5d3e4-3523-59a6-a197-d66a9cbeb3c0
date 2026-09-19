"""SQLite connection management and schema initialisation."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id    TEXT PRIMARY KEY,
    mode        TEXT NOT NULL CHECK (mode IN ('validate', 'trial')),
    created_at  TEXT NOT NULL,
    matrix_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flights (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    runway        TEXT NOT NULL,
    position      INTEGER NOT NULL,
    flight_no     TEXT NOT NULL,
    scheduled_off TEXT NOT NULL,
    wake          TEXT NOT NULL,
    locked        INTEGER,
    UNIQUE(batch_id, runway, position)
);

CREATE TABLE IF NOT EXISTS violations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id          TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    runway            TEXT NOT NULL,
    leader_position   INTEGER NOT NULL,
    follower_position INTEGER NOT NULL,
    leader_no         TEXT NOT NULL,
    follower_no       TEXT NOT NULL,
    leader_wake       TEXT NOT NULL,
    follower_wake     TEXT NOT NULL,
    leader_time       TEXT NOT NULL,
    follower_time     TEXT NOT NULL,
    required_seconds  INTEGER NOT NULL,
    actual_seconds    INTEGER NOT NULL,
    shortfall_seconds INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS trial_items (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id       TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    runway         TEXT NOT NULL,
    position       INTEGER NOT NULL,
    flight_no      TEXT NOT NULL,
    wake           TEXT NOT NULL,
    scheduled_off  TEXT NOT NULL,
    adjusted_off   TEXT NOT NULL,
    delay_seconds  INTEGER NOT NULL,
    triggered_by   TEXT,
    closure_hit    INTEGER NOT NULL DEFAULT 0,
    closure_window_start TEXT,
    closure_window_end   TEXT,
    locked         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS closures (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id     TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    runway       TEXT NOT NULL,
    window_index INTEGER NOT NULL,
    start_utc    TEXT NOT NULL,
    end_utc      TEXT NOT NULL,
    UNIQUE(batch_id, runway, window_index)
);

CREATE TABLE IF NOT EXISTS closure_conflicts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    runway        TEXT NOT NULL,
    position      INTEGER NOT NULL,
    flight_no     TEXT NOT NULL,
    wake          TEXT NOT NULL,
    scheduled_off TEXT NOT NULL,
    window_index  INTEGER NOT NULL,
    window_start  TEXT NOT NULL,
    window_end    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capacities (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id       TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    runway         TEXT NOT NULL,
    window_seconds INTEGER NOT NULL,
    max_departures INTEGER NOT NULL,
    UNIQUE(batch_id, runway)
);

CREATE TABLE IF NOT EXISTS capacity_violations (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id               TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    runway                 TEXT NOT NULL,
    position               INTEGER NOT NULL,
    flight_no              TEXT NOT NULL,
    wake                   TEXT NOT NULL,
    scheduled_off          TEXT NOT NULL,
    window_seconds         INTEGER NOT NULL,
    max_departures         INTEGER NOT NULL,
    departures_in_window   INTEGER NOT NULL,
    anchor_flight_no       TEXT NOT NULL,
    anchor_position        INTEGER NOT NULL,
    window_start           TEXT NOT NULL,
    window_end             TEXT NOT NULL,
    earliest_compliant_off TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trial_blockers (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id               TEXT NOT NULL REFERENCES batches(batch_id) ON DELETE CASCADE,
    runway                 TEXT NOT NULL,
    position               INTEGER NOT NULL,
    flight_no              TEXT NOT NULL,
    wake                   TEXT NOT NULL,
    scheduled_off          TEXT NOT NULL,
    constraint_type        TEXT NOT NULL,
    required_earliest_off  TEXT NOT NULL,
    triggered_by           TEXT,
    required_gap_seconds   INTEGER,
    closure_hit            INTEGER NOT NULL DEFAULT 0,
    closure_window_start   TEXT,
    closure_window_end     TEXT,
    capacity_hit           INTEGER NOT NULL DEFAULT 0,
    capacity_window_seconds   INTEGER,
    capacity_max_departures   INTEGER,
    capacity_anchor        TEXT,
    capacity_window_start  TEXT,
    capacity_window_end    TEXT,
    UNIQUE(batch_id, runway)
);

CREATE INDEX IF NOT EXISTS idx_flights_batch ON flights(batch_id);
CREATE INDEX IF NOT EXISTS idx_violations_batch ON violations(batch_id);
CREATE INDEX IF NOT EXISTS idx_trial_batch ON trial_items(batch_id);
CREATE INDEX IF NOT EXISTS idx_closures_batch ON closures(batch_id);
CREATE INDEX IF NOT EXISTS idx_closure_conflicts_batch ON closure_conflicts(batch_id);
CREATE INDEX IF NOT EXISTS idx_capacities_batch ON capacities(batch_id);
CREATE INDEX IF NOT EXISTS idx_capacity_violations_batch ON capacity_violations(batch_id);
CREATE INDEX IF NOT EXISTS idx_trial_blockers_batch ON trial_blockers(batch_id);
"""

# Columns added after the initial release: existing database files (e.g. the
# Docker volume) are migrated in place, since CREATE TABLE IF NOT EXISTS never
# alters an already-present table.
_FLIGHT_COLUMNS = {
    # NULL = the request never carried ``locked``; 0/1 = explicitly given.
    "locked": "INTEGER",
}

_TRIAL_ITEM_COLUMNS = {
    "closure_hit": "INTEGER NOT NULL DEFAULT 0",
    "closure_window_start": "TEXT",
    "closure_window_end": "TEXT",
    # rolling-capacity support
    "capacity_hit": "INTEGER NOT NULL DEFAULT 0",
    "capacity_window_seconds": "INTEGER",
    "capacity_max_departures": "INTEGER",
    "capacity_anchor": "TEXT",
    "capacity_window_start": "TEXT",
    "capacity_window_end": "TEXT",
    # immovable (locked) flight support
    "locked": "INTEGER NOT NULL DEFAULT 0",
}


def _migrate(conn: sqlite3.Connection) -> None:
    flight_cols = {row[1] for row in conn.execute("PRAGMA table_info(flights)")}
    for name, ddl in _FLIGHT_COLUMNS.items():
        if name not in flight_cols:
            conn.execute(f"ALTER TABLE flights ADD COLUMN {name} {ddl}")
    existing = {row[1] for row in conn.execute("PRAGMA table_info(trial_items)")}
    for name, ddl in _TRIAL_ITEM_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE trial_items ADD COLUMN {name} {ddl}")


def _connect(db_path: str) -> sqlite3.Connection:
    directory = os.path.dirname(db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: str | None = None) -> None:
    """Create the schema (idempotent). Called once at application startup."""
    path = db_path or settings.db_path
    with _connect(path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


@contextmanager
def get_conn(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    """Transactional connection: commits on success, rolls back on error."""
    conn = _connect(db_path or settings.db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI dependency."""
    with get_conn() as conn:
        yield conn
