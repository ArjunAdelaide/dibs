"""SQLite storage: users, conversation history, booking proposals, bookings."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    handle TEXT PRIMARY KEY,
    prefs TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conv_id TEXT NOT NULL,
    handle TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_conv ON messages(conv_id, id);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY,
    conv_id TEXT NOT NULL,
    handle TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    party_size INTEGER NOT NULL,
    deal_id TEXT,
    rate TEXT,
    shown TEXT,
    total_cents INTEGER,
    ttl_minutes INTEGER NOT NULL DEFAULT 30,
    notes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY,
    proposal_id INTEGER NOT NULL REFERENCES proposals(id),
    status TEXT NOT NULL DEFAULT 'needs_human',
    reference TEXT NOT NULL DEFAULT '',
    amount_cents INTEGER,
    payment_intent TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY,
    conv_id TEXT NOT NULL,
    handle TEXT NOT NULL,
    venue_id TEXT NOT NULL,
    day TEXT,
    weekday TEXT,
    time_from TEXT NOT NULL,
    time_to TEXT NOT NULL,
    party_size INTEGER NOT NULL,
    max_price REAL,
    repeat INTEGER NOT NULL DEFAULT 0,
    last_fired_for TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    last_checked TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS event_alerts (
    id INTEGER PRIMARY KEY,
    conv_id TEXT NOT NULL,
    handle TEXT NOT NULL,
    kind TEXT NOT NULL,
    event_id TEXT,
    event_name TEXT,
    url TEXT,
    keyword TEXT,
    fire_at TEXT,
    label TEXT NOT NULL DEFAULT '',
    seen_ids TEXT NOT NULL DEFAULT '[]',
    place TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    last_checked TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS discovered (
    id TEXT PRIMARY KEY,
    tile TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS areas (
    tile TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if "rate" not in [c[1] for c in conn.execute("PRAGMA table_info(proposals)")]:  # databases from before live rates
        conn.execute("ALTER TABLE proposals ADD COLUMN rate TEXT")
    if "shown" not in [c[1] for c in conn.execute("PRAGMA table_info(proposals)")]:  # databases from before exact summaries
        conn.execute("ALTER TABLE proposals ADD COLUMN shown TEXT")
    for table, column, kind in (("proposals", "total_cents", "INTEGER"), ("proposals", "ttl_minutes", "INTEGER NOT NULL DEFAULT 30"),
                                ("alerts", "repeat", "INTEGER NOT NULL DEFAULT 0"), ("alerts", "last_fired_for", "TEXT")):
        if column not in [c[1] for c in conn.execute(f"PRAGMA table_info({table})")]:  # databases from before stage 1
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
    if "place" not in [c[1] for c in conn.execute("PRAGMA table_info(event_alerts)")]:  # databases from before worldwide events
        conn.execute("ALTER TABLE event_alerts ADD COLUMN place TEXT")
    if "payment_intent" not in [c[1] for c in conn.execute("PRAGMA table_info(bookings)")]:  # databases from before payments
        conn.execute("ALTER TABLE bookings ADD COLUMN amount_cents INTEGER")
        conn.execute("ALTER TABLE bookings ADD COLUMN payment_intent TEXT")
    return conn


def get_prefs(conn: sqlite3.Connection, handle: str) -> dict:
    row = conn.execute("SELECT prefs FROM users WHERE handle = ?", (handle,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO users (handle, created_at) VALUES (?, ?)", (handle, now_iso()))
        conn.commit()
        return {}
    return json.loads(row["prefs"])


def set_pref(conn: sqlite3.Connection, handle: str, key: str, value) -> dict:
    prefs = get_prefs(conn, handle)
    prefs[key] = value
    conn.execute("UPDATE users SET prefs = ? WHERE handle = ?", (json.dumps(prefs), handle))
    conn.commit()
    return prefs


def add_message(conn: sqlite3.Connection, conv_id: str, handle: str, role: str, content: str) -> None:
    conn.execute(
        "INSERT INTO messages (conv_id, handle, role, content, created_at) VALUES (?, ?, ?, ?, ?)",
        (conv_id, handle, role, content, now_iso()),
    )
    conn.commit()


def history(conn: sqlite3.Connection, conv_id: str, limit: int) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT handle, role, content FROM messages WHERE conv_id = ? ORDER BY id DESC LIMIT ?",
        (conv_id, limit),
    ).fetchall()
    return list(reversed(rows))


def user_messages_since(conn: sqlite3.Connection, handle: str, since_iso: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM messages WHERE handle = ? AND role = 'user' AND created_at > ?",
                        (handle, since_iso)).fetchone()[0]


def last_assistant_message(conn: sqlite3.Connection, conv_id: str) -> str:
    row = conn.execute("SELECT content FROM messages WHERE conv_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1",
                       (conv_id,)).fetchone()
    return row["content"] if row else ""


def kv_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
    conn.commit()
