"""追記専用イベントストア（SQLite）。仕様 §2：ログは追記のみ（上書き・削除しない）。

UPDATE / DELETE はトリガで拒否する。参加者表も追記のみ。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Optional

from engine.events import Event

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    type TEXT NOT NULL,
    t REAL NOT NULL,
    task_id TEXT NOT NULL,
    in_support INTEGER NOT NULL,
    payload TEXT NOT NULL,
    UNIQUE (participant_id, event_id)
);
CREATE INDEX IF NOT EXISTS events_pid ON events (participant_id, seq);
CREATE TABLE IF NOT EXISTS participants (
    participant_id TEXT PRIMARY KEY,
    token TEXT NOT NULL UNIQUE,
    grp TEXT NOT NULL,
    created_t REAL NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
    BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
    BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS participants_no_update BEFORE UPDATE ON participants
    BEGIN SELECT RAISE(ABORT, 'participants is append-only'); END;
CREATE TRIGGER IF NOT EXISTS participants_no_delete BEFORE DELETE ON participants
    BEGIN SELECT RAISE(ABORT, 'participants is append-only'); END;
"""


class EventStore:
    def __init__(self, path: str) -> None:
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.executescript(_SCHEMA)

    def append(self, ev: Event) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO events (participant_id, event_id, type, t, task_id, in_support, payload)"
                " VALUES (?,?,?,?,?,?,?)",
                (ev.participant_id, ev.event_id, ev.type, ev.t, ev.task_id, int(ev.in_support),
                 json.dumps(ev.payload, ensure_ascii=False)))

    def events(self, participant_id: str) -> list[Event]:
        with self._lock:
            rows = self._db.execute(
                "SELECT event_id, type, t, participant_id, task_id, payload, in_support FROM events"
                " WHERE participant_id=? ORDER BY seq", (participant_id,)).fetchall()
        return [Event(r[0], r[1], r[2], r[3], r[4], json.loads(r[5]), bool(r[6])) for r in rows]

    def add_participant(self, participant_id: str, token: str, group: str, t: float, note: str = "") -> None:
        with self._lock:
            self._db.execute("INSERT INTO participants VALUES (?,?,?,?,?)", (participant_id, token, group, t, note))

    def participants(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT participant_id, token, grp, created_t, note FROM participants ORDER BY created_t").fetchall()
        return [dict(participant_id=r[0], token=r[1], group=r[2], created_t=r[3], note=r[4]) for r in rows]

    def participant_by_token(self, token: str) -> Optional[dict[str, Any]]:
        return next((p for p in self.participants() if p["token"] == token), None)

    def close(self) -> None:
        self._db.close()
