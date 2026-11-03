"""タグの履歴と生産管理データを突き合わせて、確認が要るものを決まった手順で拾う。

見つけることはここでやり、Claude は triage.py で原因と優先順位を考える。
"""

import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time

from .flow import flow
from .source import Order, ProductionSource, Shipment

STAGNANT_DAYS = 3
QTY_TOLERANCE = 0.02  # 計量の誤差として許す割合。現場の値が決まったら直す


@dataclass
class Anomaly:
    id: str
    kind: str  # forgotten / stagnant / qty_mismatch / bunno_total
    order_no: str
    bunno: int | None
    title: str
    evidence: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


def close_shipped(conn: sqlite3.Connection, shipments: list[Shipment]) -> int:
    """連絡票に載った分納の紐付けを閉じ、カードを使い回せるようにする。"""
    closed = 0
    with conn:
        for s in shipments:
            if s.bunno is None or s.qty <= 0:
                continue
            ended = datetime.combine(s.ship_date, time(23, 59, 59)).astimezone().isoformat()
            closed += conn.execute(
                """UPDATE bindings SET ended_at = ?, end_reason = 'shipped'
                   WHERE order_no = ? AND bunno = ? AND ended_at IS NULL""",
                (ended, s.order_no, s.bunno),
            ).rowcount
    return closed


def _arrivals(conn: sqlite3.Connection) -> dict[tuple[str, int], str]:
    rows = conn.execute(
        """SELECT b.order_no, b.bunno, MIN(r.read_at) AS first_read
           FROM reads r JOIN bindings b ON b.id = r.binding_id
           WHERE r.process = ? AND r.outcome = 'recorded'
           GROUP BY b.order_no, b.bunno""",
        (flow().checkpoint,),
    ).fetchall()
    return {(r["order_no"], r["bunno"]): r["first_read"] for r in rows}


def _who(order_no: str, bunno: int | None) -> str:
    return f"注文{order_no}の分納{bunno}" if bunno is not None else f"注文{order_no}の分納番号が未定の分"


def _tracking_start(conn: sqlite3.Connection) -> date | None:
    row = conn.execute("SELECT MIN(started_at) AS s FROM bindings").fetchone()
    return datetime.fromisoformat(row["s"]).astimezone().date() if row["s"] else None


def detect(conn: sqlite3.Connection, source: ProductionSource, today: date) -> list[Anomaly]:
    orders = {o.order_no: o for o in source.orders()}
    shipments = source.shipments()
    close_shipped(conn, shipments)
    arrivals = _arrivals(conn)
    found: list[Anomaly] = []
    found += _forgotten(conn, shipments, arrivals, orders)
    found += _stagnant(conn, shipments, arrivals, orders, today)
    found += _qty_mismatch(source, shipments)
    found += _bunno_total(shipments, orders)
    return found


def _forgotten(conn, shipments, arrivals, orders) -> list[Anomaly]:
    start = _tracking_start(conn)
    if start is None:
        return []
    out = []
    seen = set()
    for s in shipments:
        # タグを使い始める前の出荷は、読み取りが無くて当たり前
        if s.ship_date < start or s.qty <= 0 or s.status == "返品" or s.bunno is None:
            continue
        key = (s.order_no, s.bunno)
        if key in arrivals or key in seen:
            continue
        seen.add(key)
        order = orders.get(s.order_no)
        out.append(
            Anomaly(
                id=f"forgotten:{s.order_no}:{s.bunno}",
                kind="forgotten",
                order_no=s.order_no,
                bunno=s.bunno,
                title=f"{_who(s.order_no, s.bunno)}は出荷データに載っていますが、{flow().checkpoint_name}で読まれていません。",
                evidence={
                    "ship_date": s.ship_date.isoformat(),
                    "qty": s.qty,
                    "status": s.status,
                    "item_code": order.item_code if order else None,
                },
            )
        )
    return out


def _stagnant(conn, shipments, arrivals, orders, today) -> list[Anomaly]:
    shipped = {(s.order_no, s.bunno) for s in shipments if s.qty > 0}
    out = []
    for (order_no, bunno), first_read in arrivals.items():
        order = orders.get(order_no)
        if (order_no, bunno) in shipped or (order and order.stock):
            continue
        days = (today - datetime.fromisoformat(first_read).astimezone().date()).days
        if days < STAGNANT_DAYS:
            continue
        due = order.due_date if order else None
        out.append(
            Anomaly(
                id=f"stagnant:{order_no}:{bunno}",
                kind="stagnant",
                order_no=order_no,
                bunno=bunno,
                title=f"{_who(order_no, bunno)}が、{flow().checkpoint_name}で読まれてから{days}日経っていますが、出荷データに載っていません。",
                evidence={
                    "arrived_at": first_read,
                    "days": days,
                    "due_date": due.isoformat() if due else None,
                    "days_to_due": (due - today).days if due else None,
                    "customer": order.customer if order else None,
                    "item_code": order.item_code if order else None,
                },
            )
        )
    return out


def _qty_mismatch(source: ProductionSource, shipments) -> list[Anomaly]:
    packed: dict[tuple[str, int], int] = defaultdict(int)
    for s in shipments:
        if s.bunno is not None:
            packed[(s.order_no, s.bunno)] += s.qty
    out = []
    for lot in source.lots():
        key = (lot.order_no, lot.bunno)
        if key not in packed:
            continue
        diff = packed[key] - lot.produced
        if lot.produced and abs(diff) / lot.produced > QTY_TOLERANCE:
            out.append(
                Anomaly(
                    id=f"qty:{lot.order_no}:{lot.bunno}",
                    kind="qty_mismatch",
                    order_no=lot.order_no,
                    bunno=lot.bunno,
                    title=f"{_who(lot.order_no, lot.bunno)}は、生産数{lot.produced}個に対して包装の計量が{packed[key]}個で、{diff:+d}個違います。",
                    evidence={"produced": lot.produced, "packed": packed[key], "diff": diff},
                )
            )
    return out


def _bunno_total(shipments, orders: dict[str, Order]) -> list[Anomaly]:
    net: dict[str, int] = defaultdict(int)
    statuses: dict[str, set[str]] = defaultdict(set)
    for s in shipments:
        net[s.order_no] += s.qty  # 赤伝のマイナスも足す
        statuses[s.order_no].add(s.status)
    out = []
    for order_no, total in net.items():
        order = orders.get(order_no)
        # 完納の判断は注残の「済」に従う。在庫向けと注文数0は比べられない
        if not order or not order.done or order.stock or order.qty == 0 or total == order.qty:
            continue
        out.append(
            Anomaly(
                id=f"total:{order_no}",
                kind="bunno_total",
                order_no=order_no,
                bunno=None,
                title=f"注文{order_no}は完納ですが、分納の合計{total}個が注文数{order.qty}個と{total - order.qty:+d}個違います。",
                evidence={
                    "ordered": order.qty,
                    "shipped_net": total,
                    "statuses": sorted(statuses[order_no]),
                },
            )
        )
    return out
