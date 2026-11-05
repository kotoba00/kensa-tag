"""デモ用の架空データを作る。実在の注文・取引先・人は含まない。

検出7種が1件ずつ以上出る状態を、今日の日付を基準に作り直す。
    uv run python demo/seed.py
"""

import json
import os
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from app.db import connect  # noqa: E402
from app.naishoku import add  # noqa: E402
from app.service import handle_scan  # noqa: E402
from app.source import JsonSource  # noqa: E402

DATA = ROOT / "demo" / "data"
CARD = "04A1B2C3D4E5{:02X}"


def build(today: date) -> dict[str, list[dict]]:
    d = lambda n: (today + timedelta(days=n)).isoformat()  # noqa: E731
    orders = [
        ("100101", "DEMO-A10", "顧客A", 300, 2, False, False),   # 滞留。納期が近い
        ("100102", "DEMO-B20", "顧客B", 200, 12, False, False),  # 滞留。納期に余裕
        ("100103", "DEMO-C30", "顧客A", 400, 9, False, False),   # 順調に社内検査にある
        ("100104", "DEMO-D40", "顧客C", 150, -1, True, False),   # かざし忘れ
        ("100105", "DEMO-E50", "顧客C", 120, -3, True, False),   # 同じ日のかざし忘れ
        ("100106", "DEMO-F60", "顧客B", 500, -2, False, False),  # 生産数と計量の差
        ("100107", "DEMO-G70", "顧客A", 100, -4, True, False),   # 分納の合計が合わない
        ("100108", "DEMO-H80", "顧客B", 250, 7, False, False),   # 実演用。まだ何も読んでいない
        ("100109", "DEMO-J90", "顧客C", 180, 10, False, False),  # 実演用
        ("100110", "DEMO-K11", "顧客A", 600, 3, False, False),   # 内職さんの納期を過ぎても戻らない
        ("100111", "DEMO-L12", "顧客B", 400, 8, False, False),   # 内職さんの所で順調
        ("100112", "DEMO-M13", "顧客C", 300, 6, False, False),   # 渡したのに内職依頼が無い
        ("100113", "DEMO-N14", "顧客A", 500, 5, False, False),   # 依頼の分納番号が書き写し違い
        ("9900001", "DEMO-Z99", "自社", 0, None, False, True),   # 在庫向け。検出しない
    ]
    return {
        "orders.json": [
            {
                "order_no": no, "item_code": item, "customer": cust, "qty": qty,
                "due_date": d(due) if due is not None else None, "done": done, "stock": stock,
            }
            for no, item, cust, qty, due, done, stock in orders
        ],
        "shipments.json": [
            {"ship_date": d(-1), "order_no": "100104", "bunno": 1, "qty": 150, "status": "完"},
            {"ship_date": d(-1), "order_no": "100105", "bunno": 1, "qty": 120, "status": "完"},
            {"ship_date": d(-2), "order_no": "100106", "bunno": 1, "qty": 212, "status": "分"},
            {"ship_date": d(-5), "order_no": "100107", "bunno": 1, "qty": 60, "status": "分"},
            {"ship_date": d(-3), "order_no": "100107", "bunno": 2, "qty": 30, "status": "不足"},
        ],
        "lots.json": [
            {"order_no": "100106", "bunno": 1, "produced": 250},
            {"order_no": "100101", "bunno": 1, "produced": 300},
            {"order_no": "100110", "bunno": 1, "produced": 600},
            {"order_no": "100111", "bunno": 1, "produced": 240},
            {"order_no": "100111", "bunno": 2, "produced": 160},
        ],
        # 内職さんの呼び名。本番は AppSheet のグループ
        "workers.json": ["検査A", "検査B", "検査C"],
    }


def seed(db_path: Path, today: date) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    for name, rows in build(today).items():
        (DATA / name).write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    if db_path.exists():
        db_path.unlink()
    conn = connect(db_path)
    counter = 0

    def scan(raw: str, process: str, days_ago: int, hour: int) -> None:  # noqa: E306
        nonlocal counter
        counter += 1
        at = datetime.combine(today - timedelta(days=days_ago), time(hour)).astimezone()
        handle_scan(conn, event_id=f"seed-{counter}", raw=raw, device_id=f"demo-{process}", read_at=at, process=process)

    def bind(order_no: str, card: int, days_ago: int) -> str:
        uid = CARD.format(card)
        scan(order_no, "press", days_ago, 9)
        scan(uid, "press", days_ago, 9)
        return uid

    # (注文番号, カード, 紐付けの何日前, 社内検査に入った何日前)
    flows = [
        ("100101", 1, 8, 6),
        ("100102", 2, 7, 4),
        ("100103", 3, 3, 1),
        ("100106", 6, 10, 5),
        ("100107", 7, 12, 7),
    ]
    for order_no, card, bound, arrived in flows:
        uid = bind(order_no, card, bound)
        scan(uid, "elevator", bound, 11)
        scan(uid, "inspection", arrived, 15)
    # エレベーターを通って、まだ社内検査に戻っていないもの
    scan(bind("100103", 8, 2), "elevator", 2, 11)
    # 紐付けただけで、分納番号がまだ無いもの
    bind("100103", 9, 0)

    # 社外検査。(注文番号, カード, 紐付け, 社外検査に渡した何日前, 納品の何日前)
    outs = [
        ("100110", 10, 9, 6, None),
        ("100111", 11, 6, 3, None),
        ("100111", 12, 6, 3, 0),
        ("100112", 13, 5, 2, None),
        ("100113", 14, 6, 3, None),
    ]
    for order_no, card, bound, handed, returned in outs:
        uid = bind(order_no, card, bound)
        scan(uid, "elevator", bound, 11)
        scan(uid, "inspection", bound - 1, 15)
        scan(uid, "homework", handed, 10)
        if returned is not None:
            scan(uid, "homework", returned, 9)
    source = JsonSource(DATA)
    asked = lambda no, bunno, worker, days_ago, due, qty: add(  # noqa: E731
        conn, source, order_no=no, bunno=bunno, worker=worker, qty=qty,
        due_date=today + timedelta(days=due),
        now=datetime.combine(today - timedelta(days=days_ago), time(10, 30)).astimezone(),
    )
    asked("100110", 1, "検査A", 6, -2, 600)
    asked("100111", 1, "検査B", 3, 2, 240)
    asked("100111", 2, "検査B", 3, 2, 160)
    # カードは分納1なのに、依頼は分納2で入っている
    asked("100113", 2, "検査C", 3, 3, None)
    conn.close()


if __name__ == "__main__":
    path = Path(os.environ.get("DB_PATH", ROOT / "data" / "kensa.db"))
    seed(path, date.today())
    print(f"デモデータを作りました: {DATA} / {path}")
