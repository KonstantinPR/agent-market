import io

import pandas as pd
import pytest
import requests

XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx(df: pd.DataFrame) -> bytes:
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    buf.seek(0)
    return buf.getvalue()


# ------------------------------------------------------------------ панельные загрузки
def test_wb_cards_returns_xlsx_and_persists(api_client):
    r = api_client.post("/api/wb/cards")
    assert r.status_code == 200
    assert r.headers["X-Count"] == "2"
    assert XLSX in r.headers["content-type"]

    products = api_client.get("/api/products").json()
    assert products["count"] == 2

    pulls = api_client.get("/api/pulls").json()
    assert any(p["api"] == "wb" and p["kind"] == "cards" for p in pulls)


def test_ozon_cards_maps_offer_id(api_client):
    r = api_client.post("/api/ozon/cards")
    assert r.status_code == 200
    articles = {p["article"] for p in api_client.get("/api/products").json()["rows"]}
    assert {"OZ-1", "OZ-2"} <= articles


def test_wb_sales_fills_sales_and_dashboard(api_client):
    api_client.post("/api/wb/cards")
    r = api_client.post("/api/wb/sales", params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    assert r.status_code == 200
    assert r.headers["X-Count"] == "2"

    sales = api_client.get("/api/sales", params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    assert sales["count"] == 2

    dash = api_client.get("/api/dashboard", params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    wb = next(m for m in dash["per_marketplace"] if m["marketplace"] == "wb")
    assert wb["sells"] == 5
    assert wb["income"] == 4500.0


def test_wb_stock_fills_stocks(api_client):
    api_client.post("/api/wb/cards")
    r = api_client.post("/api/wb/stock")
    assert r.status_code == 200
    stocks = api_client.get("/api/stocks").json()
    assert stocks["count"] == 2


def test_ozon_realization_fills_sales(api_client):
    api_client.post("/api/ozon/cards")
    r = api_client.post("/api/ozon/realization", params={"month": 8, "year": 2026})
    assert r.status_code == 200
    assert r.headers["X-Count"] == "2"
    sales = api_client.get(
        "/api/sales", params={"marketplace": "ozon",
                              "date_from": "2026-07-01", "date_to": "2026-09-30"}
    ).json()
    assert sales["count"] == 2


def test_pull_error_maps_to_status(api_client, stub_wb):
    stub_wb.prices_error = requests.HTTPError("boom")
    r = api_client.post("/api/wb/prices")
    assert r.status_code == 502


# ------------------------------------------------------ write_db (скачать без записи)
def test_write_db_off_cards_does_not_write(api_client):
    r = api_client.post("/api/wb/cards", params={"write_db": 0})
    assert r.status_code == 200
    assert r.headers["X-Count"] == "2"  # строки в файле всё равно есть
    assert api_client.get("/api/products").json()["count"] == 0
    pulls = api_client.get("/api/pulls").json()
    p = next(p for p in pulls if p["api"] == "wb" and p["kind"] == "cards")
    assert p["db_rows"] == 0


def test_write_db_off_sales_does_not_write(api_client):
    api_client.post("/api/wb/cards")  # товары нужны для join в /api/sales
    r = api_client.post("/api/wb/sales", params={"write_db": 0,
                                                 "date_from": "2026-09-01", "date_to": "2026-09-10"})
    assert r.status_code == 200
    assert r.headers["X-Count"] == "2"
    sales = api_client.get("/api/sales", params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    assert sales["count"] == 0
    pulls = api_client.get("/api/pulls").json()
    assert next(p for p in pulls if p["api"] == "wb" and p["kind"] == "sales")["db_rows"] == 0


def test_write_db_off_ozon_realization_does_not_write(api_client):
    r = api_client.post("/api/ozon/realization", params={"write_db": 0, "month": 8, "year": 2026})
    assert r.status_code == 200
    assert r.headers["X-Count"] == "2"
    sales = api_client.get(
        "/api/sales", params={"marketplace": "ozon",
                              "date_from": "2026-07-01", "date_to": "2026-09-30"}
    ).json()
    assert sales["count"] == 0


def test_write_db_on_by_default_writes(api_client):
    r = api_client.post("/api/wb/cards")
    assert r.status_code == 200
    assert api_client.get("/api/products").json()["count"] == 2


def test_invalid_date_returns_400(api_client):
    r = api_client.post("/api/wb/sales", params={"date_from": "нет-даты"})
    assert r.status_code == 400


# ---------------------------------------------------------------------- экспорт
def test_exports_are_xlsx(api_client):
    api_client.post("/api/wb/cards")
    api_client.post("/api/wb/sales", params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    api_client.post("/api/wb/funnel", params={"date_from": "2026-09-01", "date_to": "2026-09-10"})

    for path in ("/api/export/sales", "/api/export/margin",
                 "/api/export/margin/funnel", "/api/export/margin/detail"):
        r = api_client.get(path)
        assert r.status_code == 200
        assert XLSX in r.headers["content-type"]


# ---------------------------------------------------------------------- импорт
def test_import_products(api_client):
    df = pd.DataFrame([
        {"Артикул": "A1", "Наименование": "Первый", "Себестоимость": "120"},
        {"Артикул": "A2", "Наименование": "Второй", "Себестоимость": "80"},
    ])
    r = api_client.post("/api/import/products", files={"file": ("p.xlsx", _xlsx(df), XLSX)})
    assert r.status_code == 200
    assert r.json()["imported"] == 2
    products = api_client.get("/api/products").json()
    assert products["count"] == 2
    assert all(p["net_cost"] > 0 for p in products["rows"])


def test_import_net_cost_updates_and_counts(api_client):
    api_client.post("/api/import/products", files={
        "file": ("p.xlsx", _xlsx(pd.DataFrame([
            {"Артикул": "A1", "Наименование": "Первый", "Себестоимость": "1"},
        ])), XLSX)})
    r = api_client.post("/api/import/net-cost", files={
        "file": ("n.xlsx", _xlsx(pd.DataFrame([
            {"article": "A1", "net_cost": "250"},
        ])), XLSX)})
    assert r.status_code == 200
    body = r.json()
    assert body["with_cost_total"] == 1
    a1 = next(p for p in api_client.get("/api/products").json()["rows"] if p["article"] == "A1")
    assert a1["net_cost"] == 250.0


def test_import_custom_stock(api_client):
    r = api_client.post("/api/import/custom-stock", files={
        "file": ("c.xlsx", _xlsx(pd.DataFrame([
            {"Артикул": "A1", "Количество": "10", "Себестоимость": "55"},
        ])), XLSX)})
    assert r.status_code == 200
    assert r.json()["imported"] == 1