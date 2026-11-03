"""検出結果が変わったときだけ、裏で Claude に見立てを頼む。

Claude の返事には 20〜30 秒かかる。画面を開いたときに待たせないよう、
保存済みの見立てを先に出し、作り直しは裏で1本だけ走らせる。
"""

import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import date
from typing import Any

from .detect import detect
from .source import ProductionSource
from .triage import TriageUnavailable, latest_report, run_triage, save_report, signature

# かざしはまとまって来る。読み終わるのを少し待ってから聞く
SETTLE_SECONDS = 10.0


class AutoTriage:
    def __init__(
        self,
        open_conn: Callable[[], sqlite3.Connection],
        source: ProductionSource,
        settle: float = SETTLE_SECONDS,
        client: Any = None,
    ):
        self._open_conn = open_conn
        self._source = source
        self._settle = settle
        self._client = client
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._failed_sig: str | None = None
        self.error: str | None = None

    @property
    def thinking(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def poke(self, current_sig: str, saved_sig: str | None) -> None:
        """今の検出結果と保存済みの見立てが食い違っていれば、作り直しを始める。"""
        # 同じ検出結果で失敗したものは繰り返さない。ボタンからは聞き直せる
        if current_sig in (saved_sig, self._failed_sig):
            return
        with self._lock:
            if self.thinking:
                return
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    def _run(self) -> None:
        # 考えている間に新しいかざしが来たら、終わったあとにもう一度見る
        while True:
            time.sleep(self._settle)
            conn = self._open_conn()
            try:
                today = date.today()
                found = detect(conn, self._source, today)
                sig = signature(found)
                saved = latest_report(conn)
                if saved and saved["signature"] == sig:
                    return
                try:
                    report = run_triage(found, today, self._client)
                except TriageUnavailable as e:
                    self.error = str(e)
                    self._failed_sig = sig
                    return
                save_report(conn, report, sig)
                self.error = None
            finally:
                conn.close()
