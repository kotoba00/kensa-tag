import os
import sqlite3
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import AwareDatetime, BaseModel

from .auto_triage import AutoTriage
from .db import connect
from .detect import detect
from .flow import flow
from .service import POINT, handle_scan
from .source import JsonSource
from .triage import TriageUnavailable, latest_report, run_triage, save_report, signature
from .views import in_inspection, order_detail

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
    read_at: AwareDatetime
    process: str
    phase: str = POINT


@app.post("/api/scan")
def scan(body: ScanIn, conn: Conn):
    result = handle_scan(
        conn,
        event_id=body.event_id,
        raw=body.raw,
        device_id=body.device_id,
        read_at=body.read_at,
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


app.mount("/static", StaticFiles(directory=STATIC), name="static")
