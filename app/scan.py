"""読み取り機が打ち込んだ文字列の種類を、形だけで判定する。"""

import re
import unicodedata
from typing import Literal

Kind = Literal["ntag", "uhf", "hf10", "order", "bunno", "unknown"]

_RULES: list[tuple[Kind, re.Pattern[str]]] = [
    ("uhf", re.compile(r"E2[0-9A-F]{22}")),
    ("ntag", re.compile(r"04[0-9A-F]{12}")),
    ("hf10", re.compile(r"\d{10}")),
    # 通常は6桁。特殊な注文番号があるので幅を持たせる
    ("order", re.compile(r"\d{5,9}")),
    # QR に分納番号が無いので、記録票の「分納 n/m」の n を手で打つ
    ("bunno", re.compile(r"[1-9]\d{0,2}")),
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
