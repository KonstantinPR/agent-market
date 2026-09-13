import pandas as pd
from sqlalchemy import select

from app.services.sync import (
    normalize_ozon_realization,
    normalize_wb_detail,
    normalize_wb_sales,
    rebuild_sales_from_detail,
    record_api_pull,
    upsert_products,
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


def test_rebuild_sales_from_detail_splits_returns(db):
    rows = pd.DataFrame([
        {"op_key": "sr:s1", "article": "A1", "doc_type_name": "Продажа",
         "sale_dt": pd.Timestamp("2026-09-01"), "quantity": 2,
         "retail_amount": 2000.0, "for_pay": 1800.0,
         "ppvz_sales_commission": 200.0, "delivery_service": 100.0,
         "paid_storage": 50.0, "penalty": 10.0},
        {"op_key": "sr:s2", "article": "A1", "doc_type_name": "Возврат",
         "sale_dt": pd.Timestamp("2026-09-01"), "quantity": 1,
         "retail_amount": -1000.0, "for_pay": -900.0,
         "ppvz_sales_commission": -100.0, "delivery_service": -50.0},
        {"op_key": "sr:s3", "article": "A2", "doc_type_name": "Продажа",
         "sale_dt": pd.Timestamp("2026-09-02"), "quantity": 1,
         "retail_amount": 1000.0, "for_pay": 900.0},
    ])
    upsert_wb_detail_rows(db, rows, source="excel")
    assert rebuild_sales_from_detail(db) == 2
    a1 = db.execute(
        select(models.Sale).where(models.Sale.article == "A1")
    ).scalar_one()
    assert a1.quantity == 2
    assert a1.returns_qty == 1
    assert a1.revenue == 2000.0
    assert a1.services == 10.0  # penalty не-возврата
    a2 = db.execute(
        select(models.Sale).where(models.Sale.article == "A2")
    ).scalar_one()
    assert a2.quantity == 1