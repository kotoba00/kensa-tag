"""読み取りを受けて履歴を残す。分納番号は紐付けのときに1回だけ振る。"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from .scan import classify

PENDING_TTL = timedelta(seconds=60)
DEBOUNCE = timedelta(seconds=10)
INSPECTION = "inspection"


@dataclass
class ScanResult:
    outcome: str
    message: str
    order_no: str | None = None
    bunno: int | None = None
    replayed: bool = False


def handle_scan(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    raw: str,
    device_id: str,
    read_at: datetime,
    location: str = INSPECTION,
) -> ScanResult:
    with conn:
        stored = conn.execute(
            "SELECT outcome FROM reads WHERE event_id = ?", (event_id,)
        ).fetchone()
        if stored:
            return ScanResult(stored["outcome"], "送り直しのため記録済みです", replayed=True)

        kind, value = classify(raw)
        if kind == "order":
            result, binding_id = _remember_order(conn, device_id, value, read_at)
        elif kind == "bunno":
            result, binding_id = _remember_bunno(conn, device_id, int(value), read_at)
        elif kind in ("ntag", "uhf", "hf10"):
            result, binding_id = _on_tag(conn, device_id, value, location, read_at)
        else:
            result, binding_id = ScanResult("unknown", f"読み取れない形式です: {value}"), None

        conn.execute(
            """INSERT INTO reads (event_id, read_at, location, device_id, kind, value, binding_id, outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, read_at.isoformat(), location, device_id, kind, value, binding_id, result.outcome),
        )
    return result


def _remember_order(conn, device_id, order_no, read_at):
    conn.execute(
        """INSERT INTO pending_orders (device_id, order_no, bunno, read_at) VALUES (?, ?, NULL, ?)
           ON CONFLICT (device_id) DO UPDATE SET
             order_no = excluded.order_no, bunno = NULL, read_at = excluded.read_at""",
        (device_id, order_no, read_at.isoformat()),
    )
    return ScanResult("order_set", "分納番号を打ってカードをかざしてください", order_no=order_no), None


def _fresh_pending(conn, device_id, read_at):
    pending = conn.execute(
        "SELECT order_no, bunno, read_at FROM pending_orders WHERE device_id = ?", (device_id,)
    ).fetchone()
    if not pending:
        return None
    age = read_at - datetime.fromisoformat(pending["read_at"])
    return pending if timedelta(0) <= age <= PENDING_TTL else None


def _remember_bunno(conn, device_id, bunno, read_at):
    pending = _fresh_pending(conn, device_id, read_at)
    if not pending:
        return ScanResult("no_order", "先に記録票の QR を読んでください"), None
    conn.execute(
        "UPDATE pending_orders SET bunno = ?, read_at = ? WHERE device_id = ?",
        (bunno, read_at.isoformat(), device_id),
    )
    return (
        ScanResult("order_set", "カードをかざしてください", order_no=pending["order_no"], bunno=bunno),
        None,
    )


def _on_tag(conn, device_id, uid, location, read_at):
    pending = _fresh_pending(conn, device_id, read_at)
    conn.execute("DELETE FROM pending_orders WHERE device_id = ?", (device_id,))
    if pending:
        return _bind(conn, uid, pending["order_no"], pending["bunno"], read_at)

    binding = conn.execute(
        "SELECT * FROM bindings WHERE uid = ? AND ended_at IS NULL", (uid,)
    ).fetchone()
    if not binding:
        return ScanResult("unbound", "このカードは注文に紐付いていません"), None

    history = conn.execute(
        "SELECT read_at, outcome FROM reads WHERE binding_id = ? AND value = ? ORDER BY read_at DESC",
        (binding["id"], uid),
    ).fetchall()
    common = {"order_no": binding["order_no"], "bunno": binding["bunno"]}
    # 紐付けの直後も間引く。かざしたままのカードを検査場入りと数えない
    if history and read_at - datetime.fromisoformat(history[0]["read_at"]) < DEBOUNCE:
        return ScanResult("debounced", "さっき読み取り済みです", **common), binding["id"]
    if any(h["outcome"] == "arrived" for h in history):
        return ScanResult("already_in", "検査場に入り済みです", **common), binding["id"]
    return ScanResult("arrived", "検査場に入りました", **common), binding["id"]


def _bind(conn, uid, order_no, entered, read_at):
    current = conn.execute(
        "SELECT * FROM bindings WHERE uid = ? AND ended_at IS NULL", (uid,)
    ).fetchone()
    if current and current["order_no"] == order_no and entered in (None, current["bunno"]):
        return (
            ScanResult("already_bound", "紐付け済みです", order_no=order_no, bunno=current["bunno"]),
            current["id"],
        )
    if entered is not None:
        taken = conn.execute(
            "SELECT 1 FROM bindings WHERE order_no = ? AND bunno = ? AND ended_at IS NULL",
            (order_no, entered),
        ).fetchone()
        if taken:
            return (
                ScanResult("bunno_taken", "その分納は別のカードに紐付いています", order_no=order_no, bunno=entered),
                None,
            )
    if current:
        conn.execute(
            "UPDATE bindings SET ended_at = ?, end_reason = 'rebound' WHERE id = ?",
            (read_at.isoformat(), current["id"]),
        )
    bunno = entered or conn.execute(
        "SELECT COALESCE(MAX(bunno), 0) + 1 AS n FROM bindings WHERE order_no = ?", (order_no,)
    ).fetchone()["n"]
    cur = conn.execute(
        "INSERT INTO bindings (uid, order_no, bunno, bunno_entered, started_at) VALUES (?, ?, ?, ?, ?)",
        (uid, order_no, bunno, int(entered is not None), read_at.isoformat()),
    )
    message = "紐付けました" if entered is not None else "紐付けました（分納番号は仮です）"
    return ScanResult("bound", message, order_no=order_no, bunno=bunno), cur.lastrowid
