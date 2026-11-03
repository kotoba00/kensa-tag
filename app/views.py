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
    # エレベーター前をまだ通っていないカード
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
