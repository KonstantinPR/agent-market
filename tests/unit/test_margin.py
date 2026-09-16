import pandas as pd
from datetime import date

from app.services.margin import (compare_margin_periods, compute_margin, empty_margin_df,
                                 margin_columns, margin_detail_dataframe, storage_split)
from app import models


def _base_row():
    return {
        "article": "A1", "name": "Товар", "sells": 2, "revenue": 800.0,
        "commission": 50.0, "logistics": 40.0, "storage": 10.0,
        "services": 5.0, "income": 1000.0, "net_cost": 300.0,
    }


def test_compute_margin_formulas():
    df = compute_margin(pd.DataFrame([_base_row()]))
    row = df.iloc[0]
    assert row["other"] == 95.0  # income - (revenue+комиссия+логистика+хранение+услуги)
    # Маржа до себестоимости в главной таблице продаж: income - расходы WB
    assert row["margin_gross"] == 1000.0 - 40.0 - 10.0 - 5.0  # 945
    # В главной таблице margin не меняется: income - net_cost * продажи
    assert row["margin"] == 1000.0 - 300.0 * 2  # 400
    assert row["margin_per_one"] == 200.0
    assert row["margin_pct"] == 40.0


def test_compute_margin_other_absorbs_negative():
    row = _base_row()
    row["services"] = -560.0
    df = compute_margin(pd.DataFrame([row]))
    assert df.iloc[0]["other"] == 1000.0 - (800.0 + 50.0 + 40.0 + 10.0 + (-560.0))


def test_compute_margin_zero_income_percent():
    row = _base_row()
    row["income"] = 0.0
    df = compute_margin(pd.DataFrame([row]))
    assert df.iloc[0]["margin_pct"] == 0.0


def test_compute_margin_zero_sells_no_division_by_zero():
    row = _base_row()
    row["sells"] = 0
    df = compute_margin(pd.DataFrame([row]))
    assert df.iloc[0]["margin_per_one"] == 0.0


def test_compute_margin_sorts_descending_by_margin():
    rows = [_base_row(), {**_base_row(), "article": "A2", "income": 500.0, "net_cost": 200.0}]
    df = compute_margin(pd.DataFrame(rows))
    assert df.iloc[0]["article"] == "A1"


def test_compute_margin_all_derived_columns_present():
    df = compute_margin(pd.DataFrame([_base_row()]))
    assert set(margin_columns()).issubset(df.columns)


def test_empty_margin_df_has_full_schema():
    df = empty_margin_df()
    assert df.empty
    assert list(df.columns) == margin_columns()


def test_compute_margin_negative_sells_no_cogs():
    row = _base_row()
    row["sells"] = -5  # возвратов больше, чем продаж
    df = compute_margin(pd.DataFrame([row]))
    # себестоимость НЕ вычитается (товар вернулся на склад) — иначе -5500*... давало бы фиктивную прибыль
    assert df.iloc[0]["margin"] == 1000.0
    assert df.iloc[0]["margin_per_one"] == 0.0
    assert df.iloc[0]["margin_pct"] == 0.0


def test_compute_margin_negative_income_percent_zero():
    row = _base_row()
    row["income"] = -100.0
    df = compute_margin(pd.DataFrame([row]))
    assert df.iloc[0]["margin_pct"] == 0.0


def test_margin_detail_ignores_logistics_rows(db):
    """Read-гард: легаси-строки логистики (старая свёртка с раздутым «Кол-во»,
    нулевые деньги) не считаются «продано, шт»."""
    db.add_all([
        models.WbDetailRow(op_key="sr:sale1", source="excel", article="A1",
                           doc_type_name="Продажа", sale_dt=date(2026, 9, 1),
                           quantity=2, retail_amount=2000.0, for_pay=1800.0),
        models.WbDetailRow(op_key="sr:log1", source="excel", article="A1",
                           doc_type_name="", sale_dt=date(2026, 9, 1),
                           quantity=100, retail_amount=0.0, for_pay=0.0),
    ])
    db.commit()
    out = margin_detail_dataframe(db)
    row = out.iloc[0]
    assert row["article"] == "A1"
    assert row["sells"] == 2
    assert row["revenue"] == 2000.0


def test_detail_margin_gross_matches_margin_when_no_costs(db):
    """margin_gross = income - logistics - storage - services;
    margin = margin_gross - net_cost * sells. При нулевых расходах совпадает."""
    db.add_all([
        models.WbDetailRow(op_key="sr:sale1", source="excel", article="B1",
                           doc_type_name="Продажа", sale_dt=date(2026, 9, 1),
                           quantity=1, retail_amount=1000.0, for_pay=1000.0,
                           delivery_service=0.0, paid_storage=0.0),
    ])
    db.commit()
    out = margin_detail_dataframe(db)
    row = out.iloc[0]
    assert row["margin_gross"] == 1000.0
    assert row["margin"] == 1000.0 - 0.0  # default_net_cost=0


def test_detail_margin_gross_subtracts_wb_expenses(db):
    """margin_gross вычитает логистику, хранение, услуги."""
    db.add_all([
        models.WbDetailRow(op_key="sr:sale1", source="excel", article="B2",
                           doc_type_name="Продажа", sale_dt=date(2026, 9, 1),
                           quantity=1, retail_amount=2000.0, for_pay=1200.0,
                           delivery_service=50.0, paid_storage=10.0),
    ])
    db.commit()
    out = margin_detail_dataframe(db)
    row = out.iloc[0]
    assert row["income"] == 1200.0
    assert row["logistics"] == 50.0
    assert row["storage"] == 10.0
    assert row["margin_gross"] == 1200.0 - 50.0 - 10.0 - 0.0  # 1140


def test_detail_negative_sells_margin_gross_zero_cogs(db):
    """При отрицательных продажах (возвраты > продаж) margin_gross корректен,
    себестоимость НЕ вычитается, margin = margin_gross."""
    db.add_all([
        models.WbDetailRow(op_key="sr:sale1", source="excel", article="B3",
                           doc_type_name="Продажа", sale_dt=date(2026, 9, 1),
                           quantity=1, retail_amount=1000.0, for_pay=1000.0),
        models.WbDetailRow(op_key="sr:ret1", source="excel", article="B3",
                           doc_type_name="Возврат", sale_dt=date(2026, 9, 2),
                           quantity=3, retail_amount=3000.0, for_pay=-3000.0),
    ])
    db.commit()
    out = margin_detail_dataframe(db, default_net_cost=500.0)
    row = out.iloc[0]
    assert row["sells"] == -2  # 1 - 3
    assert row["margin_gross"] == row["income"] - row["logistics"] - row["storage"] - row["services"]
    assert row["margin"] == row["margin_gross"]  # cogs не вычитается
    assert row["margin_per_one"] == 0.0
    assert row["margin_pct"] == 0.0


def _margin_row(article, margin, sells):
    return {"article": article, "margin": margin, "sells": sells}


def test_compare_margin_periods_adds_deltas():
    cur = pd.DataFrame([
        _margin_row("A1", 400.0, 2),
        _margin_row("A2", 300.0, 1),
    ])
    prev = pd.DataFrame([
        _margin_row("A1", 200.0, 5),
    ])
    out = compare_margin_periods(cur, prev)
    by = {r["article"]: r for r in out.to_dict("records")}
    assert by["A1"]["sells_pp"] == 5
    assert by["A1"]["margin_pp"] == 200.0
    assert by["A1"]["delta_ru"] == 200.0
    assert by["A1"]["delta_pct"] == 100.0
    # артикул без пред. периода — пустые показатели, без исключений
    assert pd.isna(by["A2"]["margin_pp"])
    assert pd.isna(by["A2"]["delta_ru"])
    assert pd.isna(by["A2"]["delta_pct"])


def test_compare_margin_periods_negative_prev_uses_abs():
    cur = pd.DataFrame([_margin_row("A1", 100.0, 1)])
    prev = pd.DataFrame([_margin_row("A1", -50.0, 1)])
    out = compare_margin_periods(cur, prev)
    row = out.iloc[0]
    assert row["delta_ru"] == 150.0
    assert row["delta_pct"] == 300.0  # 150 / 50 * 100


def test_compare_margin_periods_empty_prev():
    cur = pd.DataFrame([_margin_row("A1", 400.0, 2)])
    out = compare_margin_periods(cur, pd.DataFrame())
    row = out.iloc[0]
    assert pd.isna(row["margin_pp"])
    assert pd.isna(row["delta_ru"])
    assert pd.isna(row["delta_pct"])


def test_margin_detail_matches_product_case_insensitive(db):
    """Себестоимость ищется по UPPER(article): в products.xlsx артикулы в верхнем
    регистре, в детализации — как в файле (нижний)."""
    db.add(models.Product(article="JZ2-GUCCI-5082-DARKGREY", name="Гуччи",
                          net_cost=1500.0))
    db.add(models.WbDetailRow(op_key="sr:s1", source="excel", article="jz2-gucci-5082-darkgrey",
                              doc_type_name="Продажа", sale_dt=date(2026, 9, 1),
                              quantity=1, retail_amount=3000.0, for_pay=2500.0))
    db.commit()
    out = margin_detail_dataframe(db)
    row = out.iloc[0]
    assert row["article"] == "jz2-gucci-5082-darkgrey"
    assert row["name"] == "Гуччи"
    assert row["net_cost"] == 1500.0
    assert row["net_cost_est"] == False
    assert row["margin"] == 2500.0 - 1500.0


def test_margin_detail_falls_back_to_barcode(db):
    """Если артикул не матчится по регистру — берём продукт по barcode (sku)."""
    db.add(models.Product(article="ABC-1", name="По баркоду", net_cost=700.0,
                          barcode="2047932869792"))
    db.add(models.WbDetailRow(op_key="sr:x", source="excel", article="xyz-9",
                              sku="2047932869792",
                              doc_type_name="Продажа", sale_dt=date(2026, 9, 1),
                              quantity=1, retail_amount=1200.0, for_pay=1000.0))
    db.commit()
    out = margin_detail_dataframe(db)
    row = out.iloc[0]
    assert row["article"] == "xyz-9"
    assert row["name"] == "По баркоду"
    assert row["net_cost"] == 700.0
    assert row["net_cost_est"] == False


def test_margin_detail_no_product_defaults_to_estimate(db):
    """Без продукта и default_net_cost=0 → net_cost_est=True, себестоимость 0."""
    db.add(models.WbDetailRow(op_key="sr:y", source="excel", article="nope-1",
                              doc_type_name="Продажа", sale_dt=date(2026, 9, 1),
                              quantity=1, retail_amount=500.0, for_pay=400.0))
    db.commit()
    out = margin_detail_dataframe(db)
    row = out.iloc[0]
    assert row["net_cost"] == 0.0
    assert row["net_cost_est"] == True


def _stock(article, quantity, day=date(2026, 9, 4)):
    return models.Stock(marketplace_id=1, date=day, article=article, quantity=quantity)


def test_storage_split_distributes_by_volume_x_stock(db):
    """Дневная плата хранения распределяется между товарами пропорц. объём × остаток."""
    db.add_all([
        models.StorageCost(nm_id="1", article="A1", volume=2.0),
        models.StorageCost(nm_id="2", article="B2", volume=1.0),
        _stock("A1", 100),   # вес 2*100 = 200
        _stock("B2", 300),   # вес 1*300 = 300
        models.WbDetailRow(op_key="sr:st1", source="excel", article="",  # хранение — без артикула
                           doc_type_name="Хранение", sale_dt=date(2026, 9, 4),
                           paid_storage=500.0),
    ])
    db.commit()
    out = storage_split(db, date_from=date(2026, 9, 1), date_to=date(2026, 9, 5))
    assert out == {"A1": 200.0, "B2": 300.0}  # 500 * 200/500, 500 * 300/500


def test_storage_split_accumulates_over_days(db):
    """Платы за разные дни суммируются по каждому товару."""
    db.add_all([
        models.StorageCost(nm_id="1", article="A1", volume=1.0),
        _stock("A1", 10),
        models.WbDetailRow(op_key="sr:st1", source="excel", article="",
                           doc_type_name="Хранение", sale_dt=date(2026, 9, 4),
                           paid_storage=100.0),
        models.WbDetailRow(op_key="sr:st2", source="excel", article="",
                           doc_type_name="Хранение", sale_dt=date(2026, 9, 5),
                           paid_storage=50.0),
    ])
    db.commit()
    out = storage_split(db, date_from=date(2026, 9, 1), date_to=date(2026, 9, 6))
    assert out == {"A1": 150.0}


def test_storage_split_uses_nearest_stock_for_missing_day(db):
    """Если среза остатков на день нет — берётся ближайший доступный ≤ дня."""
    db.add_all([
        models.StorageCost(nm_id="1", article="A1", volume=1.0),
        _stock("A1", 10, day=date(2026, 8, 20)),
        models.WbDetailRow(op_key="sr:st1", source="excel", article="",
                           doc_type_name="Хранение", sale_dt=date(2026, 9, 4),
                           paid_storage=100.0),
    ])
    db.commit()
    out = storage_split(db, date_from=date(2026, 9, 1), date_to=date(2026, 9, 5))
    assert out == {"A1": 100.0}


def test_storage_split_empty_when_no_storage(db):
    db.add_all([
        models.StorageCost(nm_id="1", article="A1", volume=1.0),
        _stock("A1", 10),
    ])
    db.commit()
    assert storage_split(db) == {}


def test_storage_split_uses_tariff_multiplier(db):
    """Вес = объём × тариф: товар с дорогим хранением получает большую долю."""
    db.add_all([
        models.StorageCost(nm_id="1", article="A1", volume=2.0, storage_price=3.0),
        models.StorageCost(nm_id="2", article="B2", volume=1.0, storage_price=1.0),
        _stock("A1", 10),   # вес 2*3*10 = 60
        _stock("B2", 10),   # вес 1*1*10 = 10, итого 70
        models.WbDetailRow(op_key="sr:st1", source="excel", article="",
                           doc_type_name="Хранение", sale_dt=date(2026, 9, 4),
                           paid_storage=700.0),
    ])
    db.commit()
    out = storage_split(db, date_from=date(2026, 9, 1), date_to=date(2026, 9, 5))
    assert out == {"A1": 600.0, "B2": 100.0}  # 700 * 60/70, 700 * 10/70


def test_storage_split_prefers_weighty_snapshot_in_window(db):
    """Если в окне ±7 дней лежит более «полный» срез — берётся он, а не соседний пустой."""
    db.add_all([
        models.StorageCost(nm_id="1", article="A1", volume=2.0),
        _stock("A1", 10, day=date(2026, 9, 2)),     # вес 20
        _stock("B2", 100, day=date(2026, 9, 7)),    # вес 1*100? B2 нет в vol_rate -> пусто
        models.StorageCost(nm_id="3", article="B3", volume=1.0),
        _stock("B3", 100, day=date(2026, 9, 7)),    # вес 1*100 = 100 (более полный)
        models.WbDetailRow(op_key="sr:st1", source="excel", article="",
                           doc_type_name="Хранение", sale_dt=date(2026, 9, 4),
                           paid_storage=100.0),
    ])
    db.commit()
    # срез 09-07 (вес 100) полнее 09-02 (вес 20) → используем его
    out = storage_split(db, date_from=date(2026, 9, 1), date_to=date(2026, 9, 5))
    assert out == {"B3": 100.0}


def test_margin_detail_includes_storage_est(db):
    """Безартикульное хранение попадает в «Хранение» строки товара (оценка)."""
    db.add_all([
        models.StorageCost(nm_id="1", article="A1", volume=2.0),
        _stock("A1", 100),
        models.WbDetailRow(op_key="sr:s1", source="excel", article="A1",
                           doc_type_name="Продажа", sale_dt=date(2026, 9, 4),
                           quantity=1, retail_amount=1000.0, for_pay=900.0),
        models.WbDetailRow(op_key="sr:st1", source="excel", article="",
                           doc_type_name="Хранение", sale_dt=date(2026, 9, 4),
                           paid_storage=100.0),
    ])
    db.commit()
    out = margin_detail_dataframe(db, date_from=date(2026, 9, 1), date_to=date(2026, 9, 5))
    row = out.iloc[0]
    assert row["storage"] == 100.0


def test_margin_detail_storage_est_case_insensitive(db):
    """Оценка хранения матчится по UPPER(article), как и себестоимость."""
    db.add_all([
        models.StorageCost(nm_id="1", article="JZ2-XYZ", volume=3.0),
        _stock("JZ2-XYZ", 50),
        models.WbDetailRow(op_key="sr:s1", source="excel", article="jz2-xyz",
                           doc_type_name="Продажа", sale_dt=date(2026, 9, 4),
                           quantity=1, retail_amount=1000.0, for_pay=900.0),
        models.WbDetailRow(op_key="sr:st1", source="excel", article="",
                           doc_type_name="Хранение", sale_dt=date(2026, 9, 4),
                           paid_storage=60.0),
    ])
    db.commit()
    out = margin_detail_dataframe(db, date_from=date(2026, 9, 1), date_to=date(2026, 9, 5))
    assert out.iloc[0]["storage"] == 60.0


def test_storage_split_weights_sold_items_when_snapshot_empty(db):
    """Проданные товары без строк в стоках получают долю хранения.

    Товар A1 полностью продан (остатка нет), B2 лежит на складе. Плата хранения
    не должна уходить целиком на B2 — A1 тоже занимал место (≈половину периода).
    Вес считается по «остаток + проданное × 0.5», даже если среза стоков для
    артикула нет вовсе.
    """
    db.add_all([
        models.StorageCost(nm_id="1", article="A1", volume=2.0),
        models.StorageCost(nm_id="2", article="B2", volume=1.0),
        _stock("B2", 300),               # A1 строк в стоках нет — распродан
        models.WbDetailRow(op_key="sr:s1", source="excel", article="A1",
                           doc_type_name="Продажа", sale_dt=date(2026, 9, 4),
                           quantity=100, retail_amount=1000.0, for_pay=900.0),
        models.WbDetailRow(op_key="sr:st1", source="excel", article="",
                           doc_type_name="Хранение", sale_dt=date(2026, 9, 4),
                           paid_storage=100.0),
    ])
    db.commit()
    out = storage_split(db, date_from=date(2026, 9, 1), date_to=date(2026, 9, 5))
    # A1 вес 2*100*0.5=100, B2 вес 1*300=300, итого 400 → A1 25.0, B2 75.0
    assert out == {"A1": 25.0, "B2": 75.0}


def test_storage_split_sold_fraction_adds_to_existing_stock(db):
    """К остатку прибавляется доля проданного: вес = объём × (остаток + sold×0.5)."""
    db.add_all([
        models.StorageCost(nm_id="1", article="A1", volume=2.0),
        _stock("A1", 10),
        models.WbDetailRow(op_key="sr:s1", source="excel", article="A1",
                           doc_type_name="Продажа", sale_dt=date(2026, 9, 4),
                           quantity=6, retail_amount=1000.0, for_pay=900.0),
        models.WbDetailRow(op_key="sr:st1", source="excel", article="",
                           doc_type_name="Хранение", sale_dt=date(2026, 9, 4),
                           paid_storage=100.0),
    ])
    db.commit()
    # вес = 2 * (10 + 6*0.5) = 26 → A1 забирает 100% платы
    assert storage_split(db, date_from=date(2026, 9, 1), date_to=date(2026, 9, 5)) \
        == {"A1": 100.0}
    # sold_fraction=0 → старое поведение, только остаток (10): вес 20
    assert storage_split(db, date_from=date(2026, 9, 1), date_to=date(2026, 9, 5),
                         sold_fraction=0.0) == {"A1": 100.0}
