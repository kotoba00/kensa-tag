"""生産管理データの読み込み。接続先は差し替えられるよう、ここで閉じる。

SOURCE_DIR に orders.json / shipments.json / lots.json を置く。
本番では生産管理から同じ形で書き出したものを別のフォルダーに置く。
"""

import json
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

DEFAULT_DIR = Path(__file__).parent.parent / "demo" / "data"


@dataclass(frozen=True)
class Order:
    order_no: str
    item_code: str
    customer: str
    qty: int
    due_date: date | None
    done: bool  # 完納の正本（注残の「済」）
    stock: bool = False  # 自社在庫向け。納期の遅れとして扱わない


@dataclass(frozen=True)
class Shipment:
    ship_date: date
    order_no: str
    bunno: int | None
    qty: int  # 赤伝はマイナス
    status: str  # 完 / 分 / 不足 / 再検分 / 返品


@dataclass(frozen=True)
class Lot:
    order_no: str
    bunno: int
    produced: int


class ProductionSource(Protocol):
    def orders(self) -> list[Order]: ...
    def shipments(self) -> list[Shipment]: ...
    def lots(self) -> list[Lot]: ...


class JsonSource:
    def __init__(self, directory: str | Path | None = None):
        self.directory = Path(directory or os.environ.get("SOURCE_DIR") or DEFAULT_DIR)

    def _load(self, name: str) -> list[dict]:
        return json.loads((self.directory / name).read_text(encoding="utf-8"))

    def orders(self) -> list[Order]:
        return [
            Order(
                order_no=str(r["order_no"]),
                item_code=r["item_code"],
                customer=r["customer"],
                qty=int(r["qty"]),
                due_date=date.fromisoformat(r["due_date"]) if r.get("due_date") else None,
                done=bool(r["done"]),
                stock=bool(r.get("stock", False)),
            )
            for r in self._load("orders.json")
        ]

    def shipments(self) -> list[Shipment]:
        return [
            Shipment(
                ship_date=date.fromisoformat(r["ship_date"]),
                order_no=str(r["order_no"]),
                bunno=r.get("bunno"),
                qty=int(r["qty"]),
                status=r["status"],
            )
            for r in self._load("shipments.json")
        ]

    def lots(self) -> list[Lot]:
        return [
            Lot(str(r["order_no"]), int(r["bunno"]), int(r["produced"]))
            for r in self._load("lots.json")
        ]
