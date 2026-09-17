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


def test_wb_stock_fills_stocks_by_size(api_client):
    api_client.post("/api/wb/cards")
    r = api_client.post("/api/wb/stock")
    assert r.status_code == 200
    stocks = api_client.get("/api/stocks", params={"marketplace": "wb"}).json()
    assert stocks["count"] == 2
    by_card = {s["article"]: s for s in stocks["rows"]}
    assert by_card["TST-1"]["size"] == "46"
    assert by_card["TST-2"]["size"] == "47"
    assert by_card["TST-1"]["chrt_id"] == "101"
    assert by_card["TST-1"]["quantity"] == 5
    assert by_card["TST-1"]["quantity_full"] == 9
    assert by_card["TST-1"]["in_way"] == 3
    assert by_card["TST-2"]["quantity_full"] == 11


def test_wb_stock_excel_groups_without_sizes(api_client):
    api_client.post("/api/wb/cards")
    r = api_client.post("/api/wb/stock", params={"by_size": 0})
    assert r.status_code == 200
    df = pd.read_excel(r.content)
    assert set(df.columns) == {"date", "article", "warehouse", "quantity", "quantity_full", "in_way"}
    assert len(df) == 2  # агрегат по артикулу+склад
    assert df["quantity_full"].sum() == 20
    assert df["in_way"].sum() == 6


def test_wb_stock_json_only_updates_db_no_file(api_client):
    api_client.post("/api/wb/cards")
    r = api_client.post("/api/wb/stock", params={"excel": 0})
    assert r.status_code == 200
    assert "Content-Disposition" not in r.headers
    data = r.json()
    assert data["ok"] is True
    assert data["count"] == 2
    stocks = api_client.get("/api/stocks", params={"marketplace": "wb"}).json()
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


# ---------------------------------------------------------------------- цены и хранение
def test_wb_prices_snapshot_written_and_viewed(api_client, stub_wb):
    stub_wb.get_prices = lambda: pd.DataFrame({
        "nmID": ["1001", "1002"],
        "vendorCode": ["TST-1", "TST-2"],
        "techSizeName": ["46", "47"],
        "price": [1100, 990],
        "discountedPrice": [990, 891],
        "discount": [10, 10],
    })
    r = api_client.post("/api/wb/prices")
    assert r.status_code == 200
    assert r.headers["X-Count"] == "2"
    view = api_client.get("/api/prices").json()
    assert view["count"] == 2
    arts = {p["article"]: p for p in view["rows"]}
    assert set(arts) == {"TST-1", "TST-2"}
    assert arts["TST-1"]["price"] == 1100.0
    assert arts["TST-1"]["discount"] == 10.0
    assert arts["TST-1"]["size"] == "46"
    pulls = api_client.get("/api/pulls").json()
    assert next(p for p in pulls if p["api"] == "wb" and p["kind"] == "prices")["db_rows"] == 2


def test_wb_storage_cost_written_and_viewed(api_client):
    r = api_client.post("/api/wb/storage")
    assert r.status_code == 200
    assert r.headers["X-Count"] == "2"
    view = api_client.get("/api/storage-cost").json()
    assert view["count"] == 2
    rows = {x["article"]: x for x in view["rows"]}
    assert set(rows) == {"TST-1", "TST-2"}
    assert rows["TST-1"]["warehouse_price"] > 0
    assert rows["TST-1"]["storage_price"] > 0
    pulls = api_client.get("/api/pulls").json()
    assert next(p for p in pulls if p["api"] == "wb" and p["kind"] == "storage")["db_rows"] == 2


def test_write_db_off_prices_storage_does_not_write(api_client):
    r = api_client.post("/api/wb/prices", params={"write_db": 0})
    assert r.status_code == 200
    assert api_client.get("/api/prices").json()["count"] == 0
    r = api_client.post("/api/wb/storage", params={"write_db": 0})
    assert r.status_code == 200
    assert api_client.get("/api/storage-cost").json()["count"] == 0


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


# ---------------------------------------------------------------------- воронка без nmID
def test_funnel_without_nmid_column_uses_vendor_code(api_client, stub_wb):
    """Реальный WB-ответ может не содержать колонку nmID — не должно быть 502."""
    api_client.post("/api/wb/cards")  # товары TST-1/TST-2 для /api/margin/funnel
    stub_wb.get_sales_funnel = lambda from_, to_: pd.DataFrame({
        "vendorCode": ["TST-1", "TST-2"],
        "viewsCount": [100, 90],
        "openCardCount": [10, 9],
        "addToCartCount": [4, 3],
        "orderCount": [2, 2],
        "avgPrice": [1000, 900],
        "revenue": [2000, 1800],
    })
    r = api_client.post("/api/wb/funnel", params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    assert r.status_code == 200
    assert r.headers["X-Count"] == "2"
    assert XLSX in r.headers["content-type"]

    pulls = api_client.get("/api/pulls").json()
    assert any(p["api"] == "wb" and p["kind"] == "funnel" and p["db_rows"] == 2 for p in pulls)

    funnel = api_client.get("/api/margin/funnel",
                            params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    assert {row["article"] for row in funnel["rows"]} == {"TST-1", "TST-2"}


def test_funnel_accepts_alternate_nmid_casing(api_client, stub_wb):
    stub_wb.get_sales_funnel = lambda from_, to_: pd.DataFrame({
        "nmId": ["101", "102"],
        "viewsCount": [50, 40],
    })
    r = api_client.post("/api/wb/funnel", params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    assert r.status_code == 200
    assert r.headers["X-Count"] == "2"


def test_wb_detail_fills_margin_detail_view(api_client):
    api_client.post("/api/wb/cards")  # nm_articles/products для join
    r = api_client.post("/api/wb/detail",
                        params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    assert r.status_code == 200
    view = api_client.get("/api/margin/detail",
                          params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    assert view["count"] == 2
    assert {row["article"] for row in view["rows"]} == {"TST-1", "TST-2"}


def test_funnel_view_returns_loaded_rows(api_client):
    api_client.post("/api/wb/cards")  # nm_articles: 1001->TST-1 и т.д.
    r = api_client.post("/api/wb/funnel", params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    assert r.status_code == 200
    assert r.headers["X-Count"] == "2"

    view = api_client.get("/api/funnel",
                          params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    assert view["count"] == 2
    assert {row["article"] for row in view["rows"]} == {"TST-1", "TST-2"}
    assert all(row["views"] >= 0 for row in view["rows"])
    assert all("buyouts" in row and "buyout_sum" in row for row in view["rows"])


def test_funnel_view_totals_match_rows(api_client):
    api_client.post("/api/wb/cards")
    r = api_client.post("/api/wb/funnel", params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    assert r.status_code == 200

    view = api_client.get("/api/funnel",
                          params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    t = view["totals"]
    assert set(t) >= {"views", "opens", "adds", "orders", "buyouts", "revenue", "buyout_sum"}
    assert "avg_price" not in t
    for k in ("views", "opens", "adds", "orders", "buyouts"):
        got = round(sum(float(row[k]) for row in view["rows"]), 2)
        assert t[k] == got
    for k in ("revenue", "buyout_sum"):
        assert t[k] == round(sum(float(row[k]) for row in view["rows"]), 2)


def test_funnel_view_defaults_to_latest_snapshot(api_client):
    api_client.post("/api/wb/cards")
    api_client.post("/api/wb/funnel", params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    view = api_client.get("/api/funnel").json()
    assert view["date_from"] == "2026-09-01"
    assert view["date_to"] == "2026-09-10"
    assert view["count"] == 2


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


# ------------------------------------------------------------------ Яндекс.Диск
def test_yandex_requires_token(api_client, monkeypatch):
    from app.config import settings
    from app.services import yandex_disk

    monkeypatch.setattr(settings, "yandex_disk_token", "")
    r = api_client.get("/api/yandex/list")
    assert r.status_code == 400
    assert "не задан" in r.json()["detail"]


def test_yandex_upload_ok(api_client, monkeypatch):
    from app.config import settings
    from app.services import yandex_disk

    monkeypatch.setattr(settings, "yandex_disk_token", "tok")
    monkeypatch.setattr(yandex_disk, "upload_bytes",
                        lambda token, folder, name, data: {"ok": True, "folder": folder, "name": name, "size": len(data)})
    r = api_client.post("/api/yandex/upload", files={"file": ("wb_stock.xlsx", b"XLSX", XLSX)}, data={"folder": "/agent_market"})
    assert r.status_code == 200
    assert r.json()["name"] == "wb_stock.xlsx"


def test_yandex_list_ok(api_client, monkeypatch):
    from app.config import settings
    from app.services import yandex_disk

    monkeypatch.setattr(settings, "yandex_disk_token", "tok")
    monkeypatch.setattr(yandex_disk, "list_files", lambda token, folder: {"folder": folder, "files": []})
    r = api_client.get("/api/yandex/list")
    assert r.status_code == 200
    assert r.json()["files"] == []

# ------------------------------------------------------- экспорт разделов WB API (вьюхи)
def _read_xlsx(r):
    return pd.read_excel(io.BytesIO(r.content))


def test_export_wb_cards(api_client):
    api_client.post("/api/wb/cards")
    r = api_client.get("/api/export/wb/cards")
    assert r.status_code == 200
    assert XLSX in r.headers["content-type"]
    assert r.headers["X-Count"] == "2"
    df = _read_xlsx(r)
    assert list(df.columns) == ["Код размера", "Артикул WB", "Артикул продавца", "Бренд",
                                "Предмет", "Размер", "Баркод", "Объём, л", "Состав",
                                "Наименование"]
    assert set(df["Артикул продавца"]) == {"TST-1", "TST-2"}


def test_export_wb_cards_like_filters(api_client):
    api_client.post("/api/wb/cards")
    r = api_client.get("/api/export/wb/cards", params={"like": "TST-2"})
    assert r.status_code == 200
    assert r.headers["X-Count"] == "1"
    assert _read_xlsx(r)["Артикул продавца"].tolist() == ["TST-2"]


def test_export_wb_stock_by_size(api_client):
    api_client.post("/api/wb/cards")
    api_client.post("/api/wb/stock")
    r = api_client.get("/api/export/wb/stock", params={"by_size": 1})
    assert r.status_code == 200
    assert r.headers["X-Count"] == "2"
    assert XLSX in r.headers["content-type"]
    df = _read_xlsx(r)
    assert list(df.columns) == ["Дата", "Маркетплейс", "Артикул", "Наименование",
                                "Код размера", "Размер", "Баркод", "Склад", "Доступно",
                                "Всего на складах", "В пути"]
    assert df["Всего на складах"].sum() == 20


def test_export_wb_stock_agg(api_client):
    api_client.post("/api/wb/cards")
    api_client.post("/api/wb/stock")
    r = api_client.get("/api/export/wb/stock", params={"by_size": 0})
    assert r.status_code == 200
    assert r.headers["X-Count"] == "2"
    df = _read_xlsx(r)
    assert list(df.columns) == ["Дата", "Маркетплейс", "Артикул", "Наименование",
                                "Склад", "Доступно", "Всего на складах", "В пути"]
    assert df["Всего на складах"].sum() == 20


def test_export_wb_funnel(api_client):
    api_client.post("/api/wb/cards")
    api_client.post("/api/wb/funnel", params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    r = api_client.get("/api/export/wb/funnel", params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    assert r.status_code == 200
    assert r.headers["X-Count"] == "2"
    assert XLSX in r.headers["content-type"]
    df = _read_xlsx(r)
    assert set(df["Артикул"]) == {"TST-1", "TST-2"}
    assert df["Заказы"].sum() == 4


def test_export_wb_funnel_article_like(api_client):
    api_client.post("/api/wb/cards")
    api_client.post("/api/wb/funnel", params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    r = api_client.get("/api/export/wb/funnel", params={
        "date_from": "2026-09-01", "date_to": "2026-09-10", "article_like": "TST-2"})
    assert r.status_code == 200
    assert r.headers["X-Count"] == "1"
    assert _read_xlsx(r)["Артикул"].tolist() == ["TST-2"]


def test_export_wb_prices(api_client, stub_wb):
    stub_wb.get_prices = lambda: pd.DataFrame({
        "nmID": ["1001", "1002"], "vendorCode": ["TST-1", "TST-2"],
        "techSizeName": ["46", "47"], "price": [1100, 990],
        "discountedPrice": [990, 891], "discount": [10, 10],
    })
    api_client.post("/api/wb/cards")
    api_client.post("/api/wb/prices")
    r = api_client.get("/api/export/wb/prices")
    assert r.status_code == 200
    assert r.headers["X-Count"] == "2"
    assert XLSX in r.headers["content-type"]
    df = _read_xlsx(r)
    assert set(df["Артикул"]) == {"TST-1", "TST-2"}
    assert df["Цена без скидки"].tolist() == [1100, 990]


def test_export_wb_prices_article_like(api_client, stub_wb):
    stub_wb.get_prices = lambda: pd.DataFrame({
        "nmID": ["1001", "1002"], "vendorCode": ["TST-1", "TST-2"],
        "techSizeName": ["46", "47"], "price": [1100, 990],
        "discountedPrice": [990, 891], "discount": [10, 10],
    })
    api_client.post("/api/wb/cards")
    api_client.post("/api/wb/prices")
    r = api_client.get("/api/export/wb/prices", params={"article_like": "TST-2"})
    assert r.status_code == 200
    assert r.headers["X-Count"] == "1"
    assert _read_xlsx(r)["Артикул"].tolist() == ["TST-2"]


def test_export_wb_storage(api_client):
    api_client.post("/api/wb/cards")
    api_client.post("/api/wb/storage")
    r = api_client.get("/api/export/wb/storage")
    assert r.status_code == 200
    assert r.headers["X-Count"] == "2"
    assert XLSX in r.headers["content-type"]
    df = _read_xlsx(r)
    assert set(df["Артикул"]) == {"TST-1", "TST-2"}
    assert df["Сумма хранения"].tolist() == [5000.0, 5400.0]


def test_export_wb_storage_article_like(api_client):
    api_client.post("/api/wb/cards")
    api_client.post("/api/wb/storage")
    r = api_client.get("/api/export/wb/storage", params={"article_like": "TST-1"})
    assert r.status_code == 200
    assert r.headers["X-Count"] == "1"
    assert _read_xlsx(r)["Артикул"].tolist() == ["TST-1"]


def test_export_sales_article_like(api_client):
    api_client.post("/api/wb/cards")
    api_client.post("/api/wb/sales", params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    r = api_client.get("/api/export/sales", params={"article_like": "TST-2"})
    assert r.status_code == 200
    assert r.headers["X-Count"] == "1"
    df = _read_xlsx(r)
    assert df["Артикул"].tolist() == ["TST-2"]
    assert df["Продано, шт"].tolist() == [3]
