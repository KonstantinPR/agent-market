import pandas as pd
from datetime import date

from sqlalchemy import select

from app.services.margin import (compare_margin_periods, compute_margin, empty_margin_df,
                                 margin_columns, margin_detail_dataframe, storage_split,
                                 ozon_margin_detail_dataframe, funnel_dataframe)
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


def test_margin_detail_stock_columns(db):
    """Остатки WB попадают в детализацию: срез = последняя дата <= date_to,
    количество суммируется по складам; артикул без стоков получает 0."""
    db.add_all([
        models.WbDetailRow(op_key="sr:s1", source="excel", article="A1",
                           doc_type_name="Продажа", sale_dt=date(2026, 9, 4),
                           quantity=1, retail_amount=1000.0, for_pay=900.0),
        models.WbDetailRow(op_key="sr:s2", source="excel", article="B2",
                           doc_type_name="Продажа", sale_dt=date(2026, 9, 4),
                           quantity=1, retail_amount=1000.0, for_pay=900.0),
        models.Stock(marketplace_id=1, date=date(2026, 9, 2), article="A1",
                     warehouse="WH1", chrt_id="1", quantity=5, quantity_full=7, in_way=2),
        models.Stock(marketplace_id=1, date=date(2026, 9, 4), article="A1",
                     warehouse="WH1", chrt_id="1", quantity=3, quantity_full=4, in_way=0),
        models.Stock(marketplace_id=1, date=date(2026, 9, 4), article="A1",
                     warehouse="WH2", chrt_id="2", quantity=5, quantity_full=6, in_way=3),
    ])
    db.commit()
    out = margin_detail_dataframe(db, date_from=date(2026, 9, 1), date_to=date(2026, 9, 5))
    a1 = out[out["article"] == "A1"].iloc[0]
    assert a1["stock_qty"] == 8          # 3+5 на срез 09-04
    assert a1["stock_total"] == 10       # 4+6
    assert a1["stock_in_way"] == 3       # 0+3
    b2 = out[out["article"] == "B2"].iloc[0]
    assert b2["stock_qty"] == 0
    assert b2["stock_total"] == 0
    assert b2["stock_in_way"] == 0


def test_margin_detail_stock_no_snapshot_in_window(db):
    """Окно раньше первого среза стоков → остатки 0, без падений."""
    db.add_all([
        models.WbDetailRow(op_key="sr:s1", source="excel", article="A1",
                           doc_type_name="Продажа", sale_dt=date(2026, 8, 4),
                           quantity=1, retail_amount=1000.0, for_pay=900.0),
        models.Stock(marketplace_id=1, date=date(2026, 9, 2), article="A1",
                     warehouse="WH1", chrt_id="1", quantity=5, quantity_full=7, in_way=2),
    ])
    db.commit()
    out = margin_detail_dataframe(db, date_from=date(2026, 8, 1), date_to=date(2026, 8, 5))
    assert out.iloc[0]["stock_qty"] == 0


def test_margin_detail_stock_absent_without_date_to(db):
    """Без date_to остатки не подтягиваются и колонки нулевые."""
    db.add_all([
        models.WbDetailRow(op_key="sr:s1", source="excel", article="A1",
                           doc_type_name="Продажа", sale_dt=date(2026, 9, 4),
                           quantity=1, retail_amount=1000.0, for_pay=900.0),
        models.Stock(marketplace_id=1, date=date(2026, 9, 4), article="A1",
                     warehouse="WH1", chrt_id="1", quantity=9, quantity_full=9, in_way=1),
    ])
    db.commit()
    out = margin_detail_dataframe(db)
    assert out.iloc[0]["stock_qty"] == 0
    assert out.iloc[0]["stock_in_way"] == 0


def test_margin_detail_storage_share_is_global_under_filter(db):
    """Фильтр в строке поиска не пересчитывает распределение хранения.

    Полный вид: A1 получает 500×200/500=200.0 (объём 2×остаток 100), B2 — 300.0.
    С фильтром по A1 доля остаётся глобальной (200.0), а не весь пул 500.
    """
    db.add_all([
        models.StorageCost(nm_id="1", article="A1", volume=2.0),
        models.StorageCost(nm_id="2", article="B2", volume=1.0),
        _stock("A1", 100),
        _stock("B2", 300),
        models.WbDetailRow(op_key="sr:s1", source="excel", article="A1",
                           doc_type_name="Продажа", sale_dt=date(2026, 9, 4),
                           quantity=0, retail_amount=1000.0, for_pay=900.0),
        models.WbDetailRow(op_key="sr:s2", source="excel", article="B2",
                           doc_type_name="Продажа", sale_dt=date(2026, 9, 4),
                           quantity=0, retail_amount=800.0, for_pay=700.0),
        models.WbDetailRow(op_key="sr:st1", source="excel", article="",
                           doc_type_name="Хранение", sale_dt=date(2026, 9, 4),
                           paid_storage=500.0),
    ])
    db.commit()
    full = margin_detail_dataframe(db, date_from=date(2026, 9, 1), date_to=date(2026, 9, 5))
    got = margin_detail_dataframe(db, date_from=date(2026, 9, 1), date_to=date(2026, 9, 5),
                                  article_like="A1")
    assert len(got) == 1
    a1 = got.loc[got["article"] == "A1"].iloc[0]
    full_a1 = full.loc[full["article"] == "A1"].iloc[0]
    assert a1["storage"] == full_a1["storage"] == 200.0


def test_margin_detail_articleless_logistics_global_under_filter(db):
    """Безартикульная логистика распределяется по глобальным весам продаж.

    A1 (1 шт) и B2 (2 шт) — веса 1 и 2, пул логистики 100: A1 получает 33.33
    и при фильтре по A1 (а не весь пул 100).
    """
    db.add_all([
        models.WbDetailRow(op_key="sr:s1", source="excel", article="A1",
                           doc_type_name="Продажа", sale_dt=date(2026, 9, 4),
                           quantity=1, retail_amount=1000.0, for_pay=900.0),
        models.WbDetailRow(op_key="sr:s2", source="excel", article="B2",
                           doc_type_name="Продажа", sale_dt=date(2026, 9, 4),
                           quantity=2, retail_amount=1600.0, for_pay=1400.0),
        models.WbDetailRow(op_key="sr:lg1", source="excel", article="",
                           doc_type_name="Логистика", sale_dt=date(2026, 9, 4),
                           delivery_service=100.0),
    ])
    db.commit()
    full = margin_detail_dataframe(db, date_from=date(2026, 9, 1), date_to=date(2026, 9, 5))
    got = margin_detail_dataframe(db, date_from=date(2026, 9, 1), date_to=date(2026, 9, 5),
                                  article_like="A1")
    assert len(got) == 1
    a1 = got.loc[got["article"] == "A1"].iloc[0]
    full_a1 = full.loc[full["article"] == "A1"].iloc[0]
    assert a1["logistics"] == full_a1["logistics"] == 33.33


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


def test_margin_detail_logistics_split_and_per_unit(db):
    """Логистика туда/обратно, возвраты, *_per_one и return_rate."""
    db.add_all([
        models.WbDetailRow(op_key="sr:s1", source="excel", article="A1",
                           doc_type_name="Продажа", sale_dt=date(2026, 9, 1),
                           quantity=10, retail_amount=5000.0, for_pay=3000.0,
                           delivery_service=200.0, ppvz_sales_commission=200.0),
        models.WbDetailRow(op_key="sr:r1", source="excel", article="A1",
                           doc_type_name="Возврат", sale_dt=date(2026, 9, 2),
                           quantity=2, retail_amount=1000.0, for_pay=1000.0,
                           delivery_service=50.0),
    ])
    db.commit()
    out = margin_detail_dataframe(db, default_net_cost=100.0)
    row = out.iloc[0]
    # sells = 10 − 2 = 8; returns_qty = 2
    assert row["sells"] == 8
    assert row["returns_qty"] == 2
    # logistics_out = 200 (Продажа), logistics_in = 50 (Возврат)
    assert row["logistics_out"] == 200.0
    assert row["logistics_in"] == 50.0
    assert row["logistics"] == 250.0
    # *_per_one: sells=8 (комиссия только с Продажи = 200)
    assert row["commission_per_one"] == round(200.0 / 8, 2)
    assert row["logistics_per_one"] == round(250.0 / 8, 2)
    assert row["logistics_out_per_one"] == round(200.0 / 8, 2)
    assert row["logistics_in_per_one"] == round(50.0 / 8, 2)
    assert row["income_per_one"] == round(2000.0 / 8, 2)
    assert row["revenue_per_one"] == round(4000.0 / 8, 2)
    assert row["margin_gross_per_one"] == round(
        (2000.0 - 250.0 - 0.0 - 0.0) / 8, 2)  # no storage estimate → 0
    # return_rate = 2 / (8 + 2) * 100 = 20%
    assert row["return_rate"] == 20.0


def test_margin_detail_per_unit_zero_sells_no_division(db):
    """При sells=0 (продажа и возврат уравновесили друг друга) все *_per_one = 0,
    return_rate считает по знаменателю sells+returns_qty."""
    db.add_all([
        models.WbDetailRow(op_key="sr:s1", source="excel", article="A2",
                           doc_type_name="Продажа", sale_dt=date(2026, 9, 1),
                           quantity=1, retail_amount=500.0, for_pay=500.0),
        models.WbDetailRow(op_key="sr:r1", source="excel", article="A2",
                           doc_type_name="Возврат", sale_dt=date(2026, 9, 2),
                           quantity=1, retail_amount=500.0, for_pay=500.0,
                           delivery_service=30.0),
    ])
    db.commit()
    out = margin_detail_dataframe(db)
    row = out.iloc[0]
    assert row["sells"] == 0
    assert row["returns_qty"] == 1
    assert row["logistics_out"] == 0.0
    assert row["logistics_in"] == 30.0
    assert row["commission_per_one"] == 0.0
    assert row["logistics_per_one"] == 0.0
    assert row["return_rate"] == 100.0  # 1 / (0+1) * 100


def _oz_detail(op_key, article, qty=1, ret_qty=0, price=1000.0, amount=1000.0,
               commission=-100.0, standard_fee=-50.0, income=850.0, day=date(2026, 8, 1),
               barcode=""):
    return models.OzonDetailRow(
        op_key=op_key, source="api", date=day, posting_number="p" + op_key,
        offer_id=article, name="Товар " + article, sku="", barcode=barcode,
        quantity=qty, seller_price=price, amount=amount,
        commission_ratio=0.0, commission=commission,
        standard_fee=standard_fee, income=income, return_qty=ret_qty,
        return_total=0.0,
    )


def test_ozon_margin_detail_matches_product_and_nm(db):
    """Ozon-аналитика: себестоимость по UPPER(артикул), артикул WB из карточек."""
    mp_oz = db.execute(
        select(models.Marketplace.id).where(models.Marketplace.code == "ozon")).scalar_one()
    db.add(models.Product(article="OZ-ONE", name="Озон-товар", net_cost=300.0))
    db.add(models.MarketplaceCard(marketplace_id=mp_oz, chrt_id="c1",
                                  vendor_code="oz-one", nm_id="777888"))
    db.add(_oz_detail("k1", "oz-one"))
    db.commit()
    out = ozon_margin_detail_dataframe(db)
    row = out.iloc[0]
    assert row["article"] == "oz-one"
    assert row["nm_id"] == "777888"
    assert row["name"] == "Озон-товар"
    assert row["sells"] == 1
    assert row["net_cost"] == 300.0
    assert row["net_cost_est"] == False
    assert row["income"] == 850.0
    assert row["margin"] == 850.0 - 300.0  # income − себестоимость×продано
    # безразмерный товар: размеров нет, артикул один
    assert row["sizes_count"] == 0
    assert row["offers_count"] == 1


def test_ozon_margin_detail_groups_sizes_into_base_by_default(db):
    """По умолчанию артикулы размеров свёрнуты в базовый артикул товара."""
    db.add(models.Product(article="TIE-RED", name="Галстук", net_cost=300.0))
    db.add(_oz_detail("k1", "TIE-RED-42", qty=1, amount=1000.0, income=800.0))
    db.add(_oz_detail("k2", "TIE-RED-43", qty=2, amount=2000.0, income=1500.0))
    db.commit()
    out = ozon_margin_detail_dataframe(db)
    assert len(out) == 1
    row = out.iloc[0]
    assert row["article"] == "TIE-RED"
    assert row["name"] == "Галстук"
    assert row["sells"] == 3
    assert row["sizes_count"] == 2
    assert row["offers_count"] == 2
    # себестоимость берётся у базового артикула и умножается на все продажи
    assert row["net_cost"] == 300.0
    assert row["margin"] == 800.0 + 1500.0 - 300.0 * 3


def test_ozon_margin_detail_by_size_keeps_full_offer(db):
    """Режим «в разрезе размеров»: строка — полный артикул, размер виден."""
    db.add(_oz_detail("k1", "TIE-RED-42", qty=1, amount=1000.0, income=800.0))
    db.add(_oz_detail("k2", "TIE-RED-43", qty=2, amount=2000.0, income=1500.0))
    db.commit()
    out = ozon_margin_detail_dataframe(db, by_size=True)
    assert sorted(out["article"]) == ["TIE-RED-42", "TIE-RED-43"]
    assert sorted(out["size"]) == ["42", "43"]
    assert out["sizes_count"].tolist() == [1, 1]
    assert out["offers_count"].tolist() == [1, 1]


def test_ozon_margin_detail_rolls_up_storage_and_accruals_to_base(db):
    """Хранение и начисления размеров суммируются в строку базового артикула."""
    db.add(_oz_detail("k1", "TIE-RED-42", qty=1, amount=1000.0, income=800.0))
    db.add(_oz_detail("k2", "TIE-RED-43", qty=1, amount=1000.0, income=700.0))
    db.add(models.OzonPlacement(
        op_key="2026-08-01|110001|T1", date=date(2026, 8, 1), sku="110001",
        offer_id="TIE-RED-42", warehouse="Т1", paid_quantity=1, paid_volume=0.1,
        storage=-30.0))
    db.add(models.OzonPlacement(
        op_key="2026-08-01|110002|T1", date=date(2026, 8, 1), sku="110002",
        offer_id="TIE-RED-43", warehouse="Т1", paid_quantity=1, paid_volume=0.1,
        storage=-12.0))
    db.add(models.OzonAccrual(
        op_key="2026-08-01|a1|sale|0|110001|0", date=date(2026, 8, 1),
        accrual_id="a1", bucket="sale", type_id=0, sku="110001",
        offer_id="TIE-RED-42", quantity=1, amount=1000.0,
        seller_price=1000.0, sale_price=1200.0))
    db.add(models.OzonAccrual(
        op_key="2026-08-01|a1|sale|0|110002|0", date=date(2026, 8, 1),
        accrual_id="a1", bucket="sale", type_id=0, sku="110002",
        offer_id="TIE-RED-43", quantity=1, amount=900.0,
        seller_price=1000.0, sale_price=1200.0))
    db.commit()
    out = ozon_margin_detail_dataframe(db)
    assert len(out) == 1
    row = out.iloc[0]
    assert row["article"] == "TIE-RED"
    assert row["storage"] == -42.0
    assert row["accrued_sale"] == 1900.0
    assert row["accrued_coverage"] == 1
    # income 800+700, services -50-50, начисления 1900 → accrued_diff = 0
    assert row["accrued_net"] == 1900.0
    # accrued_diff = начисления − income − services = 1900 − 1500 − (−100)
    assert row["accrued_diff"] == 500.0


def test_ozon_margin_detail_commissions_reference_not_double_subtracted(db):
    """Комиссия/услуги не вычитаются повторно: income уже чистый к перечислению,
    колонки commission/services справочные (в минусе у Ozon)."""
    db.add(_oz_detail("k1", "oz-2"))
    db.commit()
    out = ozon_margin_detail_dataframe(db)
    row = out.iloc[0]
    assert row["income"] == 850.0
    assert row["commission"] == -100.0
    assert row["services"] == -50.0
    assert row["margin"] == 850.0  # default_net_cost=0
    assert row["margin_pct"] == 85.0  # 850 / 1000 * 100


def test_ozon_margin_detail_aggregates_and_sorts(db):
    """Агрегация по артикулу: гросс-продажи + возвраты отдельно, сорт по марже."""
    db.add(_oz_detail("k1", "oz-a", qty=2, ret_qty=1, amount=2000.0, income=1700.0))
    db.add(_oz_detail("k2", "oz-a", qty=1, amount=1000.0, income=800.0))
    db.add(_oz_detail("k3", "oz-b", qty=1, amount=1000.0, income=900.0))
    db.commit()
    out = ozon_margin_detail_dataframe(db, default_net_cost=0.0)
    rec = {r["article"]: r for r in out.to_dict("records")}
    a = rec["oz-a"]
    assert a["sells"] == 3
    assert a["returns_qty"] == 1
    assert a["revenue"] == 3000.0
    assert a["income"] == 2500.0
    assert a["postings"] == 2
    assert a["return_rate"] == 25.0  # 1 / (3+1) * 100
    assert out.iloc[0]["article"] == "oz-a"  # 2500 > 900


def test_ozon_margin_detail_fallback_barcode(db):
    """Если артикул не матчится — фолбэк по barcode строки детализации."""
    db.add(models.Product(article="PARENT-1", name="По баркоду", net_cost=400.0,
                          barcode="4607001234567"))
    db.add(_oz_detail("k1", "oz-unknown", price=1200.0, amount=1200.0,
                      income=900.0, barcode="4607001234567"))
    db.commit()
    out = ozon_margin_detail_dataframe(db)
    row = out.iloc[0]
    assert row["name"] == "По баркоду"
    assert row["net_cost"] == 400.0


def test_ozon_margin_detail_no_product_estimate(db):
    """Нет продукта → default_net_cost и флаг net_cost_est."""
    db.add(_oz_detail("k1", "oz-none"))
    db.commit()
    out = ozon_margin_detail_dataframe(db, default_net_cost=250.0)
    row = out.iloc[0]
    assert row["net_cost"] == 250.0


def test_ozon_margin_detail_accrued_buckets(db):
    """Точные начисления из ozon_accruals: корзины продажа/комиссия/логистика/
    услуги приходят в маржу без повторного вычитания; accrued_net = сумма;"""
    db.add(_oz_detail("k1", "oz-acc", qty=1, amount=1000.0, income=850.0,
                      commission=-100.0, standard_fee=-50.0))
    db.commit()
    db.add(models.OzonAccrual(
        op_key="2026-08-01|a1|sale|0|110001|0", date=date(2026, 8, 1),
        accrual_id="a1", bucket="sale", type_id=0, sku="110001",
        offer_id="oz-acc", quantity=1, amount=1000.0,
        seller_price=1000.0, sale_price=1200.0))
    db.add(models.OzonAccrual(
        op_key="2026-08-01|a1|commission|69|110001|0", date=date(2026, 8, 1),
        accrual_id="a1", bucket="commission", type_id=69, sku="110001",
        offer_id="oz-acc", quantity=1, amount=-100.0,
        seller_price=1000.0, sale_price=1200.0))
    db.add(models.OzonAccrual(
        op_key="2026-08-01|a1|logistics|32|110001|0", date=date(2026, 8, 1),
        accrual_id="a1", bucket="logistics", type_id=32, sku="110001",
        offer_id="oz-acc", quantity=1, amount=-30.0,
        seller_price=0.0, sale_price=0.0))
    db.add(models.OzonAccrual(
        op_key="2026-08-01|a2|services|5|110001|0", date=date(2026, 8, 1),
        accrual_id="a2", bucket="services", type_id=5, sku="110001",
        offer_id="oz-acc", quantity=1, amount=-20.0,
        seller_price=0.0, sale_price=0.0))
    db.add(models.OzonAccrual(
        op_key="2026-08-01|a3|other|76||0", date=date(2026, 8, 1),
        accrual_id="a3", bucket="other", type_id=76, sku="",
        offer_id="", quantity=0, amount=-5.0,
        seller_price=0.0, sale_price=0.0))
    db.commit()
    out = ozon_margin_detail_dataframe(db)
    row = out.iloc[0]
    assert row["accrued_sale"] == 1000.0
    assert row["accrued_commission"] == -100.0
    assert row["accrued_logistics"] == -30.0
    assert row["accrued_services"] == -20.0
    assert row["accrued_other"] == 0.0  # NON_ITEM без артикула не привязывается
    assert row["accrued_net"] == 850.0  # 1000 − 100 − 30 − 20
    assert row["accrued_coverage"] == 1
    assert row["has_detail"] == 1
    # маржа и раньше считалась по income (комиссия/услуги уже вычтены):
    # начисления их подтверждают, а не вычитают повторно.
    assert row["margin"] == row["income"]
    # прибыль по начислениям: точная «на р/с» минус себестоимость×продано
    assert row["margin_accrued"] == 850.0


def test_ozon_margin_detail_accrual_only_article_without_detail(db):
    """Артикул с начислениями, но без строк детализации, попадает в отчёт."""
    db.add(_oz_detail("k1", "oz-with", qty=1, income=500.0))
    db.add(models.OzonAccrual(
        op_key="a1", date=date(2026, 8, 1), accrual_id="a1", bucket="logistics",
        type_id=32, sku="999", offer_id="oz-only", quantity=1, amount=-120.0,
        seller_price=0.0, sale_price=0.0))
    db.commit()
    out = ozon_margin_detail_dataframe(db)
    rec = {r["article"]: r for r in out.to_dict("records")}
    assert "oz-only" in rec
    only = rec["oz-only"]
    assert only["sells"] == 0
    assert only["has_detail"] == 0
    assert only["accrued_logistics"] == -120.0
    assert only["accrued_net"] == -120.0
    assert only["accrued_coverage"] == 1
    assert only["margin_accrued"] == -120.0


def test_ozon_margin_detail_accrued_fields_empty_without_accruals(db):
    """Без начислений: accrued_* нулевые, margin_accrued пусто (None)."""
    db.add(_oz_detail("k1", "oz-none"))
    db.commit()
    out = ozon_margin_detail_dataframe(db)
    row = out.iloc[0]
    assert row["accrued_net"] == 0.0
    assert row["accrued_coverage"] == 0
    assert row["margin_accrued"] is None
    assert row["net_cost_est"] == True


def _funnel_row(**kw):
    base = {
        "date_from": date(2026, 9, 1), "date_to": date(2026, 9, 10),
        "nm_id": "1001", "article": "A1", "views": 100, "opens": 20,
        "adds": 5, "orders": 4, "cancelled": 1, "buyouts": 3,
        "avg_price": 1000.0, "revenue": 4000.0, "buyout_sum": 3000.0,
        "subject_name": "Куртки", "brand_name": "Бренд",
        "product_rating": 8.2, "feedback_rating": 4.8,
        "stock_wb": 10, "stock_mp": 2, "stock_balance_sum": 8.0,
        "cancel_sum": 1000.0, "avg_orders_per_day": 0.4,
        "share_order_percent": 50.0, "add_to_wishlist": 3,
        "time_to_ready_min": 60, "localization_percent": 100.0,
        "conv_to_cart_percent": 5.0, "conv_cart_to_order_percent": 80.0,
        "conv_buyout_percent": 75.0,
        "wb_club_order_count": 1, "wb_club_order_sum": 1000.0,
        "wb_club_buyout_count": 1, "wb_club_buyout_sum": 800.0,
        "wb_club_cancel_count": 0, "wb_club_cancel_sum": 0.0,
        "wb_club_avg_price": 1000.0, "wb_club_buyout_percent": 80.0,
        "wb_club_avg_orders_per_day": 0.1,
        "title": "", "subject_id": "101", "tags": "",
        "past_json": '{"orderCount": 3, "orderSum": 2700.0, "openCount": 10}',
        "comparison_json": '{"orderCountDynamic": 33.3, "openCountDynamic": 100.0}',
    }
    base.update(kw)
    return models.FunnelMetric(**base)


def test_funnel_dataframe_full_fields_margin_and_storage_separate(db):
    """Воронка маржи: весь набор полей funnel_metric, маржа до расходов WB,
    хранение отдельной колонкой (не входит в маржу)."""
    db.add(models.Product(article="A1", name="Товар из products", net_cost=300.0))
    db.add(_funnel_row())
    db.add_all([
        models.WbDetailRow(op_key="sr:st1", source="excel", article="",
                           doc_type_name="Хранение", sale_dt=date(2026, 9, 4),
                           paid_storage=100.0),
        models.StorageCost(nm_id="1", article="A1", volume=2.0),
        _stock("A1", 10),
    ])
    db.commit()
    df = funnel_dataframe(db, date_from=date(2026, 9, 1), date_to=date(2026, 9, 10))
    assert df.attrs["matched"] is True
    assert df.attrs["date_from"] == "2026-09-01"
    assert len(df) == 1
    row = df.iloc[0]
    assert row["name"] == "Товар из products"            # title пуст → products.name
    assert row["views"] == 100 and row["opens"] == 20
    assert row["stock_wb"] == 10 and row["subject_name"] == "Куртки"
    assert row["conv_to_cart_percent"] == 5.0
    assert row["conv_cart_to_order_percent"] == 80.0
    assert row["conv_buyout_percent"] == 75.0
    assert row["wb_club_buyout_percent"] == 80.0
    assert row["past_orders"] == 3 and row["past_revenue"] == 2700.0
    assert row["dy_orders"] == 33.3 and row["dy_views"] == 100.0
    assert row["cart_pct"] == 5.0 and row["order_pct"] == 4.0
    assert row["margin"] == 4000.0 - 300.0 * 4            # выручка − net_cost×заказы
    assert row["margin_pct"] == 70.0
    assert row["storage_est"] == 100.0                    # отдельно, не в марже


def test_funnel_dataframe_name_prefers_funnel_title(db):
    db.add(models.Product(article="A1", name="products-name", net_cost=100.0))
    db.add(_funnel_row(title="Воронка-название", revenue=1000.0, orders=1))
    db.commit()
    df = funnel_dataframe(db, date_from=date(2026, 9, 1), date_to=date(2026, 9, 10))
    assert df.iloc[0]["name"] == "Воронка-название"


def test_funnel_dataframe_article_like_filters_output_only(db):
    db.add(models.Product(article="A1", name="Товар A1", net_cost=0.0))
    db.add(models.Product(article="B2", name="Товар B2", net_cost=0.0))
    db.add(_funnel_row(article="A1"))
    db.add(_funnel_row(article="B2", nm_id="1002"))
    db.commit()
    df = funnel_dataframe(db, date_from=date(2026, 9, 1), date_to=date(2026, 9, 10),
                          article_like="A1")
    assert list(df["article"]) == ["A1"]
