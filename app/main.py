import os
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime, BaseModel, Field

from .auto_triage import AutoTriage
from .db import connect
from .detect import detect
from .flow import flow
from .naishoku import NaishokuError, add, pending
from .service import POINT, handle_scan
from .source import JsonSource
from .triage import TriageUnavailable, latest_report, run_triage, save_report, signature
from .scan import classify
from .views import board, in_inspection, order_detail, tag_detail

STATIC = Path(__file__).parent / "static"
DEFAULT_DB = Path(__file__).parent.parent / "data" / "kensa.db"
source = JsonSource()
app = FastAPI(title="検査場タグ")


def open_conn() -> sqlite3.Connection:
    return connect(os.environ.get("DB_PATH", DEFAULT_DB))


auto = AutoTriage(open_conn, source)


def get_conn() -> Iterator[sqlite3.Connection]:
    # 処理はスレッドをまたぐので、接続はリクエストごとに分ける
    conn = open_conn()
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


class ScanIn(BaseModel):
    event_id: str
    raw: str
    device_id: str
    # 時計を持たない読み取り機は、読んでから送るまでの経過時間を送る
    read_at: AwareDatetime | None = None
    age_ms: int | None = Field(default=None, ge=0)
    process: str
    phase: str = POINT

    def read_time(self) -> datetime:
        if self.read_at:
            return self.read_at
        return datetime.now().astimezone() - timedelta(milliseconds=self.age_ms or 0)


@app.post("/api/scan")
def scan(body: ScanIn, conn: Conn):
    result = handle_scan(
        conn,
        event_id=body.event_id,
        raw=body.raw,
        device_id=body.device_id,
        read_at=body.read_time(),
        process=body.process,
        phase=body.phase,
    )
    return result.__dict__


@app.get("/api/flow")
def get_flow():
    f = flow()
    return {
        "checkpoint": f.checkpoint,
        "checkpoint_name": f.checkpoint_name,
        "processes": [p.__dict__ for p in f.processes],
        "map": f.map,
        "outsource": f.outsource,
    }


@app.get("/api/inspection")
def inspection(conn: Conn):
    return in_inspection(conn, source, date.today())


@app.get("/api/orders/{order_no}")
def order(order_no: str, conn: Conn):
    detail = order_detail(conn, source, order_no)
    if detail is None:
        raise HTTPException(404, "その注文番号は見つかりません")
    return detail


@app.get("/api/board")
def get_board(conn: Conn):
    today = date.today()
    found = detect(conn, source, today)
    return {
        "items": board(conn, source, today, found, latest_report(conn)),
        "generated_at": datetime.now().astimezone().isoformat(),
    }


class NaishokuIn(BaseModel):
    order_no: str
    bunno: int
    worker: str
    due_date: date | None = None
    qty: int | None = Field(default=None, ge=0)


@app.get("/api/naishoku")
def naishoku(conn: Conn):
    return {"workers": source.workers(), "pending": pending(conn, source)}


@app.post("/api/naishoku", status_code=201)
def naishoku_add(body: NaishokuIn, conn: Conn):
    try:
        return add(conn, source, **body.model_dump())
    except NaishokuError as e:
        raise HTTPException(409, str(e)) from e


@app.get("/api/tags/{raw}")
def tag(raw: str, conn: Conn):
    kind, uid = classify(raw)
    if kind not in ("ntag", "uhf", "hf"):
        raise HTTPException(400, "カードの番号ではありません")
    detail = tag_detail(conn, source, uid)
    if detail is None:
        raise HTTPException(404, "このカードはまだ注文と紐付いていません")
    return detail


@app.get("/api/anomalies")
def anomalies(conn: Conn):
    found = detect(conn, source, date.today())
    sig = signature(found)
    report = latest_report(conn)
    saved_sig = report["signature"] if report else None
    if os.environ.get("AUTO_TRIAGE", "1") != "0":
        auto.poke(sig, saved_sig)
    return {
        "items": [a.as_dict() for a in found],
        "triage": report,
        # 見立てが今の検出結果より古いか。古ければ画面で断り書きを出す
        "triage_stale": report is not None and saved_sig != sig,
        "thinking": auto.thinking,
        "auto_error": auto.error,
        "generated_at": datetime.now().astimezone().isoformat(),
    }


@app.post("/api/triage")
def triage(conn: Conn):
    today = date.today()
    found = detect(conn, source, today)
    try:
        report = run_triage(found, today)
    except TriageUnavailable as e:
        raise HTTPException(503, str(e)) from e
    save_report(conn, report, signature(found))
    return latest_report(conn)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/board")
def board_page():
    # 工場図。大きな画面に出しっぱなしにする
    return FileResponse(STATIC / "board.html")


@app.get("/naishoku")
def naishoku_page():
    # 内職さんに渡すときの入力。本番の AppSheet のフォームと同じ動きをまねる
    return FileResponse(STATIC / "naishoku.html")


@app.get("/t/{uid}")
def tag_page(uid: str):
    # カードに書いた URL をスマホで開いたときの画面。中身は /api/tags から読む
    return FileResponse(STATIC / "tag.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
