import pandas as pd
from datetime import date
from sqlalchemy import select

from app.services.sync import (
    detail_summary_dataframe,
    normalize_ozon_buyout,
    normalize_ozon_detail,
    normalize_ozon_realization,
    normalize_oz_cards,
    normalize_wb_detail,
    normalize_wb_sales,
    oz_detail_summary_dataframe,
    rebuild_sales_from_detail,
    record_api_pull,
    upsert_ozon_buyouts,
    upsert_ozon_detail_rows,
    upsert_price_snapshots,
    upsert_products,
    upsert_ozon_price_snapshots,
    upsert_sales,
    upsert_wb_detail_rows,
)
from app import models


def _v5_two_rows():
    return pd.DataFrame([
        {
            "sa_name": "TST-1", "sale_dt": "2026-09-01", "quantity": 2,
            "retail_amount": 2200.0, "ppvz_for_pay": 2000.0,
            "delivery_rub": 100.0, "storage_fee": 20.0,
            "ppvz_sales_commission": 80.0, "return_amount": 0,
            "additional_payment": 5.0, "penalty": 10.0, "deduction": 3.0,
        },
        {
            "sa_name": "TST-2", "sale_dt": "2026-09-02", "quantity": 3,
            "retail_amount": 2700.0, "ppvz_for_pay": 2500.0,
            "delivery_rub": 120.0, "storage_fee": 25.0,
            "ppvz_sales_commission": 55.0, "return_amount": 0,
            "additional_payment": 0.0, "penalty": 0.0, "deduction": 0.0,
        },
    ])


def test_normalize_wb_v5_maps_columns_and_services_sum():
    out = normalize_wb_sales(_v5_two_rows())
    assert out is not None
    row = out.iloc[0]
    assert row["article"] == "TST-1"
    assert str(row["date"]) == "2026-09-01"
    assert row["quantity"] == 2
    assert row["income"] == 2000.0
    assert row["revenue"] == 2200.0
    # services = additional_payment + penalty + deduction
    assert row["services"] == 18.0


def test_normalize_wb_v5_no_service_cols_gives_zero():
    df = _v5_two_rows().drop(columns=["additional_payment", "penalty", "deduction"])
    out = normalize_wb_sales(df)
    assert out.iloc[0]["services"] == 0


def test_normalize_wb_detail_mapping():
    df = pd.DataFrame([{
        "vendorCode": "TST-1", "saleDt": "2026-09-01", "quantity": 1,
        "retailAmount": 1100.0, "forPay": 1000.0, "deliveryService": 50.0,
        "paidStorage": 10.0, "ppvzSalesCommission": 40.0, "returnedAmount": 0,
        "additionalPayment": 5.0, "penalty": 10.0, "deduction": 2.0,
    }])
    out = normalize_wb_sales(df)
    row = out.iloc[0]
    assert row["article"] == "TST-1"
    assert row["income"] == 1000.0
    assert row["logistics"] == 50.0
    assert row["services"] == 17.0


def test_normalize_wb_stat_mapping():
    df = pd.DataFrame([{
        "supplierArticle": "TST-1", "date": "2026-08-10", "quantity": 1,
        "totalPrice": 900.0, "forPay": 800.0, "commission": 45.0,
        "logistics": 20.0, "storage": 5.0, "services": 7.0, "returnedAmount": 0,
    }])
    out = normalize_wb_sales(df)
    row = out.iloc[0]
    assert row["article"] == "TST-1"
    assert row["commission"] == 45.0
    assert row["services"] == 7.0


def test_normalize_wb_empty_or_unknown_returns_none():
    assert normalize_wb_sales(None) is None
    assert normalize_wb_sales(pd.DataFrame()) is None
    assert normalize_wb_sales(pd.DataFrame({"foo": [1]})) is None


def test_normalize_ozon_realization_maps_columns():
    df = pd.DataFrame([{
        "date": "2026-09-01", "offer_id": "OZ-1", "quantity": 3,
        "returns_qty": 0, "seller_price": 1300.0, "income": 1200.0,
        "commission": 100.0,
    }])
    out = normalize_ozon_realization(df)
    row = out.iloc[0]
    assert row["article"] == "OZ-1"
    assert row["quantity"] == 3
    assert row["revenue"] == 1300.0
    assert row["income"] == 1200.0
    assert row["services"] == 0


def test_normalize_ozon_empty_or_missing_returns_none():
    assert normalize_ozon_realization(None) is None
    assert normalize_ozon_realization(pd.DataFrame({"x": [1]})) is None


def test_upsert_ozon_price_snapshots(db):
    df = pd.DataFrame([{
        "offer_id": "OZ-1", "product_id": "P1",
        "price_price": 1200, "price_old_price": 1500, "price_min_price": 1000,
    }])
    assert upsert_ozon_price_snapshots(db, df) == 1
    row = db.execute(select(models.PriceSnapshot).where(
        models.PriceSnapshot.article == "OZ-1")).scalars().one()
    assert row.marketplace == "ozon"
    assert row.price == 1200 and row.discounted_price == 1200
    assert round(row.discount, 1) == 20.0


def test_upsert_ozon_price_snapshots_skips_non_positive():
    df = pd.DataFrame([{
        "offer_id": "OZ-X", "product_id": "PX",
        "price_price": 0, "price_old_price": 0, "price_min_price": 0,
    }])
    assert upsert_ozon_price_snapshots(None, df) == 0


def test_normalize_oz_cards_english_columns():
    df = pd.DataFrame({
        "Ozon Product ID": ["111", "222"], "SKU": ["88", "99"],
        "Offer ID": ["OZ-1", "OZ-2"], "Name": ["Товар 1", "Товар 2"],
        "Barcode": ["4-а", "4-б"], "Category": ["Обувь", "Одежда"],
    })
    out = normalize_oz_cards(df)
    assert out is not None and len(out) == 2
    rec = out.iloc[0].to_dict()
    assert rec["chrt_id"] == "111" and rec["nm_id"] == "111"
    assert rec["vendor_code"] == "OZ-1"
    assert rec["barcode"] == "88"
    assert rec["name"] == "Товар 1"
    assert rec["brand"] == "Обувь"
    assert rec["size"] == "" and rec["subject"] == "" and rec["volume_l"] == 0.0


def test_normalize_oz_cards_russian_columns_and_dedupe():
    df = pd.DataFrame({
        "Ozon Product ID": ["95000001", "95000001"],
        "Артикул": ["OZ-1", "OZ-1"], "Штрихкод": ["5-с", "5-с"],
        "Название товара": ["Товар", "Товар"], "Категория": ["Товары для дома", "Товары для дома"],
    })
    out = normalize_oz_cards(df)
    assert out is not None and len(out) == 1
    rec = out.iloc[0].to_dict()
    assert rec["chrt_id"] == "95000001"
    assert rec["vendor_code"] == "OZ-1"
    assert rec["barcode"] == "5-с"
    assert rec["brand"] == "Товары для дома"


def test_normalize_oz_cards_empty_returns_none():
    assert normalize_oz_cards(None) is None
    assert normalize_oz_cards(pd.DataFrame({"x": [1]})) is None


def test_wb_price_snapshots_tagged_marketplace(db):
    df = pd.DataFrame([{
        "nmID": "1001", "vendorCode": "TST-1", "techSizeName": "46",
        "price": 1100, "discountedPrice": 990, "discount": 10,
    }])
    assert upsert_price_snapshots(db, df) == 1
    row = db.execute(select(models.PriceSnapshot).where(
        models.PriceSnapshot.article == "TST-1")).scalars().one()
    assert row.marketplace == "wb"


def test_upsert_products_is_idempotent(db):
    df = pd.DataFrame([{"article": "A1", "name": "Первый", "net_cost": 100.5}])
    assert upsert_products(db, df) == 1
    assert db.get(models.Product, "A1").net_cost == 100.5
    # повторная загрузка: не дублирует, обновляет себестоимость
    df2 = pd.DataFrame([{"article": "A1", "name": "Первый", "net_cost": 90.0}])
    assert upsert_products(db, df2) == 1
    rows = db.execute(
        select(models.Product).where(models.Product.article == "A1")
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].net_cost == 90.0


def test_upsert_sales_agg_and_idempotent(db):
    df = pd.DataFrame([
        {"date": "2026-09-01", "article": "A1", "quantity": 1, "returns_qty": 0,
         "revenue": 100.0, "commission": 10.0, "logistics": 5.0,
         "storage": 1.0, "services": 0, "income": 84.0},
        {"date": "2026-09-01", "article": "A1", "quantity": 2, "returns_qty": 0,
         "revenue": 200.0, "commission": 20.0, "logistics": 10.0,
         "storage": 2.0, "services": 0, "income": 168.0},
    ])
    assert upsert_sales(db, df, "wb") == 1  # агрегируется в одну строку
    row = db.execute(
        select(models.Sale).where(models.Sale.article == "A1")
    ).scalar_one()
    assert row.quantity == 3
    assert row.revenue == 300.0
    assert row.income == 252.0
    # повторный запуск перезаписывает, а не дублирует
    upsert_sales(db, df, "wb")
    cnt = db.execute(
        select(models.Sale).where(models.Sale.article == "A1")
    ).scalars().all()
    assert len(cnt) == 1


def test_record_api_pull_upserts_by_api_kind(db):
    record_api_pull(db, "wb", "sales", rows=10, db_rows=8, window="2026-08-01..2026-09-01")
    pulls = db.execute(select(models.ApiPull)).scalars().all()
    assert len(pulls) == 1
    assert pulls[0].rows == 10
    # повторная запись перезаписывает строку; expire_on_commit=False, поэтому принудительно перечитываем
    record_api_pull(db, "wb", "sales", rows=12, db_rows=9, window="2026-09-01..2026-09-08")
    pull = db.execute(
        select(models.ApiPull).execution_options(populate_existing=True)
    ).scalars().one()
    assert pull.rows == 12
    assert pull.window == "2026-09-01..2026-09-08"


def test_normalize_wb_detail_excel_maps_srid_op_key():
    df = pd.DataFrame([{
        "Артикул поставщика": "TST-1", "Дата продажи": "2026-09-01",
        "Тип документа": "Продажа", "Кол-во": 2, "Srid": "ab.123.0.0",
        "Вайлдберриз реализовал Товар (Пр)": 2200.0,
        "К перечислению Продавцу за реализованный Товар": 2000.0,
    }])
    out = normalize_wb_detail(df, source="excel")
    assert out is not None
    row = out.iloc[0]
    assert row["article"] == "TST-1"
    assert row["op_key"] == "sr:ab.123.0.0"
    assert row["quantity"] == 2
    assert row["retail_amount"] == 2200.0
    assert row["for_pay"] == 2000.0
    assert str(row["sale_dt"].date()) == "2026-09-01"


def test_normalize_wb_detail_api_fallback_to_rrd_id():
    df = pd.DataFrame([{
        "vendorCode": "TST-1", "saleDt": "2026-09-01", "quantity": 1,
        "rrdId": 777, "retailAmount": 1100.0, "forPay": 1000.0,
    }])
    out = normalize_wb_detail(df, source="api")
    assert out.iloc[0]["op_key"] == "rr::777"


def test_normalize_wb_detail_drops_service_rows_without_id_or_article():
    df = pd.DataFrame([{
        "Дата продажи": "2026-09-01", "Кол-во": 1, "Тип документа": "Доставка",
    }])
    assert normalize_wb_detail(df, source="excel") is None


def test_upsert_wb_detail_rows_idempotent_by_op_key(db):
    df = pd.DataFrame([
        {"op_key": "sr:op1", "article": "A1", "sale_dt": pd.Timestamp("2026-09-01"),
         "quantity": 2, "retail_amount": 2000.0, "for_pay": 1800.0},
        {"op_key": "sr:op2", "article": "A1", "sale_dt": pd.Timestamp("2026-09-01"),
         "quantity": 1, "retail_amount": 1000.0, "for_pay": 900.0},
    ])
    assert upsert_wb_detail_rows(db, df, source="excel") == 2
    # повторная загрузка с тем же op_key и НОВЫМ значением — перезапись, не дубль
    df2 = pd.DataFrame([{"op_key": "sr:op1", "article": "A1",
                         "sale_dt": pd.Timestamp("2026-09-01"),
                         "quantity": 5, "retail_amount": 5000.0, "for_pay": 4500.0}])
    assert upsert_wb_detail_rows(db, df2, source="excel") == 1
    rows = db.execute(select(models.WbDetailRow)).scalars().all()
    assert len(rows) == 2
    assert next(r for r in rows if r.op_key == "sr:op1").quantity == 5


def test_upsert_wb_detail_rows_sums_only_goods_quantity(db):
    """Строка логистики в группе SRID (пустой «Тип документа», без денег)
    не должна раздувать «Кол-во» при свёртке по op_key."""
    df = pd.DataFrame([
        {"op_key": "sr:op1", "article": "A1", "doc_type_name": "Продажа",
         "sale_dt": pd.Timestamp("2026-09-01"), "quantity": 2,
         "retail_amount": 2000.0, "for_pay": 1800.0},
        {"op_key": "sr:op1", "article": "A1", "doc_type_name": "",
         "sale_dt": pd.Timestamp("2026-09-01"), "quantity": 2,
         "retail_amount": 0.0, "for_pay": 0.0},
        # группа только из логистики — «Кол-во» вообще не считаем
        {"op_key": "sr:op2", "article": "A1", "doc_type_name": "",
         "sale_dt": pd.Timestamp("2026-09-01"), "quantity": 4,
         "retail_amount": 0.0, "for_pay": 0.0},
    ])
    assert upsert_wb_detail_rows(db, df, source="excel") == 2
    rows = {r.op_key: r for r in db.execute(select(models.WbDetailRow)).scalars().all()}
    assert rows["sr:op1"].quantity == 2
    assert rows["sr:op2"].quantity == 0


def test_rebuild_sales_from_detail_ignores_logistics_rows(db):
    """Легаси-строка чистой логистики (без денег) не попадает в «продано»."""
    rows = pd.DataFrame([
        {"op_key": "sr:legacy1", "article": "A1", "doc_type_name": "Продажа",
         "sale_dt": pd.Timestamp("2026-09-01"), "quantity": 3,
         "retail_amount": 3000.0, "for_pay": 2700.0},
        {"op_key": "sr:legacy2", "article": "A1", "doc_type_name": "",
         "sale_dt": pd.Timestamp("2026-09-01"), "quantity": 100,
         "retail_amount": 0.0, "for_pay": 0.0},
    ])
    upsert_wb_detail_rows(db, rows, source="excel")
    assert rebuild_sales_from_detail(db) == 1
    a1 = db.execute(select(models.Sale).where(models.Sale.article == "A1")).scalar_one()
    assert a1.quantity == 3


def test_rebuild_sales_from_detail_splits_returns(db):
    rows = pd.DataFrame([
        {"op_key": "sr:s1", "article": "A1", "doc_type_name": "Продажа",
         "sale_dt": pd.Timestamp("2026-09-01"), "quantity": 2,
         "retail_amount": 2000.0, "for_pay": 1800.0,
         "ppvz_sales_commission": 200.0, "delivery_service": 100.0,
         "paid_storage": 50.0, "penalty": 10.0},
        {"op_key": "sr:s2", "article": "A1", "doc_type_name": "Возврат",
         "sale_dt": pd.Timestamp("2026-09-01"), "quantity": 1,
         "retail_amount": 1000.0, "for_pay": 900.0,
         "ppvz_sales_commission": 100.0, "delivery_service": 50.0},
        {"op_key": "sr:s3", "article": "A2", "doc_type_name": "Продажа",
         "sale_dt": pd.Timestamp("2026-09-02"), "quantity": 1,
         "retail_amount": 1000.0, "for_pay": 900.0},
    ])
    upsert_wb_detail_rows(db, rows, source="excel")
    assert rebuild_sales_from_detail(db) == 2
    a1 = db.execute(
        select(models.Sale).where(models.Sale.article == "A1")
    ).scalar_one()
    # возврат вычитается из продаж; расходы WB всегда расход (без знака)
    assert a1.quantity == 1
    assert a1.returns_qty == 1
    assert a1.revenue == 1000.0
    assert a1.income == 900.0
    assert a1.logistics == 150.0  # доставка продажи + доставка возврата
    assert a1.services == 10.0  # penalty не-возврата
    a2 = db.execute(
        select(models.Sale).where(models.Sale.article == "A2")
    ).scalar_one()
    assert a2.quantity == 1


def test_detail_summary_dataframe_aggregates_by_article(db):
    rows = pd.DataFrame([
        {"op_key": "sr:s1", "source": "excel", "article": "A1", "title": "Товар A1",
         "doc_type_name": "Продажа", "sale_dt": pd.Timestamp("2026-09-01"),
         "quantity": 2, "retail_amount": 2000.0, "for_pay": 1800.0,
         "ppvz_sales_commission": 200.0, "delivery_service": 100.0,
         "paid_storage": 50.0, "penalty": 10.0},
        {"op_key": "sr:s2", "source": "excel", "article": "A1", "title": "Товар A1",
         "doc_type_name": "Возврат", "sale_dt": pd.Timestamp("2026-09-02"),
         "quantity": 1, "retail_amount": 1000.0, "for_pay": 900.0,
         "ppvz_sales_commission": 100.0, "delivery_service": 50.0},
        {"op_key": "sr:s3", "source": "api", "article": "A2", "title": "Товар A2",
         "doc_type_name": "Продажа", "sale_dt": pd.Timestamp("2026-09-03"),
         "quantity": 1, "retail_amount": 1000.0, "for_pay": 900.0},
    ])
    # source в upsert_wb_detail_rows один на весь вызов — грузим двумя вызовами
    upsert_wb_detail_rows(db, rows.iloc[:2], source="excel")
    upsert_wb_detail_rows(db, rows.iloc[2:], source="api")
    out = detail_summary_dataframe(db, date_from="2026-09-01", date_to="2026-09-10")
    assert list(out.columns) == ["article", "title", "sells", "returns_qty", "revenue",
                                  "commission", "for_pay", "logistics", "delivery_count",
                                  "return_delivery_count", "storage", "pvz_compensation",
                                  "payment_services", "services", "ops_count", "sources"]
    by = {r["article"]: r for r in out.to_dict("records")}
    a1 = by["A1"]
    # нетто-продажи = 2 − 1, возвраты отдельным счётчиком
    assert a1["sells"] == 1
    assert a1["returns_qty"] == 1
    assert a1["revenue"] == 1000.0
    assert a1["for_pay"] == 900.0
    assert a1["commission"] == 100.0
    # расходы WB всегда расход независимо от типа
    assert a1["logistics"] == 150.0
    assert a1["storage"] == 50.0
    assert a1["ops_count"] == 2
    assert a1["sources"] == "excel"
    a2 = by["A2"]
    assert a2["sells"] == 1
    assert a2["ops_count"] == 1
    assert a2["sources"] == "api"


def test_detail_summary_includes_storage_estimate(db):
    """Безартикульная плата хранения разносится по артикулу свода (объём × остаток)."""
    upsert_wb_detail_rows(db, pd.DataFrame([
        {"op_key": "sr:s1", "article": "A1", "doc_type_name": "Продажа",
         "sale_dt": pd.Timestamp("2026-09-01"), "quantity": 1,
         "retail_amount": 1000.0, "for_pay": 900.0},
    ]), source="excel")
    db.add_all([
        models.StorageCost(nm_id="1", article="A1", volume=2.0),
        models.Stock(marketplace_id=1, date=date(2026, 9, 1), article="A1", quantity=10),
        models.WbDetailRow(op_key="sr:st1", source="excel", article="",
                           doc_type_name="Хранение", sale_dt=date(2026, 9, 1),
                           paid_storage=100.0),
    ])
    db.commit()
    out = detail_summary_dataframe(db, date_from="2026-09-01", date_to="2026-09-10")
    row = out[out["article"] == "A1"].iloc[0]
    assert row["storage"] == 100.0
    assert not (out["article"] == "").any()  # всё разнесено — остатка нет


def test_detail_summary_storage_full_distribution_db(db):
    """Плата за день вне окна ±7 разносится по ближайшему полезному срезу,
    излишка (строки-остатка) не остаётся."""
    from app.services.sync import detail_summary_dataframe

    db.add_all([
        models.StorageCost(nm_id="d1", article="A1", volume=2.0),
        models.StorageCost(nm_id="d2", article="B2", volume=4.0),
        models.Stock(marketplace_id=1, date=date(2026, 9, 11), article="A1", quantity=10),
        models.Stock(marketplace_id=1, date=date(2026, 9, 11), article="B2", quantity=10),
        models.Stock(marketplace_id=1, date=date(2026, 9, 4), article="1999", quantity=5),
        models.WbDetailRow(op_key="sr:s1", source="excel", article="A1",
                           doc_type_name="Продажа", sale_dt=date(2026, 9, 3),
                           quantity=1, retail_amount=1000.0, for_pay=900.0),
        models.WbDetailRow(op_key="sr:s2", source="excel", article="B2",
                           doc_type_name="Продажа", sale_dt=date(2026, 9, 3),
                           quantity=1, retail_amount=1000.0, for_pay=900.0),
        models.WbDetailRow(op_key="sr:st1", source="excel", article="",
                           doc_type_name="Хранение", sale_dt=date(2026, 9, 1),
                           paid_storage=300.0),
    ])
    db.commit()
    # Списание 09-01: окно ±7 = 08-25..09-08 → срез 09-04 имеет вес от продаж
    # A1/B2 (объём × проданное×0.5: 1 и 2), срез 09-11 — от остатков (20/40).
    # Выбранный срез (в окне — 09-04) даёт то же соотношение 1:2 → 100/200.
    out = detail_summary_dataframe(db, date_from="2026-09-01", date_to="2026-09-10")
    # 300 * 20/60 = 100; 300 * 40/60 = 200
    assert out[out["article"] == "A1"].iloc[0]["storage"] == 100.0
    assert out[out["article"] == "B2"].iloc[0]["storage"] == 200.0
    assert not (out["article"] == "").any()  # строки-остатка нет


def test_detail_summary_dataframe_empty_and_filters(db):
    assert detail_summary_dataframe(db,
                                    date_from="2026-01-01", date_to="2026-01-02").empty
    rows = pd.DataFrame([
        {"op_key": "sr:s1", "article": "A1", "doc_type_name": "Продажа",
         "sale_dt": pd.Timestamp("2026-09-01"), "quantity": 1,
         "retail_amount": 1000.0, "for_pay": 900.0},
    ])
    upsert_wb_detail_rows(db, rows, source="excel")
    # фильтр по артикулу
    out = detail_summary_dataframe(db, date_from="2026-09-01", date_to="2026-09-10",
                                   article_like="NOPE")
    assert out.empty
    out = detail_summary_dataframe(db, date_from="2026-09-01", date_to="2026-09-10",
                                   article_like="A1")
    assert len(out) == 1
    assert out.iloc[0]["sells"] == 1


def test_detail_summary_dataframe_is_goods_guard(db):
    """Строка чистой логистики (пустой тип, без денег) не считается продажей."""
    rows = pd.DataFrame([
        {"op_key": "sr:log1", "article": "A1", "doc_type_name": "",
         "sale_dt": pd.Timestamp("2026-09-01"), "quantity": 100,
         "retail_amount": 0.0, "for_pay": 0.0, "delivery_service": 50.0},
    ])
    upsert_wb_detail_rows(db, rows, source="excel")
    out = detail_summary_dataframe(db, date_from="2026-09-01", date_to="2026-09-10")
    a1 = out.iloc[0]
    assert a1["sells"] == 0
    assert a1["ops_count"] == 1
    assert a1["logistics"] == 50.0


def test_detail_summary_redistributes_articleless_logistics(db):
    """Безартикульная логистика (без srid-привязки) разносится по артикулам
    пропорционально delivery_count + return_delivery_count."""
    rows = pd.DataFrame([
        {"op_key": "sr:s1", "article": "A1", "doc_type_name": "Продажа",
         "sale_dt": pd.Timestamp("2026-09-01"), "quantity": 1,
         "retail_amount": 1000.0, "for_pay": 900.0,
         "delivery_service": 100.0, "delivery_count": 1, "return_delivery_count": 0},
        {"op_key": "sr:s2", "article": "A2", "doc_type_name": "Продажа",
         "sale_dt": pd.Timestamp("2026-09-01"), "quantity": 1,
         "retail_amount": 1000.0, "for_pay": 900.0,
         "delivery_service": 100.0, "delivery_count": 3, "return_delivery_count": 1},
        {"op_key": "sr:al1", "article": "", "doc_type_name":
         "Возмещение издержек по перевозке/по складским операциям с то",
         "sale_dt": pd.Timestamp("2026-09-01"),
         "delivery_service": 400.0, "delivery_count": 4, "return_delivery_count": 4,
         "pvz_compensation": 20.0, "penalty": 5.0},
    ])
    upsert_wb_detail_rows(db, rows, source="excel")
    out = detail_summary_dataframe(db, date_from="2026-09-01", date_to="2026-09-10")
    assert not (out["article"] == "").any()  # всё разнесено — остатка нет
    a1 = out[out["article"] == "A1"].iloc[0]
    a2 = out[out["article"] == "A2"].iloc[0]
    # веса: A1 = 1+0 = 1, A2 = 3+1 = 4 → доли A1=0.2, A2=0.8
    # доставка безарт.: 400 → A1 +80, A2 +320 (сверх своих 100/100)
    assert a1["logistics"] == 180.0
    assert a2["logistics"] == 420.0
    # счётчики: 4/4 доставок → A1 +1/+1, A2 +3/+3
    assert a1["delivery_count"] == 2
    assert a1["return_delivery_count"] == 1
    assert a2["delivery_count"] == 6
    assert a2["return_delivery_count"] == 4
    # pvz_compensation и услуги тоже разнесены (доли 0.2/0.8)
    assert a1["pvz_compensation"] == 4.0
    assert a2["pvz_compensation"] == 16.0
    assert a1["services"] == 1.0
    assert a2["services"] == 4.0


def _oz_detail_rows():
    return pd.DataFrame([
        {
            "date": "2026-09-01", "posting_number": "PZ-1", "offer_id": "OZ-1",
            "name": "Ozon 1", "sku": "3001", "barcode": "3001", "quantity": 2,
            "seller_price": 1300.0, "amount": 2560.0, "commission_ratio": 0.11,
            "commission": -281.6, "standard_fee": -38.4, "income": 2280.0,
            "return_qty": 0, "return_total": 0.0,
        },
        {
            "date": "2026-09-01", "posting_number": "PZ-2", "offer_id": "OZ-2",
            "name": "Ozon 2", "sku": "3002", "barcode": "3002", "quantity": 1,
            "seller_price": 900.0, "amount": 800.0, "commission_ratio": 0.10,
            "commission": -80.0, "standard_fee": -20.0, "income": 700.0,
            "return_qty": 1, "return_total": 700.0,
        },
    ])


def test_normalize_ozon_detail_maps_columns_and_op_key():
    out = normalize_ozon_detail(_oz_detail_rows())
    assert out is not None
    assert list(out["op_key"]) == ["2026-09-01|PZ-1|3001", "2026-09-01|PZ-2|3002"]
    row = out.iloc[0]
    assert str(row["date"]) == "2026-09-01"
    assert row["offer_id"] == "OZ-1"
    assert row["quantity"] == 2
    assert row["income"] == 2280.0
    assert row["commission"] == -281.6


def test_normalize_ozon_detail_drops_rows_without_date_or_article():
    df = _oz_detail_rows()
    df = pd.concat([df, pd.DataFrame([{
        "date": None, "posting_number": "PZ-3", "offer_id": "OZ-1",
        "sku": "3003", "quantity": 1,
    }])], ignore_index=True)
    out = normalize_ozon_detail(df)
    assert out is not None
    assert len(out) == 2


def test_normalize_ozon_detail_missing_required_returns_none():
    assert normalize_ozon_detail(pd.DataFrame({"x": [1]})) is None
    assert normalize_ozon_detail(pd.DataFrame()) is None
    assert normalize_ozon_detail(None) is None


def test_upsert_ozon_detail_rows_idempotent_by_op_key(db):
    n1 = upsert_ozon_detail_rows(db, normalize_ozon_detail(_oz_detail_rows()))
    assert n1 == 2
    df2 = _oz_detail_rows()
    df2.iloc[0, df2.columns.get_loc("income")] = 9999.0
    n2 = upsert_ozon_detail_rows(db, normalize_ozon_detail(df2))
    assert n2 == 2
    rows = db.execute(select(models.OzonDetailRow)).scalars().all()
    assert len(rows) == 2
    by_key = {r.op_key: r for r in rows}
    assert by_key["2026-09-01|PZ-1|3001"].income == 9999.0
    assert by_key["2026-09-01|PZ-1|3001"].seller_price == 1300.0


def _oz_buyout_rows():
    return pd.DataFrame([
        {
            "posting_number": "PZ-1", "offer_id": "OZ-1", "name": "Ozon 1",
            "sku": "3001", "quantity": 2, "seller_price": 1300.0,
            "buyout_price": 1250.0, "amount": 2500.0,
            "deduction_by_category_percent": 12.5, "vat_percent": 20,
        },
        {
            "posting_number": "PZ-2", "offer_id": "OZ-2", "name": "Ozon 2",
            "sku": "3002", "quantity": 1, "seller_price": 900.0,
            "buyout_price": 880.0, "amount": 880.0,
            "deduction_by_category_percent": 0.0, "vat_percent": 20,
        },
    ])


def test_normalize_ozon_buyout_maps_columns():
    out = normalize_ozon_buyout(_oz_buyout_rows())
    assert out is not None
    assert list(out["op_key"]) == ["PZ-1|3001", "PZ-2|3002"]
    row = out.iloc[0]
    assert row["offer_id"] == "OZ-1"
    assert row["buyout_price"] == 1250.0
    assert row["amount"] == 2500.0


def test_upsert_ozon_buyouts_idempotent_by_op_key(db):
    n1 = upsert_ozon_buyouts(db, normalize_ozon_buyout(_oz_buyout_rows()))
    assert n1 == 2
    df2 = _oz_buyout_rows()
    df2.iloc[0, df2.columns.get_loc("amount")] = 1111.0
    n2 = upsert_ozon_buyouts(db, normalize_ozon_buyout(df2))
    assert n2 == 2
    rows = db.execute(select(models.OzonBuyout)).scalars().all()
    assert len(rows) == 2


def test_oz_detail_summary_dataframe_aggregates_and_buyout(db):
    day = pd.Timestamp("2026-09-01")
    upsert_ozon_detail_rows(db, normalize_ozon_detail(_oz_detail_rows()))
    upsert_ozon_buyouts(db, normalize_ozon_buyout(_oz_buyout_rows()))
    out = oz_detail_summary_dataframe(db, date_from="2026-09-01", date_to="2026-09-30")
    o1 = out[out["article"] == "OZ-1"].iloc[0]
    o2 = out[out["article"] == "OZ-2"].iloc[0]
    assert o1["sells"] == 2 and o1["returns_qty"] == 0
    assert o2["sells"] == 1 and o2["returns_qty"] == 1
    assert o1["seller_total"] == 2600.0
    assert o1["amount"] == 2560.0
    assert o1["commission"] == -281.6
    assert o1["income"] == 2280.0
    # выкупы: OZ-1 — 2500/2600 = 96.15%, OZ-2 — 880/900 = 97.78%
    assert o1["buyout_sum"] == 2500.0
    assert abs(o1["buyout_percent"] - 96.15) < 0.01
    assert abs(o2["buyout_percent"] - 97.78) < 0.01