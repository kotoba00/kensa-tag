"""内職依頼。誰に、どの分納を、いつまでに渡したか。

本番の正本は AppSheet の内職依頼。kensa-tag は「社外検査でかざしたのに依頼がまだ無い分納」を出し、
AppSheet の入力フォームではそれを選ぶだけにする（担当はグループで決まり、納期だけ入れる）。
デモでは同じ形の表を naishoku に持ち、/naishoku の画面から入れる。
"""

import sqlite3
from datetime import date, datetime

from .flow import flow
from .source import ProductionSource


class NaishokuError(ValueError):
    pass


def out_now(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """最後に読んだのが社外検査の分納。phase が start なら外にあり、end なら納品済み。"""
    if not flow().outsource:
        return []
    return conn.execute(
        """SELECT b.id, b.order_no, b.bunno, r.phase, r.read_at
           FROM bindings b
           JOIN reads r ON r.id = (SELECT id FROM reads WHERE binding_id = b.id
                                   ORDER BY read_at DESC, id DESC LIMIT 1)
           WHERE b.ended_at IS NULL AND b.bunno IS NOT NULL AND r.process = ?""",
        (flow().outsource,),
    ).fetchall()


def requests(conn: sqlite3.Connection) -> dict[tuple[str, int], sqlite3.Row]:
    return {(r["order_no"], r["bunno"]): r for r in conn.execute("SELECT * FROM naishoku")}


def pending(conn: sqlite3.Connection, source: ProductionSource) -> list[dict]:
    """社外検査で渡したのに、内職依頼がまだ無い分納。新しいものを上に。"""
    have = requests(conn)
    orders = {o.order_no: o for o in source.orders()}
    produced = {(lot.order_no, lot.bunno): lot.produced for lot in source.lots()}
    out = []
    for r in out_now(conn):
        key = (r["order_no"], r["bunno"])
        if r["phase"] != "start" or key in have:
            continue
        order = orders.get(r["order_no"])
        out.append(
            {
                "order_no": r["order_no"],
                "bunno": r["bunno"],
                "item_code": order.item_code if order else None,
                "qty": produced.get(key),
                "handed_at": r["read_at"],
            }
        )
    return sorted(out, key=lambda x: x["handed_at"], reverse=True)


def add(
    conn: sqlite3.Connection,
    source: ProductionSource,
    *,
    order_no: str,
    bunno: int,
    worker: str,
    due_date: date | None,
    qty: int | None,
    now: datetime | None = None,
) -> dict:
    if worker not in source.workers():
        raise NaishokuError("その内職さんは一覧にありません")
    try:
        with conn:
            conn.execute(
                """INSERT INTO naishoku (order_no, bunno, qty, worker, due_date, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (order_no, bunno, qty, worker, due_date.isoformat() if due_date else None,
                 (now or datetime.now().astimezone()).isoformat()),
            )
    except sqlite3.IntegrityError as e:
        raise NaishokuError(f"注文{order_no}の分納{bunno}は、もう依頼が入っています") from e
    return dict(requests(conn)[(order_no, bunno)])
