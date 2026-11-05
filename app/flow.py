"""工程の一覧。読み取りの地点を足すときは flow.json だけを直す。

action の意味:
  bind   注文番号とカードを紐付ける
  assign 分納番号を振る
  record 読んだことを記録する
phases は point（1回読むだけ）か start と end（開始と終了）。
checkpoint は「いま何があるか」を見張る工程で、Claude に渡す検出の基準になる。
outsource は内職さんに渡す工程。開始で渡し、納品で終了する。誰に渡したかは内職依頼に入る。
zone と map は工場図の画面で使う。zone は [左, 上, 幅, 高さ]、map の大きさの中で決める。
"""

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

DEFAULT_FILE = Path(__file__).parent / "flow.json"
PHASE_LABEL = {"start": "開始", "end": "終了"}


@dataclass(frozen=True)
class Process:
    code: str
    name: str
    action: Literal["bind", "assign", "record"]
    phases: tuple[str, ...]
    zone: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class Flow:
    processes: tuple[Process, ...]
    checkpoint: str
    map: dict | None = None
    outsource: str | None = None

    def get(self, code: str) -> Process | None:
        return next((p for p in self.processes if p.code == code), None)

    @property
    def checkpoint_name(self) -> str:
        return self.get(self.checkpoint).name

    def place(self, code: str, phase: str) -> str:
        process = self.get(code)
        name = process.name if process else code
        return f"{name}（{PHASE_LABEL[phase]}）" if phase in PHASE_LABEL else name


@lru_cache
def flow() -> Flow:
    raw = json.loads(Path(os.environ.get("FLOW_FILE") or DEFAULT_FILE).read_text(encoding="utf-8"))
    return Flow(
        processes=tuple(
            Process(
                p["code"], p["name"], p["action"], tuple(p["phases"]),
                tuple(p["zone"]) if p.get("zone") else None,
            )
            for p in raw["processes"]
        ),
        checkpoint=raw["checkpoint"],
        map=raw.get("map"),
        outsource=raw.get("outsource"),
    )
