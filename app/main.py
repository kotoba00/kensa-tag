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

from .db import connect
from .detect import detect
from .service import handle_scan
from .source import JsonSource
from .triage import TriageUnavailable, latest_report, run_triage, save_report
from .views import in_inspection, order_detail

STATIC = Path(__file__).parent / "static"
DEFAULT_DB = Path(__file__).parent.parent / "data" / "kensa.db"
source = JsonSource()
app = FastAPI(title="検査場タグ")


def get_conn() -> Iterator[sqlite3.Connection]:
    # 処理はスレッドをまたぐので、接続はリクエストごとに分ける
    conn = connect(os.environ.get("DB_PATH", DEFAULT_DB))
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


@app.post("/api/scan")
def scan(body: ScanIn, conn: Conn):
    result = handle_scan(
        conn,
        event_id=body.event_id,
        raw=body.raw,
        device_id=body.device_id,
        read_at=body.read_at,
    )
    return result.__dict__


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
    return {
        "items": [a.as_dict() for a in detect(conn, source, date.today())],
        "triage": latest_report(conn),
        "generated_at": datetime.now().astimezone().isoformat(),
    }


@app.post("/api/triage")
def triage(conn: Conn):
    today = date.today()
    try:
        report = run_triage(detect(conn, source, today), today)
    except TriageUnavailable as e:
        raise HTTPException(503, str(e)) from e
    save_report(conn, report)
    return latest_report(conn)


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
