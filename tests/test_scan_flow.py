from dataclasses import dataclass, field
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import anthropic
import pytest

from app.db import connect
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

    def __call__(self, raw, at=T0, device="d1"):
        self.n += 1
        return handle_scan(self.conn, event_id=f"e{self.n}", raw=raw, device_id=device, read_at=at)


def order(no="100001", qty=100, due=None, done=False, stock=False):
    return Order(no, "X", "顧客", qty, due, done, stock)


@pytest.mark.parametrize(
    "raw,kind",
    [
        ("391341", "order"),
        ("9900002", "order"),
        ("04a1b2c3d4e5f6", "ntag"),
        ("０４A1B2C3D4E5F6", "ntag"),  # 日本語入力オンの全角
        ("1234567890", "hf10"),
        ("E2" + "0" * 22, "uhf"),
        ("hello", "unknown"),
    ],
)
def test_classify(raw, kind):
    assert classify(raw)[0] == kind


def test_bind_then_arrive(conn):
    s = Scanner(conn)
    assert s("100001").outcome == "order_set"
    r = s(CARD_A, T0 + timedelta(seconds=5))
    assert (r.outcome, r.order_no, r.bunno) == ("bound", "100001", 1)
    assert s(CARD_A, T0 + timedelta(hours=3)).outcome == "arrived"


def test_card_held_after_binding_is_not_arrival(conn):
    s = Scanner(conn)
    s("100001")
    s(CARD_A)
    assert s(CARD_A, T0 + timedelta(seconds=3)).outcome == "debounced"
    assert in_inspection(conn, FakeSource(o=[order()]), TODAY) == []


def test_entered_bunno_is_used(conn):
    s = Scanner(conn)
    s("100001")
    assert s("3").bunno == 3
    r = s(CARD_A, T0 + timedelta(seconds=2))
    assert (r.outcome, r.bunno) == ("bound", 3)
    row = conn.execute("SELECT bunno_entered FROM bindings").fetchone()
    assert row["bunno_entered"] == 1


def test_skipped_bunno_is_marked_provisional(conn):
    s = Scanner(conn)
    s("100001")
    assert s(CARD_A).bunno == 1
    assert conn.execute("SELECT bunno_entered FROM bindings").fetchone()["bunno_entered"] == 0


def test_bunno_without_order_is_rejected(conn):
    assert Scanner(conn)("3").outcome == "no_order"


def test_same_bunno_on_other_card_is_rejected(conn):
    s = Scanner(conn)
    s("100001")
    s("3")
    s(CARD_A)
    s("100001", T0 + timedelta(seconds=20))
    s("3", T0 + timedelta(seconds=20))
    r = s(CARD_B, T0 + timedelta(seconds=21))
    assert r.outcome == "bunno_taken"
    assert conn.execute("SELECT COUNT(*) FROM bindings").fetchone()[0] == 1


def test_binding_twice_keeps_number(conn):
    s = Scanner(conn)
    s("100001")
    s(CARD_A, T0 + timedelta(seconds=1))
    s("100001", T0 + timedelta(seconds=2))
    r = s(CARD_A, T0 + timedelta(seconds=3))
    assert (r.outcome, r.bunno) == ("already_bound", 1)


def test_read_twice_debounces_and_no_new_number(conn):
    s = Scanner(conn)
    s("100001")
    s(CARD_A)
    at = T0 + timedelta(hours=1)
    assert s(CARD_A, at).outcome == "arrived"
    assert s(CARD_A, at + timedelta(seconds=2)).outcome == "debounced"
    assert s(CARD_A, at + timedelta(hours=1)).outcome == "already_in"
    assert conn.execute("SELECT COUNT(*) FROM bindings").fetchone()[0] == 1


def test_second_card_gets_next_number(conn):
    s = Scanner(conn)
    s("100001")
    s(CARD_A)
    s("100001", T0 + timedelta(seconds=9))
    assert s(CARD_B, T0 + timedelta(seconds=10)).bunno == 2


def test_rebind_closes_previous(conn):
    s = Scanner(conn)
    s("100001")
    s(CARD_A)
    s("100002", T0 + timedelta(seconds=9))
    r = s(CARD_A, T0 + timedelta(seconds=10))
    assert (r.order_no, r.bunno) == ("100002", 1)
    row = conn.execute("SELECT end_reason FROM bindings WHERE order_no='100001'").fetchone()
    assert row["end_reason"] == "rebound"


def test_expired_order_does_not_bind(conn):
    s = Scanner(conn)
    s("100001")
    assert s(CARD_A, T0 + timedelta(minutes=2)).outcome == "unbound"


def test_replayed_event_is_stored_once(conn):
    for _ in range(2):
        r = handle_scan(conn, event_id="same", raw="100001", device_id="d", read_at=T0)
    assert r.replayed
    assert conn.execute("SELECT COUNT(*) FROM reads").fetchone()[0] == 1


def test_devices_do_not_share_pending_order(conn):
    s = Scanner(conn)
    s("100001", device="d1")
    assert s(CARD_A, device="d2").outcome == "unbound"


def arrive(s, no="100001", card=CARD_A, days_ago=0):
    at = T0 - timedelta(days=days_ago)
    s(no, at)
    s(card, at + timedelta(seconds=1))
    s(card, at + timedelta(hours=1))


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
    assert s(CARD_A, T0 + timedelta(hours=9)).outcome == "unbound"


def test_order_detail_places(conn):
    s = Scanner(conn)
    arrive(s, "100001", CARD_A)
    s("100001", T0 + timedelta(hours=2))
    s(CARD_B, T0 + timedelta(hours=2, seconds=1))
    src = FakeSource(o=[order()], s=[Shipment(TODAY, "100001", 3, 10, "分")])
    places = {b["bunno"]: b["place"] for b in order_detail(conn, src, "100001")["bunnos"]}
    assert places == {1: "検査場", 2: "検査待ち", 3: "出荷済み"}


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
                       next_action="a", ask_role="検査場", group=""),
            TriageItem(anomaly_id="made-up", priority="様子見", likely_cause="c",
                       next_action="a", ask_role="検査場", group=""),
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
    s("100001", at)
    s(CARD_A, at)
    s(CARD_A, at + timedelta(minutes=5))
    assert in_inspection(conn, FakeSource(o=[order()]), TODAY)[0]["days"] == 0
