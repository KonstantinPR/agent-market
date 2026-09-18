import time

import requests


def _wait_done(api_client, job_id, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = api_client.get(f"/api/refresh/{job_id}").json()
        if st["status"] == "done":
            return st
        time.sleep(0.05)
    raise AssertionError("Задание не завершилось за отведённое время")


def test_refresh_wb_success(api_client):
    r = api_client.post("/api/refresh", params={"api": "wb",
                                                "date_from": "2026-09-01", "date_to": "2026-09-10"})
    assert r.status_code == 200
    body = r.json()
    assert body["queued"] is False

    st = _wait_done(api_client, body["job_id"])
    assert st["status"] == "done"
    assert st["ok"] == 6
    assert st["failed"] == 0
    assert {s["kind"] for s in st["steps"]} == {"cards", "stock", "funnel", "sales", "prices", "storage"}
    assert all(s["status"] == "ok" for s in st["steps"])

    pulls = api_client.get("/api/pulls").json()
    assert any(p["api"] == "wb" and p["kind"] == "sales" for p in pulls)

    hist = api_client.get("/api/refresh/history").json()["runs"]
    assert hist and hist[0]["status"] == "ok"
    assert hist[0]["results"][0]["kind"] == "cards"


def test_refresh_wb_with_detail_has_seven_steps(api_client):
    r = api_client.post("/api/refresh", params={"api": "wb", "detail": 1})
    body = r.json()
    st = _wait_done(api_client, body["job_id"])
    kinds = [s["kind"] for s in st["steps"]]
    assert kinds[-1] == "detail"
    assert len(kinds) == 7


def test_refresh_ozon_success(api_client):
    # Реализация Ozon тянутся помесячно за месяцы окна: берём один месяц — 2 строки.
    r = api_client.post("/api/refresh", params={"api": "ozon",
                                                "date_from": "2026-08-01", "date_to": "2026-08-31"})
    body = r.json()
    st = _wait_done(api_client, body["job_id"])
    assert st["status"] == "done"
    assert st["ok"] == 5
    assert st["failed"] == 0
    sales = api_client.get(
        "/api/sales", params={"marketplace": "ozon",
                              "date_from": "2026-07-01", "date_to": "2026-09-30"}
    ).json()
    assert sales["count"] == 2


def test_refresh_isolates_failed_step(api_client, stub_wb):
    stub_wb.prices_error = requests.HTTPError("429 Too Many Requests")
    r = api_client.post("/api/refresh", params={"api": "wb"})
    body = r.json()
    st = _wait_done(api_client, body["job_id"])
    assert st["status"] == "done"
    assert st["ok"] == 5
    assert st["failed"] == 1
    failed = next(s for s in st["steps"] if s["status"] == "failed")
    assert failed["kind"] == "prices"
    hist = api_client.get("/api/refresh/history").json()["runs"]
    assert hist[0]["status"] == "partial"


def test_refresh_invalid_api_and_date(api_client):
    assert api_client.post("/api/refresh", params={"api": "amazon"}).status_code == 400
    assert api_client.post("/api/refresh", params={"api": "wb",
                                                   "date_from": "не-дата"}).status_code == 400


def test_refresh_queues_second_job(api_client, stub_wb):
    import types

    orig = stub_wb.get_cards

    def slow(self):
        time.sleep(1.2)
        return orig()

    stub_wb.get_cards = types.MethodType(slow, stub_wb)

    r1 = api_client.post("/api/refresh", params={"api": "wb"}).json()
    assert r1["queued"] is False
    r2 = api_client.post("/api/refresh", params={"api": "wb"}).json()
    assert r2["queued"] is True

    st1 = _wait_done(api_client, r1["job_id"])
    st2 = _wait_done(api_client, r2["job_id"])
    assert st1["ok"] == 6
    assert st2["ok"] == 6
    assert st1["status"] == "done" and st2["status"] == "done"


def test_refresh_worker_fatal_error_releases_queue(api_client, monkeypatch):
    from app.providers import factory as provider_factory

    orig = provider_factory.get_wb_provider

    def boom(**kwargs):
        raise RuntimeError("провайдер упал на старте")

    monkeypatch.setattr(provider_factory, "get_wb_provider", boom)
    r1 = api_client.post("/api/refresh", params={"api": "wb"}).json()
    st1 = _wait_done(api_client, r1["job_id"])
    assert st1["status"] == "done"
    assert st1["failed"] >= 1

    # очередь освободилась: следующий запуск с рабочим провайдером проходит
    monkeypatch.setattr(provider_factory, "get_wb_provider", orig)
    r2 = api_client.post("/api/refresh", params={"api": "wb"}).json()
    st2 = _wait_done(api_client, r2["job_id"])
    assert st2["ok"] == 6


def test_refresh_state_missing_returns_404(api_client):
    assert api_client.get("/api/refresh/99999").status_code == 404