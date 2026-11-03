"""読み取りを受けて履歴を残す。分納番号はエレベーター前で1回だけ振る。"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from .flow import PHASE_LABEL, Process, flow
from .scan import classify

PENDING_TTL = timedelta(seconds=60)
DEBOUNCE = timedelta(seconds=10)
# 使用中のカードの付け替えは、警告のあと置き直して確かめる。読み取り機の二度読み（2秒ほど）は確定にしない
CONFIRM_AFTER = timedelta(seconds=3)
POINT = "point"


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
    process: str,
    phase: str = POINT,
) -> ScanResult:
    with conn:
        stored = conn.execute(
            "SELECT outcome FROM reads WHERE event_id = ?", (event_id,)
        ).fetchone()
        if stored:
            return ScanResult(stored["outcome"], "送り直しのため記録済みです", replayed=True)

        kind, value = classify(raw)
        proc = flow().get(process)
        binding_id = None
        if proc is None or phase not in proc.phases:
            # 送信待ちの読み取りを止めないよう、エラーにせず記録して知らせる
            result = ScanResult("unknown_process", f"工程の設定にありません: {process} {phase}")
        elif kind == "order":
            result = _on_order(conn, proc, device_id, value, read_at)
        elif kind in ("ntag", "uhf", "hf"):
            result, binding_id = _on_tag(conn, proc, phase, device_id, value, read_at)
        else:
            result = ScanResult("unknown", f"読み取れない形式です: {value}")

        conn.execute(
            """INSERT INTO reads (event_id, read_at, process, phase, device_id, kind, value, binding_id, outcome)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (event_id, read_at.isoformat(), process, phase, device_id, kind, value, binding_id, result.outcome),
        )
    return result


def _on_order(conn, proc: Process, device_id, order_no, read_at) -> ScanResult:
    if proc.action != "bind":
        return ScanResult("wrong_process", f"{proc.name}では注文番号を使いません。カードをかざしてください", order_no=order_no)
    conn.execute(
        """INSERT INTO pending_orders (device_id, order_no, read_at) VALUES (?, ?, ?)
           ON CONFLICT (device_id) DO UPDATE SET
             order_no = excluded.order_no, read_at = excluded.read_at""",
        (device_id, order_no, read_at.isoformat()),
    )
    return ScanResult("order_set", "カードをかざしてください", order_no=order_no)


def _take_pending(conn, device_id, read_at) -> str | None:
    pending = conn.execute(
        "SELECT order_no, read_at FROM pending_orders WHERE device_id = ?", (device_id,)
    ).fetchone()
    conn.execute("DELETE FROM pending_orders WHERE device_id = ?", (device_id,))
    if not pending:
        return None
    age = read_at - datetime.fromisoformat(pending["read_at"])
    return pending["order_no"] if timedelta(0) <= age <= PENDING_TTL else None


def _on_tag(conn, proc: Process, phase, device_id, uid, read_at):
    if proc.action == "bind":
        order_no = _take_pending(conn, device_id, read_at)
        if order_no:
            return _bind(conn, uid, order_no, device_id, read_at)

    binding = conn.execute(
        "SELECT * FROM bindings WHERE uid = ? AND ended_at IS NULL", (uid,)
    ).fetchone()
    if not binding:
        if proc.action == "bind":
            return ScanResult("no_order", "先に記録票の QR を読んでください"), None
        return ScanResult("unbound", "このカードは注文に紐付いていません"), None

    # 同じ工程の読み直しだけを間引く。工程を移ったら、すぐ読んでも別の記録
    history = conn.execute(
        """SELECT read_at, outcome FROM reads
           WHERE binding_id = ? AND value = ? AND process = ? AND phase = ?
           ORDER BY read_at DESC""",
        (binding["id"], uid, proc.code, phase),
    ).fetchall()
    common = {"order_no": binding["order_no"], "bunno": binding["bunno"]}
    if history and read_at - datetime.fromisoformat(history[0]["read_at"]) < DEBOUNCE:
        return ScanResult("debounced", "さっき読み取り済みです", **common), binding["id"]

    if proc.action == "bind":
        return ScanResult("already_bound", "紐付け済みです", **common), binding["id"]
    if proc.action == "assign":
        return _assign(conn, binding), binding["id"]
    if any(h["outcome"] == "recorded" for h in history):
        return ScanResult("already_recorded", f"{_label(proc, phase)}は記録済みです", **common), binding["id"]
    return ScanResult("recorded", _recorded_message(proc, phase), **common), binding["id"]


def _label(proc: Process, phase) -> str:
    return f"{proc.name}の{PHASE_LABEL[phase]}" if phase in PHASE_LABEL else proc.name


def _recorded_message(proc: Process, phase) -> str:
    if phase in PHASE_LABEL:
        return f"{proc.name}を{PHASE_LABEL[phase]}しました"
    return f"{proc.name}に入りました" if proc.code == flow().checkpoint else f"{proc.name}を通りました"


def _assign(conn, binding) -> ScanResult:
    order_no = binding["order_no"]
    if binding["bunno"] is not None:
        return ScanResult("already_assigned", f"分納番号は {binding['bunno']} 番です", order_no=order_no, bunno=binding["bunno"])
    # 閉じた紐付けも数える。番号は使い回さない
    bunno = conn.execute(
        "SELECT COALESCE(MAX(bunno), 0) + 1 AS n FROM bindings WHERE order_no = ?", (order_no,)
    ).fetchone()["n"]
    conn.execute("UPDATE bindings SET bunno = ? WHERE id = ?", (bunno, binding["id"]))
    return ScanResult("assigned", f"分納番号は {bunno} 番です", order_no=order_no, bunno=bunno)


def _bind(conn, uid, order_no, device_id, read_at):
    current = conn.execute(
        "SELECT * FROM bindings WHERE uid = ? AND ended_at IS NULL", (uid,)
    ).fetchone()
    if current and current["order_no"] == order_no:
        return (
            ScanResult("already_bound", "紐付け済みです", order_no=order_no, bunno=current["bunno"]),
            current["id"],
        )
    if current and not _confirmed(conn, uid, device_id, current["id"], read_at):
        # 製品に付いたままのカードかもしれない。黙って外さず、注文番号は残してもう一度待つ
        conn.execute(
            "INSERT INTO pending_orders (device_id, order_no, read_at) VALUES (?, ?, ?)",
            (device_id, order_no, read_at.isoformat()),
        )
        where = f"注文{current['order_no']}の分納{current['bunno']}" if current["bunno"] else f"注文{current['order_no']}"
        return (
            ScanResult("in_use", f"{where}で使用中です。付け替えるなら、少し置いてもう一度かざしてください",
                       order_no=current["order_no"], bunno=current["bunno"]),
            current["id"],
        )
    if current:
        conn.execute(
            "UPDATE bindings SET ended_at = ?, end_reason = 'rebound' WHERE id = ?",
            (read_at.isoformat(), current["id"]),
        )
    cur = conn.execute(
        "INSERT INTO bindings (uid, order_no, started_at) VALUES (?, ?, ?)",
        (uid, order_no, read_at.isoformat()),
    )
    return ScanResult("bound", "紐付けました", order_no=order_no), cur.lastrowid


def _confirmed(conn, uid, device_id, binding_id, read_at) -> bool:
    """同じ端末で、同じカードの使用中の警告を少し前に出していれば、付け替えてよい。"""
    warned = conn.execute(
        """SELECT read_at FROM reads WHERE value = ? AND device_id = ? AND binding_id = ? AND outcome = 'in_use'
           ORDER BY read_at DESC LIMIT 1""",
        (uid, device_id, binding_id),
    ).fetchone()
    if not warned:
        return False
    age = read_at - datetime.fromisoformat(warned["read_at"])
    return CONFIRM_AFTER <= age <= PENDING_TTL
