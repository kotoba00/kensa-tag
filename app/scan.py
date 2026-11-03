"""読み取り機が打ち込んだ文字列の種類を、形だけで判定する。"""

import re
import unicodedata
from typing import Literal

Kind = Literal["ntag", "uhf", "hf", "order", "unknown"]

_RULES: list[tuple[Kind, re.Pattern[str]]] = [
    ("uhf", re.compile(r"E2[0-9A-F]{22}")),
    ("ntag", re.compile(r"04[0-9A-F]{12}")),
    # 4バイト（16進8桁か10進10桁）と7バイト。同じカードでも読み取り機で長さが違う
    ("hf", re.compile(r"\d{10}|[0-9A-F]{8}|[0-9A-F]{14}")),
    # 通常は6桁、在庫向けは7桁。8桁は HF の UID と区別できない
    ("order", re.compile(r"\d{5,7}")),
]


def normalize(raw: str) -> str:
    """日本語入力がオンのときの全角を半角へ直し、前後の空白を落とす。"""
    return unicodedata.normalize("NFKC", raw).strip().upper()


def classify(raw: str) -> tuple[Kind, str]:
    value = normalize(raw)
    for kind, pattern in _RULES:
        if pattern.fullmatch(value):
            return kind, value
    return "unknown", value
