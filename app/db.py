"""履歴の保存先。現在の状態は持たず、reads と bindings から計算する。"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS reads (
    id INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    read_at TEXT NOT NULL,
    location TEXT NOT NULL,
    device_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    binding_id INTEGER REFERENCES bindings(id),
    outcome TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reads_binding ON reads(binding_id, read_at);
CREATE INDEX IF NOT EXISTS reads_value ON reads(value, location, read_at);

CREATE TABLE IF NOT EXISTS bindings (
    id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL,
    order_no TEXT NOT NULL,
    bunno INTEGER NOT NULL,
    bunno_entered INTEGER NOT NULL,  -- 0 は読み取り順の仮番号
    started_at TEXT NOT NULL,
    ended_at TEXT,
    end_reason TEXT
);
-- 同じカード、同じ分納の有効な紐付けは1つだけ
CREATE UNIQUE INDEX IF NOT EXISTS bindings_open_uid ON bindings(uid) WHERE ended_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS bindings_open_bunno ON bindings(order_no, bunno) WHERE ended_at IS NULL;

CREATE TABLE IF NOT EXISTS pending_orders (
    device_id TEXT PRIMARY KEY,
    order_no TEXT NOT NULL,
    bunno INTEGER,
    read_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS triage_runs (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    model TEXT NOT NULL,
    report_json TEXT NOT NULL
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    return conn
