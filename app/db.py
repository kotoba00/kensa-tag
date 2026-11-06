"""履歴の保存先。現在の状態は持たず、reads と bindings から計算する。"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS reads (
    id INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    read_at TEXT NOT NULL,
    process TEXT NOT NULL,
    phase TEXT NOT NULL,  -- point / start / end
    device_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    binding_id INTEGER REFERENCES bindings(id),
    outcome TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS reads_binding ON reads(binding_id, read_at);
CREATE INDEX IF NOT EXISTS reads_value ON reads(value, process, read_at);

CREATE TABLE IF NOT EXISTS bindings (
    id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL,
    order_no TEXT NOT NULL,
    bunno INTEGER,  -- エレベーターを通るまでは空
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
    read_at TEXT NOT NULL
);

-- 内職依頼。本番の正本は AppSheet で、ここは同じ形の写し（デモでは /naishoku から入れる）
CREATE TABLE IF NOT EXISTS naishoku (
    id INTEGER PRIMARY KEY,
    order_no TEXT NOT NULL,
    bunno INTEGER NOT NULL,
    qty INTEGER,
    worker TEXT NOT NULL,
    due_date TEXT,  -- 内職さんへの納期
    created_at TEXT NOT NULL,
    progress TEXT NOT NULL DEFAULT '未着手',  -- 内職さんが選ぶ進捗。未着手 / ちぎり済 / 検査済
    progress_at TEXT,  -- 進捗を最後に変えた時刻
    UNIQUE (order_no, bunno)
);

-- 内職さんの専用ページの鍵。URL に入れて渡し、ログインの代わりにする
CREATE TABLE IF NOT EXISTS worker_keys (
    worker TEXT PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS triage_runs (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    model TEXT NOT NULL,
    report_json TEXT NOT NULL,
    signature TEXT  -- 見立てを作ったときの検出結果の指紋
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
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(triage_runs)")}
    if "signature" not in columns:
        conn.execute("ALTER TABLE triage_runs ADD COLUMN signature TEXT")
    if "progress" not in {r["name"] for r in conn.execute("PRAGMA table_info(naishoku)")}:
        conn.execute("ALTER TABLE naishoku ADD COLUMN progress TEXT NOT NULL DEFAULT '未着手'")
        conn.execute("ALTER TABLE naishoku ADD COLUMN progress_at TEXT")
    return conn
