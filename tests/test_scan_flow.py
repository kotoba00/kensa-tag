from dataclasses import dataclass, field
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import anthropic
import pytest

from app.db import connect
from app.flow import flow
from app.detect import close_shipped, detect
from app.scan import classify
from app.service import handle_scan
from app.source import Lot, Order, Shipment
from app.triage import TriageItem, TriageReport, TriageUnavailable, run_triage
from app.views import in_inspection, order_detail

JST = timezone(timedelta(hours=9))
T0 = datetime(2026, 11, 2, 9, 0, tzinfo=JST)
TODAY = date(2026, 11, 2)
CARD_A = "04A1B2C3D4E5F6"
CARD_B = "04A1B2C3D4E5F7"
CARD_C = "04A1B2C3D4E5F8"


@dataclass
class FakeSource:
    o: list[Order] = field(default_factory=list)
    s: list[Shipment] = field(default_factory=list)
    l: list[Lot] = field(default_factory=list)

    def orders(self):
        return self.o

    def shipments(self):
        return self.s

    def lots(self):
        return self.l


@pytest.fixture
def conn():
    return connect(":memory:")


class Scanner:
    def __init__(self, conn):
        self.conn, self.n = conn, 0

    def __call__(self, raw, at=T0, device="d1", process="press", phase="point"):
        self.n += 1
        return handle_scan(
            self.conn, event_id=f"e{self.n}", raw=raw, device_id=device, read_at=at, process=process, phase=phase
        )

    def bind(self, no="100001", card=CARD_A, at=T0, device="d1"):
        self(no, at, device)
        return self(card, at + timedelta(seconds=1), device)

    def elevator(self, card=CARD_A, at=T0):
        return self(card, at, process="elevator")

    def inspection(self, card=CARD_A, at=T0):
        return self(card, at, process="inspection")


def order(no="100001", qty=100, due=None, done=False, stock=False):
    return Order(no, "X", "顧客", qty, due, done, stock)


@pytest.mark.parametrize(
    "raw,kind",
    [
        ("391341", "order"),
        ("9900002", "order"),
        ("04a1b2c3d4e5f6", "ntag"),
        ("０４A1B2C3D4E5F6", "ntag"),  # 日本語入力オンの全角
        ("04A1B 2C3D4E5F6", "ntag"),  # 実機の読み取りで途中に空白が入った
        ("1234567890", "hf"),
        ("1234abcd", "hf"),
        ("12345678", "hf"),
        ("A1B2C3D4E5F607", "hf"),
        ("E2" + "0" * 22, "uhf"),
        ("3", "unknown"),
        ("hello", "unknown"),
    ],
)
def test_classify(raw, kind):
    assert classify(raw)[0] == kind


def test_bind_leaves_number_empty(conn):
    s = Scanner(conn)
    assert s("100001").outcome == "order_set"
    r = s(CARD_A, T0 + timedelta(seconds=5))
    assert (r.outcome, r.order_no, r.bunno) == ("bound", "100001", None)
    assert conn.execute("SELECT bunno FROM bindings").fetchone()["bunno"] is None


def test_elevator_assigns_number(conn):
    s = Scanner(conn)
    s.bind()
    r = s.elevator(at=T0 + timedelta(minutes=5))
    assert (r.outcome, r.order_no, r.bunno) == ("assigned", "100001", 1)
    assert r.message == "分納番号は 1 番です"


def test_elevator_right_after_binding_is_not_debounced(conn):
    s = Scanner(conn)
    s.bind()
    assert s.elevator(at=T0 + timedelta(seconds=2)).outcome == "assigned"


def test_elevator_twice_keeps_number(conn):
    s = Scanner(conn)
    s.bind()
    s.elevator()
    assert s.elevator(at=T0 + timedelta(seconds=2)).outcome == "debounced"
    r = s.elevator(at=T0 + timedelta(hours=1))
    assert (r.outcome, r.bunno) == ("already_assigned", 1)
    assert conn.execute("SELECT COUNT(*) FROM bindings").fetchone()[0] == 1


def test_numbers_follow_elevator_order_not_binding_order(conn):
    s = Scanner(conn)
    s.bind(card=CARD_A)
    s.bind(card=CARD_B, at=T0 + timedelta(seconds=20))
    assert s.elevator(CARD_B, T0 + timedelta(minutes=1)).bunno == 1
    assert s.elevator(CARD_A, T0 + timedelta(minutes=2)).bunno == 2


def test_number_is_not_reused_after_rebinding(conn):
    s = Scanner(conn)
    s.bind(card=CARD_A)
    s.elevator(CARD_A)
    s.bind("100002", CARD_A, T0 + timedelta(minutes=1))
    s.bind("100001", CARD_B, T0 + timedelta(minutes=2))
    assert s.elevator(CARD_B, T0 + timedelta(minutes=3)).bunno == 2


def test_elevator_without_binding(conn):
    assert Scanner(conn).elevator().outcome == "unbound"


def test_card_held_after_binding_is_not_arrival(conn):
    s = Scanner(conn)
    s.bind()
    assert s(CARD_A, T0 + timedelta(seconds=3)).outcome == "debounced"
    assert in_inspection(conn, FakeSource(o=[order()]), TODAY) == []


def test_order_is_refused_outside_binding(conn):
    s = Scanner(conn)
    assert s("100001", process="elevator").outcome == "wrong_process"
    assert conn.execute("SELECT COUNT(*) FROM pending_orders").fetchone()[0] == 0


def test_tag_at_press_without_order(conn):
    assert Scanner(conn)(CARD_A).outcome == "no_order"


def test_binding_twice_keeps_number(conn):
    s = Scanner(conn)
    s.bind()
    s.elevator()
    r = s.bind(at=T0 + timedelta(minutes=1))
    assert (r.outcome, r.bunno) == ("already_bound", 1)


def test_read_twice_debounces_and_no_new_number(conn):
    s = Scanner(conn)
    s.bind()
    s.elevator()
    at = T0 + timedelta(hours=1)
    assert s.inspection(at=at).outcome == "recorded"
    assert s.inspection(at=at + timedelta(seconds=2)).outcome == "debounced"
    assert s.inspection(at=at + timedelta(hours=1)).outcome == "already_recorded"
    assert conn.execute("SELECT COUNT(*) FROM bindings").fetchone()[0] == 1


def test_rebind_warns_then_closes_previous_on_second_tap(conn):
    s = Scanner(conn)
    s.bind()
    s.elevator()
    at = T0 + timedelta(seconds=10)
    warn = s.bind("100002", at=at)
    # 使用中の注文と分納を知らせ、まだ付け替えない
    assert (warn.outcome, warn.order_no, warn.bunno) == ("in_use", "100001", 1)
    assert conn.execute("SELECT COUNT(*) FROM bindings WHERE ended_at IS NULL AND order_no='100001'").fetchone()[0] == 1
    # 置き直してもう一度かざすと付け替える。QR は読み直さなくてよい
    r = s(CARD_A, at + timedelta(seconds=6))
    assert (r.outcome, r.order_no, r.bunno) == ("bound", "100002", None)
    row = conn.execute("SELECT end_reason FROM bindings WHERE order_no='100001'").fetchone()
    assert row["end_reason"] == "rebound"


def test_rebind_double_read_is_not_a_confirmation(conn):
    s = Scanner(conn)
    s.bind()
    at = T0 + timedelta(seconds=10)
    assert s.bind("100002", at=at).outcome == "in_use"
    assert s(CARD_A, at + timedelta(seconds=2)).outcome == "in_use"


def test_rebind_warning_expires(conn):
    s = Scanner(conn)
    s.bind()
    at = T0 + timedelta(seconds=10)
    assert s.bind("100002", at=at).outcome == "in_use"
    assert s(CARD_A, at + timedelta(minutes=5)).outcome != "bound"


def test_expired_order_does_not_bind(conn):
    s = Scanner(conn)
    s("100001")
    assert s(CARD_A, T0 + timedelta(minutes=2)).outcome == "no_order"


def test_replayed_event_is_stored_once(conn):
    for _ in range(2):
        r = handle_scan(conn, event_id="same", raw="100001", device_id="d", read_at=T0, process="press")
    assert r.replayed
    assert conn.execute("SELECT COUNT(*) FROM reads").fetchone()[0] == 1


def test_devices_do_not_share_pending_order(conn):
    s = Scanner(conn)
    s("100001", device="d1")
    assert s(CARD_A, device="d2").outcome == "no_order"


def test_start_and_end_are_recorded_separately(conn):
    s = Scanner(conn)
    s.bind()
    s.elevator()
    at = T0 + timedelta(hours=1)
    assert s(CARD_A, at, process="packing", phase="start").message == "包装を開始しました"
    assert s(CARD_A, at + timedelta(seconds=2), process="packing", phase="end").outcome == "recorded"
    rows = conn.execute("SELECT phase FROM reads WHERE process='packing' ORDER BY id").fetchall()
    assert [r["phase"] for r in rows] == ["start", "end"]
    src = FakeSource(o=[order()])
    assert order_detail(conn, src, "100001")["bunnos"][0]["place"] == "包装（終了）"


def test_unknown_process_is_stored_without_failing(conn):
    s = Scanner(conn)
    assert s(CARD_A, process="nowhere").outcome == "unknown_process"
    assert s(CARD_A, process="inspection", phase="start").outcome == "unknown_process"
    assert conn.execute("SELECT COUNT(*) FROM reads").fetchone()[0] == 2


def test_reader_alternates_start_and_end(conn):
    s = Scanner(conn)
    s.bind()
    s.elevator()
    at = T0 + timedelta(hours=1)
    first = s(CARD_A, at, process="packing")
    assert (first.outcome, first.phase, first.message) == ("recorded", "start", "包装を開始しました")
    # 読み取り機の二度読みは終了にしない
    assert s(CARD_A, at + timedelta(seconds=2), process="packing").outcome == "debounced"
    assert s(CARD_A, at + timedelta(minutes=30), process="packing").phase == "end"
    # 間違えて終了にしても、もう一度読めば開始
    assert s(CARD_A, at + timedelta(minutes=31), process="packing").phase == "start"
    rows = conn.execute("SELECT phase FROM reads WHERE process='packing' AND outcome='recorded' ORDER BY id").fetchall()
    assert [r["phase"] for r in rows] == ["start", "end", "start"]
    src = FakeSource(o=[order()])
    assert order_detail(conn, src, "100001")["bunnos"][0]["place"] == "包装（開始）"
    # 別の工程は別に数える
    assert s(CARD_A, at + timedelta(minutes=32), process="finishing").phase == "start"


def test_process_can_be_added_by_config_only(conn, tmp_path, monkeypatch):
    path = tmp_path / "flow.json"
    path.write_text(
        '''{"checkpoint": "paint", "processes": [
            {"code": "press", "name": "プレス", "action": "bind", "phases": ["point"]},
            {"code": "paint", "name": "塗装", "action": "record", "phases": ["point"]}]}''',
        encoding="utf-8",
    )
    monkeypatch.setenv("FLOW_FILE", str(path))
    flow.cache_clear()
    try:
        s = Scanner(conn)
        s.bind()
        at = T0 + timedelta(hours=1)
        assert s(CARD_A, at, process="paint").message == "塗装に入りました"
        assert len(in_inspection(conn, FakeSource(o=[order()]), TODAY)) == 1
        assert s(CARD_A, at, process="elevator").outcome == "unknown_process"
    finally:
        monkeypatch.delenv("FLOW_FILE")
        flow.cache_clear()


def arrive(s, no="100001", card=CARD_A, days_ago=0):
    at = T0 - timedelta(days=days_ago)
    s.bind(no, card, at)
    s.elevator(card, at + timedelta(minutes=10))
    s.inspection(card, at + timedelta(hours=1))


def kinds(found):
    return sorted(a.kind for a in found)


def test_forgotten_detected_only_after_tracking_start(conn):
    s = Scanner(conn)
    arrive(s, "100001", days_ago=2)
    src = FakeSource(
        o=[order("100002"), order("100003")],
        s=[
            Shipment(TODAY, "100002", 1, 50, "完"),
            Shipment(TODAY - timedelta(days=30), "100003", 1, 50, "完"),
        ],
    )
    found = detect(conn, src, TODAY)
    assert [a.id for a in found if a.kind == "forgotten"] == ["forgotten:100002:1"]


def test_stagnant_and_stock_exclusion(conn):
    s = Scanner(conn)
    arrive(s, "100001", CARD_A, days_ago=4)
    arrive(s, "9900001", CARD_B, days_ago=4)
    src = FakeSource(o=[order("100001", due=TODAY), order("9900001", qty=0, stock=True)])
    found = detect(conn, src, TODAY)
    assert [a.order_no for a in found] == ["100001"]
    assert found[0].evidence["days_to_due"] == 0


def test_not_stagnant_when_recent(conn):
    arrive(Scanner(conn), days_ago=2)
    assert detect(conn, FakeSource(o=[order()]), TODAY) == []


def test_qty_mismatch_respects_tolerance(conn):
    src = FakeSource(
        s=[Shipment(TODAY, "1", 1, 99, "分"), Shipment(TODAY, "2", 1, 90, "分")],
        l=[Lot("1", 1, 100), Lot("2", 1, 100)],
    )
    assert [a.id for a in detect(conn, src, TODAY)] == ["qty:2:1"]


def test_total_counts_red_slip_and_needs_done(conn):
    src = FakeSource(
        o=[order("1", qty=100, done=True), order("2", qty=100, done=True), order("3", qty=100)],
        s=[
            Shipment(TODAY, "1", 1, 120, "完"),
            Shipment(TODAY, "1", None, -20, "返品"),
            Shipment(TODAY, "2", 1, 90, "不足"),
            Shipment(TODAY, "3", 1, 50, "分"),
        ],
    )
    assert [a.id for a in detect(conn, src, TODAY)] == ["total:2"]


def test_shipment_closes_binding_and_frees_card(conn):
    s = Scanner(conn)
    arrive(s)
    src = FakeSource(o=[order()], s=[Shipment(TODAY, "100001", 1, 100, "完")])
    assert close_shipped(conn, src.s) == 1
    assert in_inspection(conn, src, TODAY) == []
    assert s.inspection(CARD_A, T0 + timedelta(hours=9)).outcome == "unbound"


def test_order_detail_places(conn):
    s = Scanner(conn)
    arrive(s, "100001", CARD_A)
    s.bind("100001", CARD_B, T0 + timedelta(hours=2))
    s.elevator(CARD_B, T0 + timedelta(hours=3))
    s.bind("100001", CARD_C, T0 + timedelta(hours=4))
    src = FakeSource(o=[order()], s=[Shipment(TODAY, "100001", 3, 10, "分")])
    places = {b["bunno"]: b["place"] for b in order_detail(conn, src, "100001")["bunnos"]}
    assert places == {1: "社内検査", 2: "エレベーター", 3: "出荷済み", None: "プレス"}


def test_quantity_stays_undecided_until_daily_report(conn):
    s = Scanner(conn)
    arrive(s)
    src = FakeSource(o=[order()])
    assert in_inspection(conn, src, TODAY)[0]["qty"] is None
    assert order_detail(conn, src, "100001")["bunnos"][0]["produced"] is None
    src.l.append(Lot("100001", 1, 80))
    assert in_inspection(conn, src, TODAY)[0]["qty"] == 80
    assert order_detail(conn, src, "100001")["bunnos"][0]["produced"] == 80


def test_inspection_without_elevator_is_recorded_without_number(conn):
    s = Scanner(conn)
    s.bind()
    r = s.inspection(at=T0 + timedelta(hours=1))
    assert (r.outcome, r.bunno) == ("recorded", None)
    assert in_inspection(conn, FakeSource(o=[order()]), TODAY)[0]["bunno"] is None


def test_anomaly_is_a_japanese_sentence(conn):
    arrive(Scanner(conn), days_ago=5)
    found = detect(conn, FakeSource(o=[order()]), TODAY)
    assert found[0].title == "注文100001の分納1が、社内検査で読まれてから5日経っていますが、出荷データに載っていません。"


class FakeClient:
    def __init__(self, response=None, error=None):
        self.kwargs = None

        def parse(**kwargs):
            self.kwargs = kwargs
            if error:
                raise error
            return response

        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=parse))


def anomaly():
    from app.detect import Anomaly

    return Anomaly("stagnant:1:1", "stagnant", "1", 1, "滞留", {"days": 4})


def test_triage_drops_unknown_ids_and_uses_current_api():
    report = TriageReport(
        summary="s",
        items=[
            TriageItem(anomaly_id="stagnant:1:1", priority="今日中", likely_cause="c",
                       next_action="a", ask_role="社内検査", group=""),
            TriageItem(anomaly_id="made-up", priority="様子見", likely_cause="c",
                       next_action="a", ask_role="社内検査", group=""),
        ],
    )
    client = FakeClient(SimpleNamespace(stop_reason="end_turn", parsed_output=report))
    out = run_triage([anomaly()], TODAY, client)
    assert [i.anomaly_id for i in out.items] == ["stagnant:1:1"]
    assert client.kwargs["model"] == "claude-opus-5-5"
    assert client.kwargs["fallbacks"] == "default"


def test_triage_empty_does_not_call_api():
    client = FakeClient(error=RuntimeError("呼ばれないはず"))
    assert run_triage([], TODAY, client).items == []


def test_triage_refusal():
    client = FakeClient(SimpleNamespace(stop_reason="refusal", parsed_output=None))
    with pytest.raises(TriageUnavailable):
        run_triage([anomaly()], TODAY, client)


def test_triage_without_credentials_is_reported():
    client = FakeClient(error=anthropic.AnthropicError("no key"))
    with pytest.raises(TriageUnavailable):
        run_triage([anomaly()], TODAY, client)


def test_days_use_local_date_for_utc_timestamps(conn, monkeypatch):
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    time.tzset()
    s = Scanner(conn)
    # 日本時間 11/2 07:00 はブラウザの UTC 表記だと 11/1
    at = datetime(2026, 11, 1, 22, 0, tzinfo=timezone.utc)
    s.bind(at=at)
    s.elevator(at=at)
    s.inspection(at=at + timedelta(minutes=5))
    assert in_inspection(conn, FakeSource(o=[order()]), TODAY)[0]["days"] == 0


def _auto(tmp_path, src, client):
    from app.auto_triage import AutoTriage

    db = tmp_path / "auto.db"
    return AutoTriage(lambda: connect(db), src, settle=0, client=client), db


def _qty_report(anomaly_id="qty:2:1"):
    item = TriageItem(anomaly_id=anomaly_id, priority="今日中", likely_cause="c",
                      next_action="a", ask_role="社内検査", group="")
    return SimpleNamespace(stop_reason="end_turn", parsed_output=TriageReport(summary="s", items=[item]))


def test_auto_triage_saves_report_with_signature(tmp_path):
    from app.triage import latest_report, signature

    src = FakeSource(s=[Shipment(TODAY, "2", 1, 90, "分")], l=[Lot("2", 1, 100)])
    client = FakeClient(_qty_report())
    auto, db = _auto(tmp_path, src, client)
    auto.poke("now", None)
    auto.join(5)
    conn = connect(db)
    saved = latest_report(conn)
    assert saved["items"][0]["anomaly_id"] == "qty:2:1"
    assert saved["signature"] == signature(detect(conn, src, date.today()))
    assert not auto.thinking and auto.error is None


def test_auto_triage_skips_when_already_current(tmp_path):
    client = FakeClient(error=RuntimeError("呼ばれないはず"))
    auto, _ = _auto(tmp_path, FakeSource(), client)
    auto.poke("same", "same")
    assert not auto.thinking and client.kwargs is None


def test_auto_triage_does_not_retry_same_failure(tmp_path):
    src = FakeSource(s=[Shipment(TODAY, "2", 1, 90, "分")], l=[Lot("2", 1, 100)])
    client = FakeClient(error=anthropic.AnthropicError("no key"))
    auto, db = _auto(tmp_path, src, client)
    auto.poke("now", None)
    auto.join(5)
    assert auto.error
    from app.triage import signature

    sig = signature(detect(connect(db), src, date.today()))
    client.kwargs = None
    auto.poke(sig, None)
    assert not auto.thinking and client.kwargs is None


def _out(scan, card, at, no="100001"):
    scan.bind(no, card, at)
    scan(card, at + timedelta(minutes=1), process="elevator")
    scan(card, at + timedelta(hours=1), process="inspection")
    return scan(card, at + timedelta(hours=2), process="homework")


def test_naishoku_handover_needs_request_after_the_day(conn):
    from app.naishoku import pending

    scan = Scanner(conn)
    assert _out(scan, CARD_A, T0).phase == "start"
    src = FakeSource(o=[Order("100001", "X", "顧客", 10, None, False)])
    assert [p["bunno"] for p in pending(conn, src)] == [1]
    # 渡した日のうちは待つ。翌日になっても入力が無ければ拾う
    assert not [a for a in detect(conn, src, TODAY) if a.kind == "unrequested"]
    assert [a.kind for a in detect(conn, src, TODAY + timedelta(days=1)) if a.kind == "unrequested"] == ["unrequested"]


def test_naishoku_overdue_and_return_restarts_stagnant(conn):
    from app.naishoku import add

    scan = Scanner(conn)
    _out(scan, CARD_A, T0)
    src = FakeSource(o=[Order("100001", "X", "顧客", 10, None, False)])
    src.workers = lambda: ["内職A"]
    add(conn, src, order_no="100001", bunno=1, worker="内職A", due_date=TODAY + timedelta(days=2), qty=10)
    late = TODAY + timedelta(days=5)
    kinds = [a.kind for a in detect(conn, src, late)]
    assert "overdue_return" in kinds and "stagnant" not in kinds

    # 納品で受け取ったら、滞留は納品の日から数え直す
    scan(CARD_A, datetime.combine(late, T0.timetz()), process="homework")
    kinds = [a.kind for a in detect(conn, src, late + timedelta(days=1))]
    assert "overdue_return" not in kinds and "stagnant" not in kinds
    assert "stagnant" in [a.kind for a in detect(conn, src, late + timedelta(days=3))]


def test_request_without_scan_points_to_other_bunno(conn):
    from app.naishoku import add

    scan = Scanner(conn)
    _out(scan, CARD_A, T0)
    src = FakeSource(o=[Order("100001", "X", "顧客", 10, None, False)])
    src.workers = lambda: ["内職A"]
    add(conn, src, order_no="100001", bunno=2, worker="内職A", due_date=None, qty=None)
    found = [a for a in detect(conn, src, TODAY + timedelta(days=1)) if a.kind == "request_unscanned"]
    assert found[0].bunno == 2 and found[0].evidence["same_order_out"] == [1]
