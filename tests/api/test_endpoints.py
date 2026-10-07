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


def test_ozon_cards_fill_marketplace_cards(api_client):
    r = api_client.post("/api/ozon/cards")
    assert r.status_code == 200
    view = api_client.get("/api/cards", params={"marketplace": "ozon"}).json()
    assert view["total"] > 0
    assert all(str(c["vendor_code"]).startswith("OZ-") for c in view["rows"])
    assert all(c["chrt_id"] for c in view["rows"])


def test_ozon_prices_view_and_export_marketplace_filter(api_client):
    api_client.post("/api/ozon/cards")
    r = api_client.post("/api/ozon/prices")
    assert r.status_code == 200
    view = api_client.get("/api/prices", params={"marketplace": "ozon"}).json()
    assert view["count"] >= 1
    assert all(str(p["article"]).startswith("OZ-") for p in view["rows"])
    wb_view = api_client.get("/api/prices").json()
    assert {p["article"] for p in wb_view["rows"]}.isdisjoint(
        {p["article"] for p in view["rows"]})
    r = api_client.get("/api/export/wb/prices", params={"marketplace": "ozon"})
    assert r.status_code == 200
    assert r.headers["X-Count"] == str(view["count"])
    df = _read_xlsx(r)
    assert set(df["Артикул"]) == {p["article"] for p in view["rows"]}


def test_ozon_endpoints_excel_zero_only_update_db(api_client):
    """jsonMode «Обновить базу»: файл не скачивается, данные пишутся в БД."""
    for kind, url in [("cards", "/api/ozon/cards"),
                      ("stock", "/api/ozon/stock"),
                      ("prices", "/api/ozon/prices")]:
        r = api_client.post(url, params={"excel": 0})
        assert r.status_code == 200
        assert "Content-Disposition" not in r.headers
        data = r.json()
        assert data["ok"] is True
        assert data["count"] >= 1, kind


def test_ozon_realization_by_window_json_mode(api_client):
    """Реализация тянется за месяцы окна из шапки (excel=0 → json)."""
    api_client.post("/api/ozon/cards")
    r = api_client.post("/api/ozon/realization", params={
        "date_from": "2026-08-01", "date_to": "2026-09-30", "excel": 0,
    })
    assert r.status_code == 200
    assert "Content-Disposition" not in r.headers
    data = r.json()
    assert data["ok"] is True
    assert data["count"] >= 1

    sales = api_client.get("/api/sales", params={
        "marketplace": "ozon", "date_from": "2026-07-01", "date_to": "2026-10-31",
    }).json()
    assert sales["count"] >= 2  # месяцы 2026-08 и 2026-09 по 2 строки

    pulls = api_client.get("/api/pulls").json()
    p = next(p for p in pulls if p["api"] == "ozon" and p["kind"] == "realization")
    assert p["window"] == "2026-08-01..2026-09-30"


def test_stocks_marketplace_uses_own_latest_date(api_client, db):
    """Остатки по маркетплейсу берут свой максимум даты, а не глобальный."""
    from app.services import sync as sync_service
    sync_service.upsert_stocks(db, pd.DataFrame({
        "date": ["2026-09-02"], "article": ["WB-L"], "warehouse": ["Склад 1"],
        "quantity": [1], "quantity_full": [1], "in_way": [0],
    }), "wb")
    sync_service.upsert_stocks(db, pd.DataFrame({
        "date": ["2026-09-01"], "article": ["OZ-L"], "warehouse": ["FBO"],
        "quantity": [2], "quantity_full": [2], "in_way": [0],
    }), "ozon")

    global_view = api_client.get("/api/stocks").json()
    assert global_view["date"] == "2026-09-02"

    oz = api_client.get("/api/stocks", params={"marketplace": "ozon"}).json()
    assert oz["date"] == "2026-09-01"
    assert oz["count"] == 1
    assert oz["rows"][0]["article"] == "OZ-L"

    wb = api_client.get("/api/stocks", params={"marketplace": "wb"}).json()
    assert wb["date"] == "2026-09-02"
    assert {r["article"] for r in wb["rows"]} == {"WB-L"}


def test_stocks_marketplace_latest_export(api_client, db):
    """Экспорт остатков тоже показывает свой последний срез маркетплейса."""
    from app.services import sync as sync_service
    sync_service.upsert_stocks(db, pd.DataFrame({
        "date": ["2026-09-02"], "article": ["WB-L"], "warehouse": ["Склад 1"],
        "quantity": [1], "quantity_full": [1], "in_way": [0],
    }), "wb")
    sync_service.upsert_stocks(db, pd.DataFrame({
        "date": ["2026-09-01"], "article": ["OZ-L"], "warehouse": ["FBO"],
        "quantity": [2], "quantity_full": [2], "in_way": [0],
    }), "ozon")
    r = api_client.get("/api/export/wb/stock", params={"marketplace": "ozon"})
    assert r.status_code == 200
    df = _read_xlsx(r)
    assert set(df["Артикул"]) == {"OZ-L"}
    assert str(df["Дата"].iloc[0]) == "2026-09-01"


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

    for path in ("/api/export/sales", "/api/export/margin/funnel",
                 "/api/export/margin/detail"):
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


def test_funnel_view_returns_extended_fields(api_client, db):
    from datetime import date as _date

    from app import models

    db.add_all([
        models.FunnelMetric(
            date_from=_date(2026, 9, 1), date_to=_date(2026, 9, 10),
            nm_id="1001", article="TST-1", views=100, orders=4, revenue=12000,
            subject_name="Куртки", brand_name="Бренд A", product_rating=8.2,
            feedback_rating=4.6, stock_wb=12, stock_mp=3,
            stock_balance_sum=36000, cancel_sum=500, add_to_wishlist=8,
            time_to_ready_min=1710, conv_to_cart_percent=15.0,
            conv_buyout_percent=75.0, wb_club_order_count=1, wb_club_order_sum=3000,
        ),
    ])
    db.commit()

    view = api_client.get("/api/funnel",
                          params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    assert view["count"] == 1
    row = view["rows"][0]
    assert row["subject_name"] == "Куртки"
    assert row["brand_name"] == "Бренд A"
    assert row["product_rating"] == 8.2
    assert row["feedback_rating"] == 4.6
    assert row["stock_wb"] == 12
    assert row["time_to_ready_min"] == 1710
    assert row["wb_club_order_count"] == 1
    assert row["wb_club_order_sum"] == 3000.0
    assert view["totals"]["stock_wb"] == 12
    assert "share_order_percent" not in view["totals"]


def test_funnel_view_returns_past_and_dynamics_fields(api_client, db):
    """Полный отчёт WB: title/subject_id/tags + его прошлый период (past_*) и
    динамика к нему (dy_*) читаются из JSON-блоков statistic.past/comparison."""
    from datetime import date as _date
    import json

    from app import models

    db.add_all([
        models.FunnelMetric(
            date_from=_date(2026, 9, 1), date_to=_date(2026, 9, 10),
            nm_id="1001", article="TST-1", views=100, orders=4, revenue=12000,
            title="Пальто LQ", subject_id="100", tags="новинка, sale",
            past_json=json.dumps({
                "openCount": 70, "cartCount": 3, "orderCount": 2,
                "cancelCount": 0, "buyoutCount": 1,
                "orderSum": 6000, "buyoutSum": 3000, "cancelSum": 0,
                "avgPrice": 3000,
            }),
            comparison_json=json.dumps({
                "openCountDynamic": 42.86, "cartCountDynamic": 33.33,
                "orderCountDynamic": 100.0, "cancelCountDynamic": 0,
                "buyoutCountDynamic": 0.0, "orderSumDynamic": 100.0,
                "avgPriceDynamic": 0.0,
            }),
        ),
    ])
    db.commit()

    row = api_client.get("/api/funnel",
                         params={"date_from": "2026-09-01", "date_to": "2026-09-10"}
                         ).json()["rows"][0]
    assert row["title"] == "Пальто LQ"
    assert row["subject_id"] == "100"
    assert row["tags"] == "новинка, sale"
    assert row["past_views"] == 70
    assert row["past_adds"] == 3
    assert row["past_orders"] == 2
    assert row["past_buyouts"] == 1
    assert row["past_revenue"] == 6000.0
    assert row["past_buyout_sum"] == 3000.0
    assert row["past_avg_price"] == 3000.0
    assert row["dy_views"] == 42.86
    assert row["dy_orders"] == 100.0
    assert row["dy_avg_price"] == 0.0
    assert "past_json" not in row and "comparison_json" not in row
    totals = api_client.get("/api/funnel",
                            params={"date_from": "2026-09-01", "date_to": "2026-09-10"}
                            ).json()["totals"]
    assert "dy_orders" not in totals
    assert "past_avg_price" not in totals
    assert totals["past_orders"] == 2


def test_funnel_to_db_stores_all_api_fields():
    """_funnel_to_db протаскивает title/subjectId/tags и блоки past/comparison."""
    import json

    from app.services.refresh import _funnel_to_db
    from datetime import date as _date

    df = pd.DataFrame({
        "product.nmID": ["1001"],
        "product.vendorCode": ["TST-1"],
        "product.title": ["Пальто LQ"],
        "product.subjectId": [100],
        "product.tags": [["новинка", "sale"]],
        "statistic.selected.openCount": [100],
        "statistic.past.openCount": [70],
        "statistic.comparison.openCountDynamic": [42.86],
    })
    raw = [{
        "product": {"nmID": 1001, "vendorCode": "TST-1", "title": "Пальто LQ",
                    "subjectId": 100, "tags": ["новинка", "sale"]},
        "statistic": {
            "selected": {"openCount": 100},
            "past": {"openCount": 70},
            "comparison": {"openCountDynamic": 42.86},
        },
    }]
    df["_raw"] = raw
    out = _funnel_to_db(df, _date(2026, 9, 1), _date(2026, 9, 10))
    r = out.iloc[0]
    assert r["title"] == "Пальто LQ"
    assert r["subject_id"] == "100"
    assert r["tags"] == "новинка, sale"
    past = json.loads(r["past_json"])
    assert past["openCount"] == 70
    comp = json.loads(r["comparison_json"])
    assert comp["openCountDynamic"] == 42.86


def test_wb_detail_fills_margin_detail_view(api_client):
    api_client.post("/api/wb/cards")  # nm_articles/products для join
    r = api_client.post("/api/wb/detail",
                        params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    assert r.status_code == 200
    view = api_client.get("/api/margin/detail",
                          params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    assert view["count"] == 2
    assert {row["article"] for row in view["rows"]} == {"TST-1", "TST-2"}


def test_margin_detail_includes_stock_columns(api_client, db):
    """Детализация WB отдаёт остатки со среза стоков на конец окна."""
    from datetime import date

    from app import models as app_models
    from app.services.sync import marketplace_id
    api_client.post("/api/wb/cards")
    r = api_client.post("/api/wb/detail",
                        params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    assert r.status_code == 200
    wb_id = marketplace_id(db, "wb")
    db.add_all([
        app_models.Stock(marketplace_id=wb_id, date=date(2026, 9, 10), article="tst-1",
                         warehouse="WH1", chrt_id="1", quantity=4, quantity_full=6, in_way=2),
    ])
    db.commit()
    view = api_client.get("/api/margin/detail",
                          params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    row = next(x for x in view["rows"] if x["article"].upper() == "TST-1")
    assert row["stock_qty"] == 4
    assert row["stock_total"] == 6
    assert row["stock_in_way"] == 2
    assert view["totals"]["stock_qty"] == 4
    assert view["totals"]["stock_total"] == 6
    assert view["totals"]["stock_in_way"] == 2
    assert isinstance(view["totals"].get("margin_per_one"), dict)
    assert "avg" in view["totals"]["margin_per_one"]


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


def test_funnel_view_falls_back_to_latest_snapshot_for_wider_window(api_client):
    api_client.post("/api/wb/cards")
    api_client.post("/api/wb/funnel", params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    view = api_client.get("/api/funnel",
                          params={"date_from": "2026-08-20", "date_to": "2026-09-17"}).json()
    assert view["count"] == 2
    assert view["date_from"] == "2026-08-20"
    assert view["date_to"] == "2026-09-17"
    # среза с точным периодом нет — показывается последний, с пометкой о периоде
    assert view["snapshot_from"] == "2026-09-01"
    assert view["snapshot_to"] == "2026-09-10"
    assert view["matched"] is False
    assert {row["article"] for row in view["rows"]} == {"TST-1", "TST-2"}
    assert all(row["date_from"] == "2026-09-01" for row in view["rows"])


def test_funnel_view_picks_widest_snapshot_inside_window(api_client):
    api_client.post("/api/wb/cards")
    api_client.post("/api/wb/funnel", params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    api_client.post("/api/wb/funnel", params={"date_from": "2026-09-05", "date_to": "2026-09-12"})
    # запрошен 08-30..09-12: оба среза целиком внутри, выбирается самый широкий (09-01..09-10)
    view = api_client.get("/api/funnel",
                          params={"date_from": "2026-08-30", "date_to": "2026-09-12"}).json()
    assert view["snapshot_from"] == "2026-09-01"
    assert view["snapshot_to"] == "2026-09-10"
    assert view["matched"] is False
    assert all(row["date_from"] == "2026-09-01" for row in view["rows"])


def test_funnel_view_picks_newest_overlapping_snapshot(api_client):
    api_client.post("/api/wb/cards")
    api_client.post("/api/wb/funnel", params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    api_client.post("/api/wb/funnel", params={"date_from": "2026-09-05", "date_to": "2026-09-12"})
    # запрошен 09-09..09-15: ни один срез не лежит целиком внутри; выбран самый свежий пересекающийся (09-05..09-12)
    view = api_client.get("/api/funnel",
                          params={"date_from": "2026-09-09", "date_to": "2026-09-15"}).json()
    assert view["snapshot_from"] == "2026-09-05"
    assert view["snapshot_to"] == "2026-09-12"
    assert view["matched"] is False


def test_funnel_view_exact_window_is_matched(api_client):
    api_client.post("/api/wb/cards")
    r = api_client.post("/api/wb/funnel", params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    assert r.status_code == 200
    view = api_client.get("/api/funnel",
                          params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    assert view["matched"] is True
    assert view["snapshot_from"] == view["date_from"] == "2026-09-01"
    assert view["snapshot_to"] == view["date_to"] == "2026-09-10"


def test_funnel_view_empty_without_any_snapshot(api_client):
    view = api_client.get("/api/funnel",
                          params={"date_from": "2026-08-01", "date_to": "2026-08-30"}).json()
    assert view["count"] == 0
    assert view["totals"] == {}


def test_margin_funnel_full_fields_and_matched(api_client):
    """Прибыльность → Воронка: весь набор полей funnel_metric, matched=true при
    точном окне, запрошенный период и срез совпадают."""
    api_client.post("/api/wb/cards")
    api_client.post("/api/wb/funnel", params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    view = api_client.get("/api/margin/funnel",
                          params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    assert view["matched"] is True
    assert view["snapshot_from"] == view["date_from"] == "2026-09-01"
    assert view["snapshot_to"] == view["date_to"] == "2026-09-10"
    assert view["count"] == 2
    row = view["rows"][0]
    assert row["article"] in {"TST-1", "TST-2"} and row["name"]
    for col in ("title", "subject_name", "brand_name", "product_rating", "stock_wb",
                "conv_to_cart_percent", "conv_cart_to_order_percent",
                "conv_buyout_percent", "wb_club_buyout_percent", "buyout_sum",
                "avg_orders_per_day", "storage_est", "margin", "margin_pct",
                "past_views", "dy_orders"):
        assert col in row, col


def test_margin_funnel_picks_widest_snapshot_inside_window(api_client):
    """Прибыльность → Воронка использует тот же подбор среза, что и Воронка WB API."""
    api_client.post("/api/wb/cards")
    api_client.post("/api/wb/funnel", params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    api_client.post("/api/wb/funnel", params={"date_from": "2026-09-05", "date_to": "2026-09-12"})
    view = api_client.get("/api/margin/funnel",
                          params={"date_from": "2026-08-30", "date_to": "2026-09-12"}).json()
    assert view["count"] == 2
    assert view["snapshot_from"] == "2026-09-01"
    assert view["snapshot_to"] == "2026-09-10"
    assert view["matched"] is False
    assert view["date_from"] == "2026-08-30"
    assert view["date_to"] == "2026-09-12"


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
                                "Предмет", "Размер", "Баркод", "Объём, л.", "Состав",
                                "Название"]
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
    assert df["Цена без скид."].tolist() == [1100, 990]


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


# ------------------------------------------------------------------ Ozon: детализация + выкупы
def test_ozon_detail_fills_rows_and_summary(api_client):
    api_client.post("/api/ozon/cards")
    r = api_client.post("/api/ozon/detail",
                        params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    assert r.status_code == 200
    assert r.headers["X-Count"] == "2"
    assert XLSX in r.headers["content-type"]

    rows = api_client.get("/api/ozon/detail-rows",
                          params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    assert rows["total"] == 2

    summ = api_client.get("/api/ozon/detail-summary",
                          params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    # по умолчанию свёрнуто по товару: OZ-1 и OZ-2 — размеры 1 и 2 артикула OZ
    assert summ["count"] == 1
    o1 = summ["rows"][0]
    assert o1["article"] == "OZ"
    assert o1["sells"] == 3
    assert o1["income"] == 2980.0
    assert o1["sizes_count"] == 2
    assert o1["offers_count"] == 2
    # «в разрезе размеров» — каждая карточка отдельной строкой
    by_size = api_client.get("/api/ozon/detail-summary", params={
        "date_from": "2026-09-01", "date_to": "2026-09-10", "by_size": 1}).json()
    assert by_size["count"] == 2
    assert {r["article"]: r["size"] for r in by_size["rows"]} == {"OZ-1": "1", "OZ-2": "2"}

    pulls = api_client.get("/api/pulls").json()
    assert any(p["api"] == "ozon" and p["kind"] == "detail" for p in pulls)


def test_ozon_buyout_fills_rows(api_client):
    r = api_client.post("/api/ozon/buyout",
                        params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    assert r.status_code == 200
    assert r.headers["X-Count"] == "2"
    assert XLSX in r.headers["content-type"]

    rows = api_client.get("/api/ozon/buyout-rows").json()
    assert rows["total"] == 2

    pulls = api_client.get("/api/pulls").json()
    assert any(p["api"] == "ozon" and p["kind"] == "buyout" for p in pulls)


def test_ozon_detail_excel_zero_only_updates_db(api_client):
    r = api_client.post("/api/ozon/detail",
                        params={"date_from": "2026-09-01", "date_to": "2026-09-10",
                                "excel": 0})
    assert r.status_code == 200
    assert "Content-Disposition" not in r.headers
    data = r.json()
    assert data["ok"] is True
    assert data["rows"] == 2
    # свод по умолчанию — одна строка на товар (OZ-1 и OZ-2 = размеры 1 и 2)
    assert api_client.get("/api/ozon/detail-summary").json()["count"] == 1
    assert api_client.get(
        "/api/ozon/detail-summary", params={"by_size": 1}).json()["count"] == 2


def test_export_ozon_detail_summary_and_rows(api_client):
    api_client.post("/api/ozon/detail",
                    params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    r = api_client.get("/api/export/ozon/detail-summary",
                       params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    assert r.status_code == 200
    df = _read_xlsx(r)
    assert set(df["Артикул"]) == {"OZ"}
    assert "К перечислению" in df.columns

    r = api_client.get("/api/export/ozon/detail-summary",
                       params={"date_from": "2026-09-01", "date_to": "2026-09-10",
                               "by_size": 1})
    df_bs = _read_xlsx(r)
    assert set(df_bs["Артикул"]) == {"OZ-1", "OZ-2"}
    assert dict(zip(df_bs["Артикул"], df_bs["Размер"].astype(str))) == {"OZ-1": "1", "OZ-2": "2"}

    r = api_client.get("/api/export/ozon/detail-rows",
                       params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    assert r.status_code == 200
    df2 = _read_xlsx(r)
    assert len(df2) == 2
    assert df2["Постинг"].tolist() == ["PZ-1", "PZ-2"]


def test_ozon_accrual_pull_rows_and_export(api_client):
    """Pull начислений через API, чтение строк, экспорт в Excel."""
    api_client.post("/api/ozon/cards")
    api_client.post("/api/ozon/detail",
                    params={"date_from": "2026-09-01", "date_to": "2026-09-10", "excel": 0})
    r = api_client.post("/api/ozon/accrual",
                        params={"date_from": "2026-09-01", "date_to": "2026-09-10",
                                "excel": 0})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["rows"] == 8  # 8 фиктивных начислений за D1

    rows = api_client.get("/api/ozon/accrual-rows",
                          params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    assert rows["count"] == 8
    # offer_id дорезолвился по детализации (sku 3001/3002 → OZ-1/OZ-2);
    # «other» (NON_ITEM) сквозной мок имеет пустой sku — без привязки к товару
    nonempty = {x["sku"] for x in rows["rows"] if x["sku"]}
    assert nonempty == {"3001", "3002"}

    buckets = {x["bucket"] for x in rows["rows"]}
    assert buckets == {"sale", "commission", "logistics", "services", "other"}
    sale = next(x for x in rows["rows"] if x["bucket"] == "sale")
    assert sale["quantity"] == 2
    assert sale["amount"] == 2560.0

    r = api_client.get("/api/export/ozon/accrual-rows",
                       params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    assert r.status_code == 200
    out = _read_xlsx(r)
    assert len(out) == 8
    assert "Сумма" in out.columns


def test_ozon_empty_views_report_window_and_coverage(api_client):
    """Пустой раздел должен объясняться фактами: окно запроса + покрытие базы.

    Раньше текст «период с данными: 2026-02-21 … 2026-08-30» был зашит в JS и
    не зависел ни от выбранного периода, ни от базы.
    """
    win = {"date_from": "2026-09-01", "date_to": "2026-09-10"}
    api_client.post("/api/ozon/cards")
    api_client.post("/api/ozon/detail", params=dict(win, excel=0))

    for path in ("/api/margin/ozon-detail",
                 "/api/ozon/detail-rows",
                 "/api/ozon/detail-summary",
                 "/api/ozon/accrual-rows"):
        d = api_client.get(path, params=win).json()
        assert d["window"]["date_from"] == "2026-09-01", path
        assert d["window"]["date_to"] == "2026-09-10", path
        rng = d["detail_range"]
        assert set(rng) == {"date_from", "date_to", "rows"}, path

    # покрыто — с датами; пусто — rows == 0
    d = api_client.get("/api/margin/ozon-detail", params=win).json()
    assert d["detail_range"]["date_from"] == "2026-09-01"
    assert d["detail_range"]["rows"] > 0

    empty = api_client.get("/api/margin/ozon-detail",
                           params={"date_from": "2020-01-01", "date_to": "2020-01-31"}).json()
    assert empty["rows"] == []
    assert empty["detail_range"]["rows"] > 0, "в базе есть данные, окно — другое"

    for path in ("/api/ozon/accrual-rows", "/api/ozon/cashflow-rows",
                 "/api/ozon/placement-rows", "/api/ozon/placement-summary"):
        d = api_client.get(path, params=win).json()
        assert d["rows"] == [], path
        assert d["detail_range"]["rows"] == 0, path + ": таблица пуста, покрытия нет"


def test_margin_ozon_detail_view(api_client):
    """Прибыльность Ozon из ozon_detail_rows: income − себестоимость×продано."""
    api_client.post("/api/ozon/cards")
    api_client.post("/api/ozon/detail",
                    params={"date_from": "2026-09-01", "date_to": "2026-09-10", "excel": 0})
    view = api_client.get("/api/margin/ozon-detail",
                          params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    by = {x["article"]: x for x in view["rows"]}
    # по умолчанию одна строка на товар: OZ-1 (2 шт) и OZ-2 (1 шт) → база OZ
    o1 = by["OZ"]
    assert o1["sizes_count"] == 2 and o1["offers_count"] == 2
    assert o1["sells"] == 3
    assert o1["revenue"] == 3500.0
    assert o1["commission"] == -361.6
    assert o1["services"] == -58.4
    assert o1["income"] == 2980.0
    assert o1["margin"] == pytest.approx(2980.0 - 3 * 500.0)
    assert o1["margin_pct"] == pytest.approx((2980.0 - 1500.0) / 3500.0 * 100.0)
    assert view["estimated"] == 1  # строка одна, без себестоимости в product
    assert "totals" in view and view["totals"].get("income") == pytest.approx(2980.0)
    # движение средств в тестовой БД пусто → фактически получено 0, оценка колонки → 0
    assert view["cashflow_received"] == 0.0
    assert view["cashflow_ratio"] == 0.0
    assert o1["cashflow_est"] == 0.0
    assert view["totals"].get("cashflow_est") == pytest.approx(0.0)
    # начислений в тестовой БД нет → неточные колонки нулевые, артикулов 0
    assert view["accrued_total"] == 0.0
    assert view["accrued_rows"] == 0
    assert view["accrued_other"] == 0.0
    assert view["accrued_unmapped"] == 0.0
    assert o1["accrued_net"] == 0.0
    assert o1["accrued_coverage"] == 0
    assert o1["margin_accrued"] in (None, "")  # нет начислений → пусто


def test_margin_ozon_detail_by_size_view(api_client):
    """by_size=1 — прежнее поведение: строка на артикул конкретного размера."""
    api_client.post("/api/ozon/cards")
    api_client.post("/api/ozon/detail",
                    params={"date_from": "2026-09-01", "date_to": "2026-09-10", "excel": 0})
    view = api_client.get("/api/margin/ozon-detail", params={
        "date_from": "2026-09-01", "date_to": "2026-09-10", "by_size": 1}).json()
    by = {x["article"]: x for x in view["rows"]}
    assert set(by) == {"OZ-1", "OZ-2"}
    assert by["OZ-1"]["size"] == "1"
    assert by["OZ-1"]["sells"] == 2
    assert by["OZ-1"]["income"] == 2280.0
    assert by["OZ-1"]["margin"] == pytest.approx(2280.0 - 2 * 500.0)
    assert view["estimated"] == 2


def test_margin_ozon_detail_accrual_reconciliation(api_client, db):
    """Сверка начислений: сумма по артикулам + нераспределённые = итог за окно."""
    from datetime import date
    from app import models
    api_client.post("/api/ozon/cards")
    api_client.post("/api/ozon/detail",
                    params={"date_from": "2026-09-01", "date_to": "2026-09-10", "excel": 0})
    api_client.post("/api/ozon/accrual",
                    params={"date_from": "2026-09-01", "date_to": "2026-09-10", "excel": 0})
    # прочие (NON_ITEM, без sku) и строка со sku, но без артикула
    db.add(models.OzonAccrual(
        op_key="extra-other", date=date(2026, 9, 2), accrual_id="x1", bucket="other",
        type_id=76, sku="", offer_id="", quantity=0, amount=-7.0,
        seller_price=0.0, sale_price=0.0))
    db.add(models.OzonAccrual(
        op_key="extra-unmapped", date=date(2026, 9, 2), accrual_id="x2", bucket="logistics",
        type_id=32, sku="777777", offer_id="", quantity=1, amount=-13.0,
        seller_price=0.0, sale_price=0.0))
    db.commit()
    view = api_client.get("/api/margin/ozon-detail",
                          params={"date_from": "2026-09-01", "date_to": "2026-09-10"}).json()
    assert view["accrued_rows"] == 1            # начисления двух размеров → один товар OZ
    assert view["accrued_other"] == -18.45  # -11.45 из фикстуры + -7.0 добавленный
    assert view["accrued_unmapped"] == -13.0
    # сумма по артикулам (2176.0 + 680.0) + нераспределённые = все начисления окна
    total_all = view["accrued_total"] + view["accrued_other"] + view["accrued_unmapped"]
    assert total_all == pytest.approx(2176.0 + 680.0 - 18.45 - 13.0, abs=0.01)
    # по умолчанию начисления обоих размеров свёрнуты в строку товара OZ
    o1 = next(x for x in view["rows"] if x["article"] == "OZ")
    assert o1["accrued_net"] == pytest.approx(
        2560.0 - 281.6 - 64.0 - 38.4 + 800.0 - 80.0 - 40.0)
    assert o1["margin_accrued"] == pytest.approx(o1["accrued_net"] - 3 * 500.0)
    # в режиме «в разрезе размеров» начисления остаются у своих артикулов
    bs = api_client.get("/api/margin/ozon-detail", params={
        "date_from": "2026-09-01", "date_to": "2026-09-10", "by_size": 1}).json()
    b1 = next(x for x in bs["rows"] if x["article"] == "OZ-1")
    assert bs["accrued_rows"] == 2
    assert b1["accrued_net"] == pytest.approx(2560.0 - 281.6 - 64.0 - 38.4)
    assert b1["margin_accrued"] == pytest.approx(b1["accrued_net"] - 2 * 500.0)


def test_margin_ozon_detail_compare(api_client, db):
    """compare=1 добавляет показатели предыдущего аналогичного периода."""
    from datetime import date
    from sqlalchemy import select
    from app import models
    mp_oz = db.execute(
        select(models.Marketplace.id).where(models.Marketplace.code == "ozon")).scalar_one()
    db.add(models.MarketplaceCard(marketplace_id=mp_oz, chrt_id="c1",
                                  vendor_code="CMP-OZ", nm_id="9")
           )
    db.add_all([
        models.OzonDetailRow(op_key="cur", source="api", date=date(2026, 8, 15),
                             posting_number="p-cur", offer_id="CMP-OZ", name="Сравн. 1",
                             sku="", barcode="", quantity=2, seller_price=1300.0,
                             amount=2560.0, commission_ratio=0.11, commission=-281.6,
                             standard_fee=-38.4, income=2280.0, return_qty=0, return_total=0.0),
        models.OzonDetailRow(op_key="prev", source="api", date=date(2026, 7, 15),
                             posting_number="p-prev", offer_id="CMP-OZ", name="Сравн. 1",
                             sku="", barcode="", quantity=1, seller_price=1300.0,
                             amount=1280.0, commission_ratio=0.11, commission=-140.8,
                             standard_fee=-19.2, income=1140.0, return_qty=0, return_total=0.0),
    ])
    db.commit()
    # текущее окно (31 день) → предыдущее 2026-07-15..2026-08-14
    r = api_client.get("/api/margin/ozon-detail", params={
        "date_from": "2026-08-15", "date_to": "2026-09-14", "compare": 1,
    })
    body = r.json()
    row = next(x for x in body["rows"] if x["article"] == "CMP-OZ")
    assert row["nm_id"] == "9"
    assert row["sells"] == 2
    assert row["margin"] == pytest.approx(2280.0 - 2 * 500.0)
    assert row["sells_pp"] == 1
    assert row["margin_pp"] == pytest.approx(1140.0 - 500.0)
    assert row["delta_ru"] == pytest.approx((2280.0 - 1000.0) - (1140.0 - 500.0))
    assert body["prev_window"] == {"date_from": "2026-07-15", "date_to": "2026-08-14"}


def test_export_margin_ozon_detail(api_client):
    """Экспорт Анализа Продаж OZON: русские колонки + файл."""
    api_client.post("/api/ozon/cards")
    api_client.post("/api/ozon/detail",
                    params={"date_from": "2026-09-01", "date_to": "2026-09-10", "excel": 0})
    r = api_client.get("/api/export/margin/ozon-detail",
                       params={"date_from": "2026-09-01", "date_to": "2026-09-10"})
    assert r.status_code == 200
    out = pd.read_excel(io.BytesIO(r.content))
    assert {"Артикул", "Прибыль", "К перечислению"}.issubset(set(out.columns))
    assert set(out["Артикул"]) == {"OZ"}
    assert set(out["Размеров"]) == {2}
    r = api_client.get("/api/export/margin/ozon-detail",
                       params={"date_from": "2026-09-01", "date_to": "2026-09-10",
                               "by_size": 1})
    out_bs = pd.read_excel(io.BytesIO(r.content))
    assert set(out_bs["Артикул"]) == {"OZ-1", "OZ-2"}


# ------------------------------------------------------------------ Дашборд (Обзор)
def _seed_dashboard(db):
    from datetime import date, timedelta
    from app import models as m
    from app.services.sync import marketplace_id

    db.add(m.Product(article="JBG-1", name="Джинсы", net_cost=100.0))
    db.add(m.Product(article="TIE-1", name="Галстук", net_cost=100.0))
    db.add(m.Product(article="OZ-1", name="Озон товар", net_cost=300.0))

    def wb(op_key, article, day, qty, amount, for_pay):
        return m.WbDetailRow(op_key=op_key, source="excel", article=article,
                             doc_type_name="Продажа", sale_dt=day, quantity=qty,
                             retail_amount=amount, for_pay=for_pay)

    def oz(op_key, article, day, price, income):
        return m.OzonDetailRow(op_key=op_key, source="api", date=day,
                               posting_number="p" + op_key, offer_id=article,
                               name="Товар " + article, sku="", barcode="",
                               quantity=1, seller_price=price, amount=price,
                               commission_ratio=0, commission=-100, standard_fee=-50,
                               income=income, return_qty=0, return_total=0)

    day = date(2026, 9, 20)
    db.add_all([
        # текущее окно 13..20
        wb("w1", "JBG-1", day - timedelta(days=5), 1, 1000.0, 900.0),
        wb("w2", "TIE-1", day - timedelta(days=4), 2, 2000.0, 1800.0),
        wb("w3", "SHK-1", day - timedelta(days=3), 1, 500.0, 400.0),    # без себест. →-100
        wb("w4", "X-1", day - timedelta(days=2), 1, 300.0, 250.0),       #     → -250
        oz("o1", "OZ-1", day - timedelta(days=5), 1000.0, 850.0),
        # предыдущее окно 05..12
        wb("p1", "JBG-1", day - timedelta(days=10), 1, 500.0, 450.0),
        wb("p2", "X-1", day - timedelta(days=9), 1, 600.0, 500.0),
        oz("o0", "OZ-1", day - timedelta(days=10), 800.0, 700.0),
    ])
    db.commit()
    wb_id = marketplace_id(db, "wb")
    oz_id = marketplace_id(db, "ozon")
    db.add_all([
        m.Stock(marketplace_id=wb_id, date=day, article="JBG-1", warehouse="Все",
                chrt_id="", quantity=2, quantity_full=2, in_way=1),
        m.Stock(marketplace_id=oz_id, date=day - timedelta(days=1), article="OZ-1",
                warehouse="Все", chrt_id="", quantity=3, quantity_full=3, in_way=0),
    ])
    db.add(m.ApiPull(api="wb", kind="detail", last_success_at=day, rows=10, db_rows=8,
                     window="сегодня"))
    # строки Продажи (source != detail) для обратной совместимости per_marketplace
    db.add_all([
        m.Sale(marketplace_id=wb_id, date=day - timedelta(days=5), article="JBG-1",
               quantity=4, revenue=2000.0, income=1800.0, commission=-200.0,
               logistics=-100.0, storage=-10.0, source="api"),
        m.Sale(marketplace_id=oz_id, date=day - timedelta(days=4), article="OZ-1",
               quantity=2, revenue=1200.0, income=1000.0, commission=-120.0,
               logistics=0.0, storage=0.0, source="api"),
    ])
    db.commit()


def test_dashboard_blocks_full(api_client, db):
    """Дашборд: KPI по детализациям, топы, группы, дельты цены, склад, свежесть."""
    _seed_dashboard(db)
    r = api_client.get("/api/dashboard", params={
        "date_from": "2026-09-13", "date_to": "2026-09-20", "compare": 1, "top": 10,
    })
    assert r.status_code == 200
    body = r.json()
    # старый срез (обеспечивает обратную совместимость)
    assert body["per_marketplace"] and body["total"]["sells"] == 6

    k = body["kpis"]
    assert k["total"]["margin"] == pytest.approx(2600.0)          # 800+1600-100-250+550
    assert k["total"]["income"] == pytest.approx(4200.0)
    assert k["total"]["margin_pct"] == pytest.approx(61.9, abs=0.1)
    assert k["total"]["sells"] == 6
    assert k["total"]["articles"] == 5
    by_mp = {m_["marketplace"]: m_ for m_ in k["per_mp"]}
    assert by_mp["wb"]["margin"] == pytest.approx(2050.0)
    assert by_mp["ozon"]["margin"] == pytest.approx(550.0)
    assert k["compare"]["delta_ru"] == pytest.approx(1850.0)      # 2600 - (350+0+400)
    assert k["compare"]["delta_pct"] == pytest.approx(246.7, abs=0.1)

    tops = body["tops"]
    assert tops["profit"]["rows"][0]["article"] == "TIE-1"
    assert tops["profit"]["count"] >= 5
    assert tops["loss"]["rows"][0]["article"] == "X-1"

    pref = {p["prefix"]: p["margin"] for p in body["prefixes"]["rows"]}
    assert pref["TIE"] == pytest.approx(1600.0)
    assert pref["J"] == pytest.approx(800.0)
    assert pref["остальные"] == pytest.approx(-250.0 + 550.0)
    assert pref["SH"] == pytest.approx(-100.0)

    up = {pp["article"]: pp for pp in body["price"]["up"]["rows"]}
    assert up["JBG-1"]["delta_pct"] == 100.0
    assert round(up["OZ-1"]["delta_pct"], 2) == 25.0
    down = {pp["article"]: pp for pp in body["price"]["down"]["rows"]}
    assert down["X-1"]["delta_pct"] == -50.0

    assert body["stocks"]["wb"]["quantity_full"] == 2
    assert body["stocks"]["wb"]["value"] == pytest.approx(200.0)
    assert body["stocks"]["ozon"]["value"] == pytest.approx(900.0)
    assert any(f["api"] == "wb" and f["kind"] == "detail" for f in body["freshness"])


def test_dashboard_marketplace_filter(api_client, db):
    """marketplace=wb фильтрует и детализационные KPI, и продажи, и топы."""
    _seed_dashboard(db)
    r = api_client.get("/api/dashboard", params={
        "date_from": "2026-09-13", "date_to": "2026-09-20",
        "marketplace": "wb", "compare": 1,
    })
    body = r.json()
    assert all(mp["marketplace"] == "wb" for mp in body["per_marketplace"])
    assert [mp["marketplace"] for mp in body["kpis"]["per_mp"]] == ["wb"]
    assert list(body["stocks"]) == ["wb"]
    assert body["kpis"]["compare"]["delta_pct"] is not None
    for arts in (body["tops"]["profit"]["rows"], body["tops"]["loss"]["rows"]):
        assert all("OZ-1" != a["article"] for a in arts)
    assert all(r_["article"] != "OZ-1" for r_ in body["price"]["down"]["rows"])


def test_dashboard_no_params_ok(api_client, db):
    """Дашборд без параметров (стартовая вкладка) отвечает старой структурой."""
    _seed_dashboard(db)
    body = api_client.get("/api/dashboard").json()
    assert set(["per_marketplace", "total", "daily", "kpis", "tops",
                "price", "prefixes", "stocks", "freshness"]).issubset(body.keys())
    assert body["kpis"]["compare"] is None


def test_dashboard_daily_has_profit(api_client, db):
    """daily по таблице Продажи обогащён операционной прибылью (income−комиссии−логистика−хранение)."""
    _seed_dashboard(db)
    body = api_client.get("/api/dashboard", params={
        "date_from": "2026-09-13", "date_to": "2026-09-20",
    }).json()
    by_date = {d["date"]: d for d in body["daily"]}
    assert by_date
    for d in body["daily"]:
        assert "profit" in d
    # WB-продажа 2026-09-15: income 1800 − комиссия 200 − логистика 100 − хранение 10
    key = "2026-09-15"
    assert key in by_date
    assert by_date[key]["profit"] == pytest.approx(1490.0)
    # Ozon 2026-09-16: income 1000 − комиссия 120
    assert by_date["2026-09-16"]["profit"] == pytest.approx(880.0)


def test_dashboard_export_xlsx_multi_sheet(api_client, db):
    """Экспорт дашборда — один xlsx с листами KPI/топы/цены/группы."""
    _seed_dashboard(db)
    r = api_client.get("/api/export/dashboard", params={
        "date_from": "2026-09-13", "date_to": "2026-09-20",
    })
    assert r.status_code == 200
    assert r.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    sheets = pd.read_excel(io.BytesIO(r.content), sheet_name=None)
    assert set(sheets.keys()) >= {
        "KPI", "Прибыльные", "Убыточные", "Рост цены", "Снижение цены", "Группы",
    }
    kpi = sheets["KPI"]
    итого = kpi[kpi["Показатель"] == "Итого"].iloc[0]
    assert итого["Прибыль, руб"] == pytest.approx(2600.0)
    groups = sheets["Группы"]
    assert groups[groups["Группа"] == "TIE"].iloc[0]["Прибыль, руб"] == pytest.approx(1600.0)
    assert len(sheets["Прибыльные"]) >= 5
