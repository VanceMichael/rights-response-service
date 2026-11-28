"""SQLite 持久化。

设计要点：
- 事件表 events 只追加，历史处置与文件更正均留痕，不回写改写。
- 各业务表保留当前状态便于查询；状态机的权威轨迹由 events 与各流水表共同表达。
- 所有写入在同一连接与锁内串行完成（ThreadingHTTPServer 下足够演练使用）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS users(
    id TEXT PRIMARY KEY,
    role TEXT NOT NULL CHECK(role IN ('agent','legal','liaison','reviewer','uploader','admin')),
    display_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS works(
    id TEXT PRIMARY KEY,
    duration_ms INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rights_records(
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(id),
    owner TEXT NOT NULL,
    regions TEXT NOT NULL,
    media TEXT NOT NULL,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mandates(
    id TEXT PRIMARY KEY,
    rights_id TEXT NOT NULL REFERENCES rights_records(id),
    agent_id TEXT NOT NULL REFERENCES users(id),
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS platforms(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    capabilities TEXT NOT NULL,
    replay_base_url TEXT
);

CREATE TABLE IF NOT EXISTS publications(
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(id),
    platform_id TEXT NOT NULL REFERENCES platforms(id),
    link TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    range_ms TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE(platform_id, link, content_sha256)
);

CREATE TABLE IF NOT EXISTS calendars(
    code TEXT PRIMARY KEY,
    timezone TEXT NOT NULL,
    holidays TEXT NOT NULL,
    make_up_days TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases(
    id TEXT PRIMARY KEY,
    work_id TEXT NOT NULL REFERENCES works(id),
    title TEXT NOT NULL,
    calendar_code TEXT NOT NULL REFERENCES calendars(code),
    created_by TEXT NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notices(
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES cases(id),
    publication_id TEXT NOT NULL REFERENCES publications(id),
    mandate_id TEXT NOT NULL REFERENCES mandates(id),
    submitter_id TEXT NOT NULL REFERENCES users(id),
    scope_json TEXT NOT NULL,
    status TEXT NOT NULL,
    materials_json TEXT NOT NULL,
    conflict_pending INTEGER NOT NULL DEFAULT 0,
    conflict_note TEXT,
    withdrawn_at TEXT,
    current_decision_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notice_supplements(
    id TEXT PRIMARY KEY,
    notice_id TEXT NOT NULL REFERENCES notices(id),
    materials_json TEXT NOT NULL,
    note TEXT,
    submitted_by TEXT NOT NULL REFERENCES users(id),
    submitted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS legal_decisions(
    id TEXT PRIMARY KEY,
    notice_id TEXT NOT NULL REFERENCES notices(id),
    decision TEXT NOT NULL CHECK(decision IN ('approved','rejected','conflict_resolved','returned')),
    scope_json TEXT,
    conflict_ruling TEXT,
    decided_by TEXT NOT NULL REFERENCES users(id),
    decided_at TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS disputes(
    id TEXT PRIMARY KEY,
    notice_id TEXT NOT NULL REFERENCES notices(id),
    uploader_id TEXT NOT NULL REFERENCES users(id),
    scope_json TEXT NOT NULL,
    license_sha256 TEXT,
    status TEXT NOT NULL,
    received_at TEXT NOT NULL,
    calendar_code TEXT NOT NULL REFERENCES calendars(code),
    validity_due_at TEXT,
    validity_note TEXT,
    validity_checked_by TEXT,
    valid_served_at TEXT,
    lapse_due_at TEXT,
    lapse_at TEXT,
    created_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE TABLE IF NOT EXISTS dispute_supplements(
    id TEXT PRIMARY KEY,
    dispute_id TEXT NOT NULL REFERENCES disputes(id),
    materials_json TEXT NOT NULL,
    missing_items TEXT,
    submitted_by TEXT NOT NULL REFERENCES users(id),
    submitted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dispute_services(
    id TEXT PRIMARY KEY,
    dispute_id TEXT NOT NULL REFERENCES disputes(id),
    recipient_id TEXT NOT NULL REFERENCES users(id),
    served_at TEXT,
    attempted_at TEXT NOT NULL,
    ok INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS review_decisions(
    id TEXT PRIMARY KEY,
    dispute_id TEXT NOT NULL REFERENCES disputes(id),
    decision TEXT NOT NULL CHECK(decision IN ('maintain','suspend','lift','restore','lapsed')),
    scope_json TEXT,
    decided_by TEXT NOT NULL REFERENCES users(id),
    decided_at TEXT NOT NULL,
    material_version TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS continuations(
    id TEXT PRIMARY KEY,
    dispute_id TEXT NOT NULL REFERENCES disputes(id),
    materials_json TEXT NOT NULL,
    submitted_by TEXT NOT NULL REFERENCES users(id),
    received_at TEXT NOT NULL,
    extends_lapse_due_to TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries(
    id TEXT PRIMARY KEY,
    notice_id TEXT NOT NULL REFERENCES notices(id),
    publication_id TEXT NOT NULL REFERENCES publications(id),
    platform_id TEXT NOT NULL REFERENCES platforms(id),
    command_id TEXT,
    direction TEXT NOT NULL CHECK(direction IN ('restrict','restore')),
    scope_json TEXT NOT NULL,
    state TEXT NOT NULL,
    basis_active_at_request INTEGER NOT NULL,
    requested_by TEXT NOT NULL REFERENCES users(id),
    requested_at TEXT NOT NULL,
    last_observed_at TEXT,
    last_revision INTEGER,
    note TEXT
);

CREATE TABLE IF NOT EXISTS delivery_events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id TEXT NOT NULL REFERENCES deliveries(id),
    observed_state TEXT NOT NULL,
    native_state TEXT,
    platform_revision INTEGER,
    observed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    note TEXT,
    basis_active INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    actor_id TEXT,
    at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_type, entity_id, id);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.commit()
            self.conn.close()

    # --- 基础工具 -------------------------------------------------------
    def begin(self) -> threading.RLock:
        """返回业务锁；HTTP 每个请求在锁内完成读改写。"""
        return self._lock

    def execute(self, sql: str, params=()):
        cur = self.conn.execute(sql, params)
        return cur

    def commit(self) -> None:
        self.conn.commit()

    def get(self, sql: str, params=()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params=()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def event(
        self,
        event_type: str,
        entity_type: str,
        entity_id: str,
        actor_id: str | None,
        at: str,
        payload: dict,
    ) -> None:
        self.conn.execute(
            "INSERT INTO events(event_type,entity_type,entity_id,actor_id,at,payload_json)"
            " VALUES(?,?,?,?,?,?)",
            (
                event_type,
                entity_type,
                entity_id,
                actor_id,
                at,
                json.dumps(payload, ensure_ascii=False),
            ),
        )

    def history(self, entity_type: str, entity_id: str) -> list[dict]:
        rows = self.all(
            "SELECT * FROM events WHERE entity_type=? AND entity_id=? ORDER BY id",
            (entity_type, entity_id),
        )
        out = []
        for r in rows:
            item = dict(r)
            item["payload"] = json.loads(item.pop("payload_json"))
            out.append(item)
        return out
