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
    seed(db, date.today())
    from app.main import app

    return TestClient(app)


def test_demo_data_shows_all_four_kinds(client):
    kinds = {a["kind"] for a in client.get("/api/anomalies").json()["items"]}
    assert kinds == {"forgotten", "stagnant", "qty_mismatch", "bunno_total"}


def test_concurrent_requests_do_not_fail(client):
    urls = ["/api/inspection", "/api/anomalies", "/api/orders/100101"] * 20
    with ThreadPoolExecutor(8) as pool:
        codes = list(pool.map(lambda u: client.get(u).status_code, urls))
    assert set(codes) == {200}


def test_scan_then_order_detail(client):
    at = datetime.now(timezone(timedelta(hours=9)))
    send = lambda e, raw, t: client.post(  # noqa: E731
        "/api/scan",
        json={"event_id": e, "raw": raw, "device_id": "t", "read_at": t.isoformat()},
    ).json()
    assert send("a", "200001", at)["outcome"] == "order_set"
    assert send("b", "04FFFFFFFFFFFF", at)["bunno"] == 1
    assert send("c", "04FFFFFFFFFFFF", at + timedelta(hours=1))["outcome"] == "arrived"
    places = client.get("/api/orders/200001").json()["bunnos"]
    assert places[0]["place"] == "検査場"


def test_triage_without_key_returns_readable_error(client):
    res = client.post("/api/triage")
    assert res.status_code == 503
    assert "キー" in res.json()["detail"]


def test_unknown_order_is_404(client):
    assert client.get("/api/orders/999999").status_code == 404
