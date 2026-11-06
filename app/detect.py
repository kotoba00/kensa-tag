"""タグの履歴と生産管理データを突き合わせて、確認が要るものを決まった手順で拾う。

見つけることはここでやり、Claude は triage.py で原因と優先順位を考える。
"""

import sqlite3
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time

from .flow import flow
from .naishoku import FINISHED, out_now, requests
from .source import Order, ProductionSource, Shipment

STAGNANT_DAYS = 3
PICKUP_DAYS = 2  # 内職さんが終わってから、引き取りを待つ日数
QTY_TOLERANCE = 0.02  # 計量の誤差として許す割合。現場の値が決まったら直す


@dataclass
class Anomaly:
    id: str
    kind: str  # forgotten / stagnant / qty_mismatch / bunno_total / unrequested / overdue_return / pickup_waiting / request_unscanned
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


def _inspected(conn: sqlite3.Connection) -> dict[tuple[str, int], str]:
    """検査（社内か社外）が初めて終わった時刻。"""
    cond, params = flow().inspected()
    rows = conn.execute(
        f"""SELECT b.order_no, b.bunno, MIN(r.read_at) AS done_at
            FROM reads r JOIN bindings b ON b.id = r.binding_id
            WHERE {cond} AND r.outcome = 'recorded'
            GROUP BY b.order_no, b.bunno""",
        params,
    ).fetchall()
    return {(r["order_no"], r["bunno"]): r["done_at"] for r in rows}


def _who(order_no: str, bunno: int | None) -> str:
    return f"注文{order_no}の分納{bunno}" if bunno is not None else f"注文{order_no}の分納番号が未定の分"


def _tracking_start(conn: sqlite3.Connection) -> date | None:
    row = conn.execute("SELECT MIN(started_at) AS s FROM bindings").fetchone()
    return datetime.fromisoformat(row["s"]).astimezone().date() if row["s"] else None


def detect(conn: sqlite3.Connection, source: ProductionSource, today: date) -> list[Anomaly]:
    orders = {o.order_no: o for o in source.orders()}
    shipments = source.shipments()
    close_shipped(conn, shipments)
    inspected = _inspected(conn)
    found: list[Anomaly] = []
    found += _forgotten(conn, shipments, inspected, orders)
    found += _stagnant(conn, shipments, orders, today)
    found += _qty_mismatch(source, shipments)
    found += _bunno_total(shipments, orders)
    found += _naishoku(conn, orders, today)
    return found


def _forgotten(conn, shipments, inspected, orders) -> list[Anomaly]:
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
        if key in inspected or key in seen:
            continue
        seen.add(key)
        order = orders.get(s.order_no)
        out.append(
            Anomaly(
                id=f"forgotten:{s.order_no}:{s.bunno}",
                kind="forgotten",
                order_no=s.order_no,
                bunno=s.bunno,
                title=f"{_who(s.order_no, s.bunno)}は出荷データに載っていますが、検査（社内か社外）の終了が記録されていません。",
                evidence={
                    "ship_date": s.ship_date.isoformat(),
                    "qty": s.qty,
                    "status": s.status,
                    "item_code": order.item_code if order else None,
                },
            )
        )
    return out


def _stagnant(conn, shipments, orders, today) -> list[Anomaly]:
    """検査が終わったのに、次の工程（出荷検査）に進まないもの。"""
    shipped = {(s.order_no, s.bunno) for s in shipments if s.qty > 0}
    cond, params = flow().inspected("r")
    # 最後に読んだのが検査の終わりのまま、という分納
    rows = conn.execute(
        f"""SELECT b.order_no, b.bunno, r.process, r.read_at
            FROM bindings b
            JOIN reads r ON r.id = (SELECT id FROM reads WHERE binding_id = b.id AND outcome = 'recorded'
                                    ORDER BY read_at DESC, id DESC LIMIT 1)
            WHERE b.ended_at IS NULL AND b.bunno IS NOT NULL AND {cond}""",
        params,
    ).fetchall()
    out = []
    for r in rows:
        order_no, bunno = r["order_no"], r["bunno"]
        order = orders.get(order_no)
        if (order_no, bunno) in shipped or (order and order.stock):
            continue
        days = (today - datetime.fromisoformat(r["read_at"]).astimezone().date()).days
        if days < STAGNANT_DAYS:
            continue
        due = order.due_date if order else None
        where = flow().get(r["process"]).name
        out.append(
            Anomaly(
                id=f"stagnant:{order_no}:{bunno}",
                kind="stagnant",
                order_no=order_no,
                bunno=bunno,
                title=f"{_who(order_no, bunno)}は{where}が終わってから{days}日経ちますが、出荷検査が始まっていません。",
                evidence={
                    "inspected_at": r["read_at"],
                    "inspected_by": where,
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


def _naishoku(conn, orders, today) -> list[Anomaly]:
    """社外検査の読み取りと内職依頼を突き合わせる。"""
    if not flow().outsource:
        return []
    asked = requests(conn)
    out = []
    for r in out_now(conn):
        key = (r["order_no"], r["bunno"])
        if r["phase"] != "start":
            continue
        handed = datetime.fromisoformat(r["read_at"]).astimezone().date()
        req = asked.get(key)
        order = orders.get(r["order_no"])
        common = {
            "handed_at": r["read_at"],
            "item_code": order.item_code if order else None,
            "order_due_date": order.due_date.isoformat() if order and order.due_date else None,
        }
        if req is None:
            # 渡した日のうちは入力を待つ
            if handed >= today:
                continue
            out.append(
                Anomaly(
                    id=f"unrequested:{key[0]}:{key[1]}",
                    kind="unrequested",
                    order_no=key[0],
                    bunno=key[1],
                    title=f"{_who(*key)}は社外検査に渡してから{(today - handed).days}日経ちますが、内職依頼が入力されていません。",
                    evidence=common,
                )
            )
            continue
        if req["progress"] == FINISHED and req["progress_at"]:
            # 内職さんは終わっている。待っているのはこちらの引き取り
            done = datetime.fromisoformat(req["progress_at"]).astimezone().date()
            if (today - done).days >= PICKUP_DAYS:
                out.append(
                    Anomaly(
                        id=f"pickup_waiting:{key[0]}:{key[1]}",
                        kind="pickup_waiting",
                        order_no=key[0],
                        bunno=key[1],
                        title=f"{_who(*key)}は{req['worker']}さんが{done.isoformat()}に検査済にしましたが、{(today - done).days}日経っても納品で受け取っていません。",
                        evidence={**common, "worker": req["worker"], "finished_at": req["progress_at"],
                                  "worker_due_date": req["due_date"], "qty": req["qty"]},
                    )
                )
            continue
        due = date.fromisoformat(req["due_date"]) if req["due_date"] else None
        if due and due < today:
            out.append(
                Anomaly(
                    id=f"overdue_return:{key[0]}:{key[1]}",
                    kind="overdue_return",
                    order_no=key[0],
                    bunno=key[1],
                    title=f"{_who(*key)}は{req['worker']}さんへの納期{due.isoformat()}を{(today - due).days}日過ぎましたが、納品されていません（進捗は{req['progress']}）。",
                    evidence={**common, "worker": req["worker"], "worker_due_date": due.isoformat(), "qty": req["qty"],
                              "progress": req["progress"], "progress_at": req["progress_at"]},
                )
            )
    # 依頼はあるのに、そのカードが社外検査で読まれていない
    scanned = {
        (r["order_no"], r["bunno"])
        for r in conn.execute(
            """SELECT DISTINCT b.order_no, b.bunno FROM reads r JOIN bindings b ON b.id = r.binding_id
               WHERE r.process = ? AND r.outcome = 'recorded'""",
            (flow().outsource,),
        )
    }
    for key, req in asked.items():
        if key in scanned:
            continue
        order = orders.get(key[0])
        out.append(
            Anomaly(
                id=f"request_unscanned:{key[0]}:{key[1]}",
                kind="request_unscanned",
                order_no=key[0],
                bunno=key[1],
                title=f"{_who(*key)}は{req['worker']}さんへの内職依頼が入っていますが、カードは社外検査で読まれていません。",
                evidence={
                    "worker": req["worker"],
                    "requested_at": req["created_at"],
                    "worker_due_date": req["due_date"],
                    "qty": req["qty"],
                    "item_code": order.item_code if order else None,
                    # 同じ注文で社外検査に出ている分納。分納番号の書き写し違いを疑う材料
                    "same_order_out": sorted(b for o, b in scanned if o == key[0]),
                },
            )
        )
    return out
