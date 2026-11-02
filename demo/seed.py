"""デモ用の架空データを作る。実在の注文・取引先・人は含まない。

検出4種が1件ずつ以上出る状態を、今日の日付を基準に作り直す。
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
from app.service import handle_scan  # noqa: E402

DATA = ROOT / "demo" / "data"
CARD = "04A1B2C3D4E5{:02X}"


def build(today: date) -> dict[str, list[dict]]:
    d = lambda n: (today + timedelta(days=n)).isoformat()  # noqa: E731
    orders = [
        ("100101", "DEMO-A10", "顧客A", 300, 2, False, False),   # 滞留。納期が近い
        ("100102", "DEMO-B20", "顧客B", 200, 12, False, False),  # 滞留。納期に余裕
        ("100103", "DEMO-C30", "顧客A", 400, 9, False, False),   # 順調に検査場にある
        ("100104", "DEMO-D40", "顧客C", 150, -1, True, False),   # かざし忘れ
        ("100105", "DEMO-E50", "顧客C", 120, -3, True, False),   # 同じ日のかざし忘れ
        ("100106", "DEMO-F60", "顧客B", 500, -2, False, False),  # 生産数と計量の差
        ("100107", "DEMO-G70", "顧客A", 100, -4, True, False),   # 分納の合計が合わない
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
        ],
    }


def seed(db_path: Path, today: date) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    for name, rows in build(today).items():
        (DATA / name).write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    if db_path.exists():
        db_path.unlink()
    conn = connect(db_path)
    counter = 0

    def scan(raw: str, days_ago: int, hour: int) -> None:
        nonlocal counter
        counter += 1
        at = datetime.combine(today - timedelta(days=days_ago), time(hour)).astimezone()
        handle_scan(conn, event_id=f"seed-{counter}", raw=raw, device_id="demo-kensa-1", read_at=at)

    # (注文番号, カード, 紐付けの何日前, 検査場に入った何日前)
    flows = [
        ("100101", 1, 8, 6),
        ("100102", 2, 7, 4),
        ("100103", 3, 3, 1),
        ("100106", 6, 10, 5),
        ("100107", 7, 12, 7),
    ]
    for order_no, card, bound, arrived in flows:
        uid = CARD.format(card)
        scan(order_no, bound, 9)
        scan("1", bound, 9)
        scan(uid, bound, 9)
        scan(uid, arrived, 15)
    # 紐付けたまま検査場に戻っていないもの。出荷もない
    scan("100103", 2, 9)
    scan("2", 2, 9)
    scan(CARD.format(8), 2, 9)
    conn.close()


if __name__ == "__main__":
    path = Path(os.environ.get("DB_PATH", ROOT / "data" / "kensa.db"))
    seed(path, date.today())
    print(f"デモデータを作りました: {DATA} / {path}")
