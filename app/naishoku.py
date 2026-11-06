"""内職依頼。誰に、どの分納を、いつまでに渡したか。

本番の正本は AppSheet の内職依頼。kensa-tag は「社外検査でかざしたのに依頼がまだ無い分納」を出し、
AppSheet の入力フォームではそれを選ぶだけにする（担当はグループで決まり、納期だけ入れる）。
デモでは同じ形の表を naishoku に持ち、/naishoku の画面から入れる。
内職さんは専用ページ /w/鍵 で、進捗（未着手 / ちぎり済 / 検査済）だけを選べる。
"""

import secrets
import sqlite3
from datetime import date, datetime

from .flow import flow
from .source import ProductionSource


class NaishokuError(ValueError):
    pass


def out_now(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """最後に読んだのが社外検査の分納。phase が start なら外にあり、end なら納品済み。"""
    if not flow().outsource:
        return []
    return conn.execute(
        """SELECT b.id, b.order_no, b.bunno, r.phase, r.read_at
           FROM bindings b
           JOIN reads r ON r.id = (SELECT id FROM reads WHERE binding_id = b.id
                                   ORDER BY read_at DESC, id DESC LIMIT 1)
           WHERE b.ended_at IS NULL AND b.bunno IS NOT NULL AND r.process = ?""",
        (flow().outsource,),
    ).fetchall()


def requests(conn: sqlite3.Connection) -> dict[tuple[str, int], sqlite3.Row]:
    return {(r["order_no"], r["bunno"]): r for r in conn.execute("SELECT * FROM naishoku")}


def pending(conn: sqlite3.Connection, source: ProductionSource) -> list[dict]:
    """社外検査で渡したのに、内職依頼がまだ無い分納。新しいものを上に。"""
    have = requests(conn)
    orders = {o.order_no: o for o in source.orders()}
    produced = {(lot.order_no, lot.bunno): lot.produced for lot in source.lots()}
    out = []
    for r in out_now(conn):
        key = (r["order_no"], r["bunno"])
        if r["phase"] != "start" or key in have:
            continue
        order = orders.get(r["order_no"])
        out.append(
            {
                "order_no": r["order_no"],
                "bunno": r["bunno"],
                "item_code": order.item_code if order else None,
                "qty": produced.get(key),
                "handed_at": r["read_at"],
            }
        )
    return sorted(out, key=lambda x: x["handed_at"], reverse=True)


def add(
    conn: sqlite3.Connection,
    source: ProductionSource,
    *,
    order_no: str,
    bunno: int,
    worker: str,
    due_date: date | None,
    qty: int | None,
    now: datetime | None = None,
) -> dict:
    if worker not in source.workers():
        raise NaishokuError("その内職さんは一覧にありません")
    try:
        with conn:
            conn.execute(
                """INSERT INTO naishoku (order_no, bunno, qty, worker, due_date, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (order_no, bunno, qty, worker, due_date.isoformat() if due_date else None,
                 (now or datetime.now().astimezone()).isoformat()),
            )
    except sqlite3.IntegrityError as e:
        raise NaishokuError(f"注文{order_no}の分納{bunno}は、もう依頼が入っています") from e
    return dict(requests(conn)[(order_no, bunno)])


# ここから内職さん本人が使う専用ページ。本番は同じ動きを GAS で作り、AppSheet のスプシに書く

RECENT_DAYS = 7  # 納品済みを専用ページに残す日数
# 内職依頼の「進捗」列と同じ選択肢。検査済が「終わった」
PROGRESS = ("未着手", "ちぎり済", "検査済")
FINISHED = "検査済"


def worker_key(conn: sqlite3.Connection, worker: str) -> str:
    """内職さんの専用ページの鍵。無ければ作る。"""
    row = conn.execute("SELECT key FROM worker_keys WHERE worker = ?", (worker,)).fetchone()
    if row:
        return row["key"]
    key = secrets.token_urlsafe(16)
    with conn:
        conn.execute("INSERT INTO worker_keys (worker, key, created_at) VALUES (?, ?, ?)",
                     (worker, key, datetime.now().astimezone().isoformat()))
    return key


def worker_of(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT worker FROM worker_keys WHERE key = ?", (key,)).fetchone()
    return row["worker"] if row else None


def _handover(conn: sqlite3.Connection, order_no: str, bunno: int) -> tuple[str | None, str | None]:
    """社外検査で渡した時刻と、納品で受け取った時刻（渡したあとのものだけ）。"""
    row = conn.execute(
        """SELECT MAX(CASE WHEN r.phase = 'start' THEN r.read_at END) AS handed,
                  MAX(CASE WHEN r.phase = 'end' THEN r.read_at END) AS returned
           FROM reads r JOIN bindings b ON b.id = r.binding_id
           WHERE b.order_no = ? AND b.bunno = ? AND r.process = ? AND r.outcome = 'recorded'""",
        (order_no, bunno, flow().outsource),
    ).fetchone()
    handed, returned = row["handed"], row["returned"]
    if handed and returned and returned < handed:
        returned = None
    return handed, returned


def worker_items(conn: sqlite3.Connection, source: ProductionSource, worker: str, today: date) -> list[dict]:
    """内職さん本人に見せる分納。手元にあるものを納期順に、そのあと最近納品したもの。"""
    orders = {o.order_no: o for o in source.orders()}
    out = []
    for r in conn.execute("SELECT * FROM naishoku WHERE worker = ?", (worker,)):
        handed, returned = _handover(conn, r["order_no"], r["bunno"])
        # カードがまだ渡されていない依頼は、内職さんには出さない
        if not handed:
            continue
        if returned and (today - datetime.fromisoformat(returned).astimezone().date()).days > RECENT_DAYS:
            continue
        order = orders.get(r["order_no"])
        out.append(
            {
                "order_no": r["order_no"],
                "bunno": r["bunno"],
                "item_code": order.item_code if order else None,
                "qty": r["qty"],
                "due_date": r["due_date"],
                "handed_at": handed,
                "progress": r["progress"],
                "progress_at": r["progress_at"],
                "returned_at": returned,
            }
        )
    return sorted(out, key=lambda x: (x["returned_at"] is not None, x["due_date"] or "9999", x["order_no"], x["bunno"]))


def set_progress(
    conn: sqlite3.Connection, worker: str, order_no: str, bunno: int, progress: str, now: datetime | None = None
) -> None:
    """内職さんが進捗を選ぶ。内職さんが変えられるのはここだけで、納品で受け取ったあとは変えられない。"""
    if progress not in PROGRESS:
        raise NaishokuError("進捗は「" + "」「".join(PROGRESS) + "」から選んでください")
    row = conn.execute("SELECT worker FROM naishoku WHERE order_no = ? AND bunno = ?", (order_no, bunno)).fetchone()
    if not row or row["worker"] != worker:
        raise NaishokuError("この分納はあなたへの依頼にありません")
    if _handover(conn, order_no, bunno)[1]:
        raise NaishokuError("この分納はもう納品で受け取っています")
    with conn:
        conn.execute(
            "UPDATE naishoku SET progress = ?, progress_at = ? WHERE order_no = ? AND bunno = ?",
            (progress, (now or datetime.now().astimezone()).isoformat(), order_no, bunno),
        )
