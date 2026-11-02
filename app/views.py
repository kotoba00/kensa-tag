"""履歴から現在の状態を計算する。"""

import sqlite3
from datetime import date, datetime

from .source import ProductionSource
from .detect import close_shipped


def _rows(conn: sqlite3.Connection, where: str = "", params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(
        f"""SELECT b.*,
                   (SELECT MIN(r.read_at) FROM reads r
                     WHERE r.binding_id = b.id AND r.location = 'inspection'
                       AND r.outcome = 'arrived') AS arrived_at
            FROM bindings b {where}""",
        params,
    ).fetchall()


def in_inspection(conn: sqlite3.Connection, source: ProductionSource, today: date) -> list[dict]:
    """検査が終わって検査場にあり、まだ出荷されていないもの。"""
    close_shipped(conn, source.shipments())
    orders = {o.order_no: o for o in source.orders()}
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
                "bunno_entered": bool(r["bunno_entered"]),
                "item_code": order.item_code if order else None,
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
    by_bunno = {b["bunno"]: b for b in bindings}
    numbers = sorted(set(by_bunno) | {s.bunno for s in shipments if s.bunno is not None})
    rows = []
    for n in numbers:
        b = by_bunno.get(n)
        shipped = [s for s in shipments if s.bunno == n and s.qty > 0]
        if shipped:
            place = "出荷済み"
        elif b and b["arrived_at"]:
            place = "検査場"
        elif b:
            place = "検査待ち"
        else:
            place = "不明"
        rows.append(
            {
                "bunno": n,
                "bunno_entered": bool(b["bunno_entered"]) if b else True,
                "place": place,
                "bound_at": b["started_at"] if b else None,
                "arrived_at": b["arrived_at"] if b else None,
                "shipped_on": shipped[0].ship_date.isoformat() if shipped else None,
                "qty": sum(s.qty for s in shipments if s.bunno == n),
            }
        )
    return {
        "order_no": order_no,
        "item_code": order.item_code if order else None,
        "customer": order.customer if order else None,
        "qty": order.qty if order else None,
        "due_date": order.due_date.isoformat() if order and order.due_date else None,
        "done": order.done if order else None,
        "bunnos": rows,
    }
