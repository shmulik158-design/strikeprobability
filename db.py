"""SQLite access for tracker.db. Schema follows SPEC.md."""
import json
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.environ.get("TRACKER_DB", os.path.join(os.path.dirname(__file__), "data", "tracker.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    source      TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    value       REAL,
    raw_json    TEXT,
    ok          INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS obs_source_time ON observations(source, observed_at);
CREATE TABLE IF NOT EXISTS articles (
    url TEXT PRIMARY KEY, title TEXT, outlet TEXT, published_at TEXT, extracted_json TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, actor TEXT, action TEXT, target TEXT, event_date TEXT,
    severity INTEGER, n_sources INTEGER, first_seen TEXT
);
CREATE TABLE IF NOT EXISTS forecasts (
    ts TEXT PRIMARY KEY, p_market REAL, p_model REAL, adjustments_json TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


MIGRATIONS = [
    # Phase 2: dedupe key, fetch time, and article->event link
    ("articles", "title_norm", "ALTER TABLE articles ADD COLUMN title_norm TEXT"),
    ("articles", "fetched_at", "ALTER TABLE articles ADD COLUMN fetched_at TEXT"),
    ("articles", "event_id", "ALTER TABLE articles ADD COLUMN event_id INTEGER"),
    ("events", "is_new_event", "ALTER TABLE events ADD COLUMN is_new_event INTEGER"),
    ("events", "sample_url", "ALTER TABLE events ADD COLUMN sample_url TEXT"),
    ("events", "sample_title", "ALTER TABLE events ADD COLUMN sample_title TEXT"),
]


def connect(path: str = DB_PATH) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    for table, col, sql in MIGRATIONS:
        if col not in {r[1] for r in con.execute(f"PRAGMA table_info({table})")}:
            con.execute(sql)
    con.execute("CREATE INDEX IF NOT EXISTS articles_title_norm ON articles(title_norm)")
    con.execute("CREATE INDEX IF NOT EXISTS articles_published ON articles(published_at)")
    return con


def write_obs(con, source, value, raw, ok=True, observed_at=None, fetched_at=None):
    """Insert one observation. Re-inserting the same (source, observed_at) replaces it."""
    fetched_at = fetched_at or now_iso()
    con.execute(
        "INSERT OR REPLACE INTO observations(source, observed_at, fetched_at, value, raw_json, ok)"
        " VALUES (?,?,?,?,?,?)",
        (source, observed_at or fetched_at, fetched_at, value,
         json.dumps(raw, ensure_ascii=False) if raw is not None else None, 1 if ok else 0),
    )
