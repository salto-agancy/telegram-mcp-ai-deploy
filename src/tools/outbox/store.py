"""The outbox journal: one row per thing an agent left for the owner.

Kept in a small SQLite file shared by both account containers (OUTBOX_DB), not in
the production PostgreSQL: it is this service's own operational state, like the
OAuth store, and it must not need a database migration to exist.

A row records what the agent wrote (``text_agent``). Background reconciliation
later fills what the chat holds now (``text_now``), how much the owner changed
(``edit_ratio``) and whether it was sent. ``operation_key`` makes a retried call
return the first result instead of writing twice.
"""

from __future__ import annotations

import asyncio
import datetime
import difflib
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_PATH = "/data/outbox/outbox.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    id            INTEGER PRIMARY KEY,
    created_at    TEXT NOT NULL,
    account       TEXT NOT NULL,
    agent         TEXT NOT NULL,
    operation_key TEXT,
    kind          TEXT NOT NULL CHECK (kind IN ('draft', 'parking', 'scheduled')),
    peer          TEXT NOT NULL,
    peer_id       INTEGER,
    peer_title    TEXT,
    topic_id      INTEGER,
    tg_msg_id     INTEGER,
    scheduled_for TEXT,
    seq           INTEGER,
    text_agent    TEXT NOT NULL,
    version_agent TEXT NOT NULL,
    status        TEXT NOT NULL,
    text_now      TEXT,
    version_now   TEXT,
    edit_ratio    REAL,
    sent_msg_id   INTEGER,
    sent_at       TEXT,
    checked_at    TEXT,
    result        TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS outbox_op ON outbox(account, operation_key, seq)
    WHERE operation_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS outbox_peer ON outbox(account, peer_id, topic_id);
-- Messages sent now (send_message): one row per message of a series, keyed like
-- the outbox so a retry finds what was already delivered. Additive: no migration.
CREATE TABLE IF NOT EXISTS sends (
    id            INTEGER PRIMARY KEY,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    account       TEXT NOT NULL,
    agent         TEXT NOT NULL,
    operation_key TEXT NOT NULL,
    seq           INTEGER NOT NULL,
    peer          TEXT NOT NULL,
    peer_id       INTEGER,
    text_version  TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('sending', 'sent', 'failed')),
    tg_msg_id     INTEGER,
    link          TEXT,
    error_code    TEXT,
    error         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS sends_op ON sends(account, operation_key, seq);
CREATE TABLE IF NOT EXISTS outbox_events (
    id        INTEGER PRIMARY KEY,
    outbox_id INTEGER NOT NULL REFERENCES outbox(id),
    at        TEXT NOT NULL,
    event     TEXT NOT NULL,
    detail    TEXT
);
"""

_lock = asyncio.Lock()


def now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def version(text: str | None) -> str:
    """Short fingerprint of a text: what an agent read and must still match to write."""
    return hashlib.sha256((text or "").encode()).hexdigest()[:12]


def edit_ratio(before: str | None, after: str | None) -> float:
    """Share of the text the owner changed, 0.0 (untouched) … 1.0 (rewritten)."""
    return round(
        1.0 - difflib.SequenceMatcher(None, before or "", after or "").ratio(), 3
    )


def db_path() -> Path:
    return Path(os.environ.get("OUTBOX_DB") or DEFAULT_PATH)


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(_SCHEMA)
    return conn


def _run(fn, *args, **kwargs):
    conn = _connect()
    try:
        out = fn(conn, *args, **kwargs)
        conn.commit()
        return out
    finally:
        conn.close()


async def call(fn, *args, **kwargs):
    async with _lock:
        return await asyncio.to_thread(_run, fn, *args, **kwargs)


def _find_op(conn: sqlite3.Connection, account: str, operation_key: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM outbox WHERE account = ? AND operation_key = ? ORDER BY seq, id",
        (account, operation_key),
    ).fetchall()
    return [dict(r) for r in rows]


async def find_operation(account: str, operation_key: str | None) -> list[dict]:
    if not operation_key:
        return []
    return await call(_find_op, account, operation_key)


def _insert(
    conn: sqlite3.Connection, row: dict[str, Any], event_detail: dict | None
) -> int:
    cols = list(row)
    cur = conn.execute(
        f"INSERT INTO outbox ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [row[c] for c in cols],
    )
    oid = int(cur.lastrowid)
    conn.execute(
        "INSERT INTO outbox_events (outbox_id, at, event, detail) VALUES (?, ?, ?, ?)",
        (
            oid,
            row["created_at"],
            "created",
            json.dumps(event_detail or {}, ensure_ascii=False),
        ),
    )
    return oid


async def record(row: dict[str, Any], event_detail: dict | None = None) -> int:
    row = {"created_at": now_iso(), **row}
    row.setdefault("version_agent", version(row.get("text_agent")))
    return await call(_insert, row, event_detail)


def _event(
    conn: sqlite3.Connection, outbox_id: int, event: str, detail: dict, updates: dict
) -> None:
    if updates:
        sets = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE outbox SET {sets} WHERE id = ?", [*updates.values(), outbox_id]
        )
    conn.execute(
        "INSERT INTO outbox_events (outbox_id, at, event, detail) VALUES (?, ?, ?, ?)",
        (outbox_id, now_iso(), event, json.dumps(detail, ensure_ascii=False)),
    )


async def add_event(
    outbox_id: int, event: str, detail: dict | None = None, **updates
) -> None:
    await call(_event, outbox_id, event, detail or {}, updates)


OPEN_DRAFT_STATES = ("saved", "waiting", "edited", "already_there")


def _supersede(
    conn: sqlite3.Connection,
    account: str,
    peer_id: int,
    topic_id: int | None,
    new_id: int,
) -> int:
    """Older open drafts of the same chat/topic were replaced by the agent, not edited by
    the owner; mark them so the report does not blame the owner for the change."""
    marks = ", ".join("?" * len(OPEN_DRAFT_STATES))
    rows = conn.execute(
        "SELECT id FROM outbox WHERE account = ? AND peer_id = ? AND coalesce(topic_id, 0) = ? "
        f"AND kind = 'draft' AND id <> ? AND status IN ({marks})",
        (account, peer_id, topic_id or 0, new_id, *OPEN_DRAFT_STATES),
    ).fetchall()
    for (oid,) in rows:
        _event(
            conn,
            oid,
            "superseded",
            {"by": new_id},
            {"status": "superseded", "result": f"outbox:{new_id}"},
        )
    return len(rows)


async def supersede_open_drafts(
    account: str, peer_id: int, topic_id: int | None, new_id: int
) -> int:
    return await call(_supersede, account, peer_id, topic_id, new_id)


def _agent_wrote(
    conn: sqlite3.Connection,
    account: str,
    peer_id: int,
    topic_id: int | None,
    text: str,
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM outbox WHERE account = ? AND peer_id = ? AND coalesce(topic_id, 0) = ? "
        "AND kind = 'draft' AND text_agent = ? "
        "AND status IN ('saved', 'waiting', 'superseded', 'already_there') LIMIT 1",
        (account, peer_id, topic_id or 0, text),
    ).fetchone()
    return row is not None


async def agent_wrote(
    account: str, peer_id: int, topic_id: int | None, text: str
) -> bool:
    """Whether this exact text in the field was put there by an agent through this server."""
    return await call(_agent_wrote, account, peer_id, topic_id, text)
