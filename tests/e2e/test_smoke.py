"""Сквозная проверка (e2e): тестовая БД + стабы провайдеров.

Повторяет бывший smoke-скрипт: полный цикл панельных загрузок WB и Ozon,
массовое обновление, историю запусков — «как из UI» через TestClient.
"""
import time

import pytest

pytestmark = pytest.mark.e2e


def _wait_done(api_client, job_id, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = api_client.get(f"/api/refresh/{job_id}").json()
        if st["status"] == "done":
            return st
        time.sleep(0.05)
    raise AssertionError("Задание не завершилось за отведённое время")


def test_full_panel_and_refresh_cycle(api_client):
    # панельные загрузки
    for path in ("/api/wb/cards", "/api/wb/stock", "/api/wb/prices",
                 "/api/wb/storage", "/api/ozon/cards", "/api/ozon/stock",
                 "/api/ozon/prices"):
        assert api_client.post(path).status_code == 200
    for path in ("/api/wb/funnel", "/api/wb/sales", "/api/ozon/cashflow"):
        r = api_client.post(path, params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
        assert r.status_code == 200
    assert api_client.post("/api/ozon/realization", params={"month": 8, "year": 2026}).status_code == 200

    # массовое обновление обоих маркетплейсов
    for api in ("wb", "ozon"):
        body = api_client.post("/api/refresh", params={"api": api}).json()
        st = _wait_done(api_client, body["job_id"])
        assert st["status"] == "done"
        assert st["failed"] == 0
        # все шаги плана должны пройти (состав плана меняется вместе с pulls)
        assert st["ok"] == len(st["steps"]), f"{api}: ok={st['ok']} из {len(st['steps'])}"
        assert st["ok"] >= 5, f"{api}: слишком мало шагов — {st['ok']}"

    # история запусков привязана к БД
    runs = api_client.get("/api/refresh/history").json()["runs"]
    assert len(runs) >= 2

    # продажи и дашборд видят данные обоих маркетплейсов
    sales = api_client.get("/api/sales", params={"date_from": "2026-08-01", "date_to": "2026-09-30"}).json()
    assert sales["count"] > 0
    dash = api_client.get("/api/dashboard",
                          params={"date_from": "2026-08-01", "date_to": "2026-09-30"}).json()
    assert {m["marketplace"] for m in dash["per_marketplace"]} == {"wb", "ozon"}

    # экспорт маржи отдаёт файл
    r = api_client.get("/api/export/margin")
    assert r.status_code == 200