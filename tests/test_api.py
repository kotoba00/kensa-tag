from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from demo.seed import seed


@pytest.fixture
def client(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    monkeypatch.setenv("DB_PATH", str(db))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("AUTO_TRIAGE", "0")
    seed(db, date.today())
    from app.main import app

    return TestClient(app)


def test_demo_data_shows_all_kinds(client):
    kinds = {a["kind"] for a in client.get("/api/anomalies").json()["items"]}
    assert kinds == {
        "forgotten", "stagnant", "qty_mismatch", "bunno_total",
        "unrequested", "overdue_return", "request_unscanned",
    }


def test_naishoku_pending_then_request_moves_worker(client):
    data = client.get("/api/naishoku").json()
    assert data["workers"] == ["検査A", "検査B", "検査C"]
    assert [(p["order_no"], p["bunno"]) for p in data["pending"]] == [("100112", 1), ("100113", 1)]

    body = {"order_no": "100112", "bunno": 1, "worker": "検査B", "due_date": "2099-01-01", "qty": 300}
    assert client.post("/api/naishoku", json=body).status_code == 201
    assert client.post("/api/naishoku", json=body).status_code == 409
    assert [p["order_no"] for p in client.get("/api/naishoku").json()["pending"]] == ["100113"]

    item = next(i for i in client.get("/api/board").json()["items"] if i["order_no"] == "100112")
    assert (item["worker"], item["worker_due"]) == ("検査B", "2099-01-01")
    kinds = {a["id"] for a in client.get("/api/anomalies").json()["items"]}
    assert "unrequested:100112:1" not in kinds


def test_naishoku_rejects_unknown_worker(client):
    body = {"order_no": "100112", "bunno": 1, "worker": "だれか", "due_date": None}
    assert client.post("/api/naishoku", json=body).status_code == 409


def test_concurrent_requests_do_not_fail(client):
    urls = ["/api/inspection", "/api/anomalies", "/api/orders/100101"] * 20
    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(lambda u: client.get(u).status_code, urls))
    assert set(codes) == {200}


def test_scan_then_order_detail(client):
    at = datetime.now(timezone(timedelta(hours=9)))
    send = lambda e, raw, t, process: client.post(  # noqa: E731
        "/api/scan",
        json={"event_id": e, "raw": raw, "device_id": "t", "read_at": t.isoformat(), "process": process},
    ).json()
    assert send("a", "200001", at, "press")["outcome"] == "order_set"
    assert send("b", "04FFFFFFFFFFFF", at, "press")["outcome"] == "bound"
    assert send("c", "04FFFFFFFFFFFF", at, "elevator")["bunno"] == 1
    assert send("d", "04FFFFFFFFFFFF", at + timedelta(hours=1), "inspection")["outcome"] == "recorded"
    places = client.get("/api/orders/200001").json()["bunnos"]
    assert places[0]["place"] == "社内検査"


def test_reader_without_clock_sends_age(client):
    send = lambda e, raw, age: client.post(  # noqa: E731
        "/api/scan",
        json={"event_id": e, "raw": raw, "device_id": "atom", "age_ms": age, "process": "press"},
    ).json()
    assert send("x1", "200002", 5000)["outcome"] == "order_set"
    assert send("x2", "04EEEEEEEEEEEE", 0)["outcome"] == "bound"
    # 送り直しは同じ event_id で来る。経過時間が変わっても1回だけ記録する
    assert send("x2", "04EEEEEEEEEEEE", 9000)["replayed"] is True


def test_flow_lists_processes(client):
    body = client.get("/api/flow").json()
    assert body["checkpoint_name"] == "社内検査"
    assert [p["code"] for p in body["processes"]][:2] == ["press", "elevator"]


def test_triage_without_key_returns_readable_error(client):
    res = client.post("/api/triage")
    assert res.status_code == 503
    assert "キー" in res.json()["detail"]


def test_anomalies_report_triage_state(client):
    body = client.get("/api/anomalies").json()
    assert body["triage"] is None
    assert body["triage_stale"] is False and body["thinking"] is False


def test_tag_lookup_shows_order_and_place(client):
    # デモの 100103 カード8 はエレベーターを通って分納番号を持つ
    t = client.get("/api/tags/04A1B2C3D4E508").json()
    assert t["order_no"] == "100103" and t["bunno"] == 2
    assert t["place"] == "エレベーター" and t["in_use"] is True
    # 空白入りや小文字でも同じカードとして引ける
    assert client.get("/api/tags/04a1b 2c3d4e508").json()["bunno"] == 2


def test_tag_lookup_errors(client):
    assert client.get("/api/tags/04FFFFFFFFFFFF").status_code == 404
    assert client.get("/api/tags/100101").status_code == 400


def test_tag_page_is_served(client):
    res = client.get("/t/04A1B2C3D4E508")
    assert res.status_code == 200 and "/api/tags/" in res.text


def test_board_places_each_card_on_its_process(client):
    items = client.get("/api/board").json()["items"]
    by_card = {(i["order_no"], i["bunno"]): i for i in items}
    assert by_card[("100103", 2)]["process"] == "elevator"
    assert by_card[("100103", None)]["process"] == "press"
    # 滞留の検出が付いたものは色を変える
    assert by_card[("100101", 1)]["level"] in ("warn", "urgent")
    assert by_card[("100103", 1)]["level"] == "ok"
    assert client.get("/board").status_code == 200


def test_flow_has_map_zones(client):
    body = client.get("/api/flow").json()
    assert body["map"]["width"] > 0
    assert all(len(p["zone"]) == 4 for p in body["processes"])


def test_unknown_order_is_404(client):
    assert client.get("/api/orders/999999").status_code == 404
