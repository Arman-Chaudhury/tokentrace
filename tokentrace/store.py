"""SQLite storage for events.

One table of events plus a sessions index.  Every report is a query over the
same table, so a future cross-tool dashboard reads exactly what the per-session
report reads.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from . import pricing
from .schema import Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id   TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    path         TEXT,
    title        TEXT,
    first_ts     TEXT,
    last_ts      TEXT,
    ingested_at  REAL NOT NULL,
    pricing_version TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    session_id   TEXT NOT NULL,
    id           TEXT NOT NULL,
    source       TEXT NOT NULL,
    kind         TEXT NOT NULL,
    ts           TEXT,
    agent_id     TEXT,
    turn         INTEGER NOT NULL DEFAULT 0,
    parent_id    TEXT,
    model        TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens      INTEGER NOT NULL DEFAULT 0,
    tool_name    TEXT,
    payload      TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (session_id, id)
);
CREATE INDEX IF NOT EXISTS events_kind_ts ON events (kind, ts);
CREATE INDEX IF NOT EXISTS events_model ON events (model);
"""

COLUMNS = (
    "session_id", "id", "source", "kind", "ts", "agent_id", "turn", "parent_id", "model",
    "input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens", "tool_name", "payload",
)


def open_db(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def ingest(conn: sqlite3.Connection, events: Iterable[Event], path: str | None = None,
           title: str | None = None) -> int:
    """Insert or replace a session's events.  Returns the number stored."""
    events = list(events)
    if not events:
        return 0
    session_id = events[0].session_id
    source = events[0].source
    ts_values = sorted(e.ts for e in events if e.ts)
    rows = [
        (
            e.session_id, e.id, e.source, e.kind, e.ts, e.agent_id, e.turn, e.parent_id, e.model,
            e.input_tokens, e.cache_read_tokens, e.cache_write_tokens, e.output_tokens, e.tool_name,
            json.dumps(e.payload, ensure_ascii=False),
        )
        for e in events
    ]
    with conn:
        conn.execute("DELETE FROM events WHERE session_id = ?", (session_id,))
        conn.executemany(
            f"INSERT INTO events ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))})", rows
        )
        conn.execute(
            "INSERT OR REPLACE INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id, source, path, title,
                ts_values[0] if ts_values else None, ts_values[-1] if ts_values else None,
                time.time(), pricing.PRICING_VERSION,
            ),
        )
    return len(rows)


def load_events(conn: sqlite3.Connection, session_id: str) -> list[Event]:
    rows = conn.execute(
        "SELECT * FROM events WHERE session_id = ? ORDER BY ts, kind", (session_id,)
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d.pop("payload") or "{}")
        out.append(Event.from_dict(d))
    return out


def sessions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM sessions ORDER BY last_ts DESC").fetchall()]


def daily_totals(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Tokens and estimated cost per UTC day and model, the seed of a dashboard."""
    rows = conn.execute(
        """
        SELECT substr(ts, 1, 10) AS day, source, model,
               COUNT(*) AS calls,
               SUM(input_tokens) AS input_tokens,
               SUM(cache_read_tokens) AS cache_read_tokens,
               SUM(cache_write_tokens) AS cache_write_tokens,
               SUM(output_tokens) AS output_tokens
        FROM events
        WHERE kind = 'api_call' AND ts != ''
        GROUP BY day, source, model
        ORDER BY day DESC, model
        """
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["cost_usd"] = pricing.cost_usd(
            d["model"], d["input_tokens"], d["cache_read_tokens"], d["cache_write_tokens"], d["output_tokens"]
        )
        out.append(d)
    return out
