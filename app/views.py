"""履歴から現在の状態を計算する。"""

import sqlite3
from datetime import date, datetime

from .detect import close_shipped
from .flow import flow
from .source import ProductionSource


def _rows(conn: sqlite3.Connection, where: str = "", params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(
        f"""SELECT b.*,
                   (SELECT MIN(r.read_at) FROM reads r
                     WHERE r.binding_id = b.id AND r.process = ?
                       AND r.outcome = 'recorded') AS arrived_at
            FROM bindings b {where}""",
        (flow().checkpoint, *params),
    ).fetchall()


def _last_read(conn: sqlite3.Connection, binding_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT process, phase, read_at FROM reads WHERE binding_id = ?
           ORDER BY read_at DESC, id DESC LIMIT 1""",
        (binding_id,),
    ).fetchone()


def _produced(source: ProductionSource) -> dict[tuple[str, int], int]:
    # 数量は翌日の生産日報で埋まる。載っていなければ未定
    return {(lot.order_no, lot.bunno): lot.produced for lot in source.lots()}


def in_inspection(conn: sqlite3.Connection, source: ProductionSource, today: date) -> list[dict]:
    """見張っている工程にあり、まだ出荷されていないもの。"""
    close_shipped(conn, source.shipments())
    orders = {o.order_no: o for o in source.orders()}
    produced = _produced(source)
    out = []
    for r in _rows(conn, "WHERE b.ended_at IS NULL"):
        if not r["arrived_at"]:
            continue
        order = orders.get(r["order_no"])
        due = order.due_date if order else None
        out.append(
            {
                "order_no": r["order_no"],
                "bunno": r["bunno"],
                "item_code": order.item_code if order else None,
                "qty": produced.get((r["order_no"], r["bunno"])),
                "arrived_at": r["arrived_at"],
                "days": (today - datetime.fromisoformat(r["arrived_at"]).astimezone().date()).days,
                "due_date": due.isoformat() if due else None,
                "days_to_due": (due - today).days if due else None,
            }
        )
    return sorted(out, key=lambda x: (x["days_to_due"] is None, x["days_to_due"] or 0))


def order_detail(conn: sqlite3.Connection, source: ProductionSource, order_no: str) -> dict | None:
    close_shipped(conn, source.shipments())
    order = next((o for o in source.orders() if o.order_no == order_no), None)
    bindings = _rows(conn, "WHERE b.order_no = ?", (order_no,))
    if not order and not bindings:
        return None
    shipments = [s for s in source.shipments() if s.order_no == order_no]
    produced = _produced(source)
    by_bunno = {b["bunno"]: b for b in bindings if b["bunno"] is not None}
    numbers = sorted(set(by_bunno) | {s.bunno for s in shipments if s.bunno is not None})

    def row(bunno, b, shipped):
        last = _last_read(conn, b["id"]) if b else None
        if shipped:
            place = "出荷済み"
        elif last:
            place = flow().place(last["process"], last["phase"])
        else:
            place = "不明"
        return {
            "bunno": bunno,
            "place": place,
            "bound_at": b["started_at"] if b else None,
            "last_read_at": last["read_at"] if last else None,
            "arrived_at": b["arrived_at"] if b else None,
            "produced": produced.get((order_no, bunno)),
            "shipped_on": shipped[0].ship_date.isoformat() if shipped else None,
            "qty": sum(s.qty for s in shipments if s.bunno == bunno) if bunno is not None else 0,
        }

    rows = [
        row(n, by_bunno.get(n), [s for s in shipments if s.bunno == n and s.qty > 0])
        for n in numbers
    ]
    # エレベーターをまだ通っていないカード
    rows += [row(None, b, []) for b in bindings if b["bunno"] is None and b["ended_at"] is None]
    return {
        "order_no": order_no,
        "item_code": order.item_code if order else None,
        "customer": order.customer if order else None,
        "qty": order.qty if order else None,
        "due_date": order.due_date.isoformat() if order and order.due_date else None,
        "done": order.done if order else None,
        "bunnos": rows,
    }


def tag_detail(conn: sqlite3.Connection, source: ProductionSource, uid: str) -> dict | None:
    """カード1枚が今どの注文のどの分納か。使い終わったカードは最後の紐付けを出す。"""
    close_shipped(conn, source.shipments())
    rows = _rows(conn, "WHERE b.uid = ? ORDER BY b.ended_at IS NULL DESC, b.id DESC LIMIT 1", (uid,))
    if not rows:
        return None
    b = rows[0]
    order = next((o for o in source.orders() if o.order_no == b["order_no"]), None)
    last = _last_read(conn, b["id"])
    if b["end_reason"] == "shipped":
        place = "出荷済み"
    elif last:
        place = flow().place(last["process"], last["phase"])
    else:
        place = "不明"
    return {
        "uid": uid,
        "in_use": b["ended_at"] is None,
        "order_no": b["order_no"],
        "bunno": b["bunno"],
        "place": place,
        "bound_at": b["started_at"],
        "last_read_at": last["read_at"] if last else None,
        "produced": _produced(source).get((b["order_no"], b["bunno"])),
        "item_code": order.item_code if order else None,
        "customer": order.customer if order else None,
        "due_date": order.due_date.isoformat() if order and order.due_date else None,
    }


def board(conn: sqlite3.Connection, source: ProductionSource, today: date, anomalies: list, triage: dict | None) -> list[dict]:
    """工場図に置く分納。使用中のカード1枚につき1つ、最後に読んだ工程に置く。"""
    orders = {o.order_no: o for o in source.orders()}
    produced = _produced(source)
    priority = {i["anomaly_id"]: i for i in (triage or {}).get("items", [])}
    # 同じ注文と分納に付いた検出と、Claude の見立て
    found: dict[tuple[str, int | None], list[dict]] = {}
    for a in anomalies:
        ai = priority.get(a.id, {})
        found.setdefault((a.order_no, a.bunno), []).append(
            {"kind": a.kind, "title": a.title, "priority": ai.get("priority"),
             "likely_cause": ai.get("likely_cause"), "next_action": ai.get("next_action"), "ask_role": ai.get("ask_role")}
        )
    out = []
    for b in _rows(conn, "WHERE b.ended_at IS NULL"):
        last = _last_read(conn, b["id"])
        order = orders.get(b["order_no"])
        notes = found.get((b["order_no"], b["bunno"]), [])
        if any(n["priority"] == "今日中" for n in notes):
            level = "urgent"
        elif notes:
            level = "warn"
        else:
            level = "ok"
        read_at = last["read_at"] if last else b["started_at"]
        out.append(
            {
                "id": b["id"],
                "order_no": b["order_no"],
                "bunno": b["bunno"],
                "process": last["process"] if last else None,
                "phase": last["phase"] if last else None,
                "place": flow().place(last["process"], last["phase"]) if last else "不明",
                "last_read_at": read_at,
                "days": (today - datetime.fromisoformat(read_at).astimezone().date()).days,
                "item_code": order.item_code if order else None,
                "due_date": order.due_date.isoformat() if order and order.due_date else None,
                "qty": produced.get((b["order_no"], b["bunno"])),
                "level": level,
                "notes": notes,
            }
        )
    return out
