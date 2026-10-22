# -*- coding: utf-8 -*-
"""Тесты сервиса ABC/XYZ (app/services/xyz.py).

Проверяются:
- раскладка по стабильности спроса (X/Y/Z) и отнесение к «Новинке»;
- отсев нулевых продаж и товаров без спроса;
- единый алгоритм ABC (доли выручки 80 % / 95 %);
- матрица ABC × XYZ 3×4 и согласованность её итогов.
"""
from datetime import date

import pandas as pd

from app import models
from app.services import xyz as xyz_service


def _wb_id(db) -> int:
    return db.query(models.Marketplace).filter_by(code="wb").one().id


def _oz_id(db) -> int:
    return db.query(models.Marketplace).filter_by(code="ozon").one().id


def _sale(db, article, day, qty, mp="wb", source="v5", revenue=0.0):
    mp_id = _wb_id(db) if mp == "wb" else _oz_id(db)
    db.add(models.Sale(marketplace_id=mp_id, date=day, article=article,
                       source=source, quantity=qty, revenue=revenue))
    db.commit()


def _months(count=12, end=date(2026, 9, 1)):
    """Список (месяц, дата) назад от ``end`` — по одной дате на месяц."""
    out = []
    y, m = end.year, end.month
    for i in range(count):
        mm = m - i
        yy = y
        while mm <= 0:
            mm += 12
            yy -= 1
        out.append((f"{yy:04d}-{mm:02d}", date(yy, mm, 15)))
    return list(reversed(out))


# ------------------------------------------------------------------ classify
def test_classify_splits_stable_moderate_and_spiky():
    """Низкий CV → X, средний → Y, высокий → Z (ранговые терцили)."""
    months = [m for m, _ in _months(12)]
    series = []
    for i, (_, day) in enumerate(_months(12)):
        series.append(("STABLE", months[i], 10))
        series.append(("MID", months[i], [10, 12, 8, 12, 8, 12, 8, 12, 8, 12, 8, 12][i]))
        series.append(("SPIKE", months[i], [40, 2, 40, 2, 40, 2, 40, 2, 40, 2, 40, 2][i]))
    df = pd.DataFrame(series, columns=["article", "month", "qty"])

    out = xyz_service.classify(df, months, min_months=6)
    cls = dict(zip(out["article"], out["xyz_class"]))

    assert cls["STABLE"] == xyz_service.XLVL
    assert cls["MID"] == xyz_service.YLVL
    assert cls["SPIKE"] == xyz_service.ZLVL


def test_classify_new_when_not_enough_active_months():
    """Мало активных месяцев (< порога) → «Новинка», а не X/Y/Z."""
    months = [m for m, _ in _months(12)]
    df = pd.DataFrame(
        [("FRESH", months[i], 10 if i < 3 else 0) for i in range(12)],
        columns=["article", "month", "qty"],
    )
    out = xyz_service.classify(df, months, min_months=6)
    assert out.loc[0, "xyz_class"] == xyz_service.NEWLVL
    assert out.loc[0, "active_months"] == 3


def test_classify_zero_demand_article_is_new():
    """Товар без продаж (μ = 0) не должен попасть в X/Y/Z."""
    months = [m for m, _ in _months(12)]
    df = pd.DataFrame(
        [("ZERO", months[i], 0) for i in range(12)],
        columns=["article", "month", "qty"],
    )
    out = xyz_service.classify(df, months, min_months=6)
    assert out.loc[0, "xyz_class"] == xyz_service.NEWLVL
    assert out.loc[0, "avg_month_qty"] == 0.0


def test_classify_empty_input():
    months = [m for m, _ in _months(12)]
    out = xyz_service.classify(pd.DataFrame(columns=["article", "month", "qty"]), months, 6)
    assert out.empty
    assert list(out.columns) == ["article", "avg_month_qty", "cv_pct",
                                 "active_months", "xyz_class"]


# ------------------------------------------------------------------- серия
def test_xyz_series_skips_zero_qty_and_other_marketplace(db):
    """В серию попадают только WB и только quantity > 0."""
    days = _months(3)
    for _, day in days:
        _sale(db, "GOOD", day, 5)
        _sale(db, "ZEROQTY", day, 0)
        _sale(db, "OZ", day, 7, mp="ozon")

    df = xyz_service.xyz_series(db, days[0][1], days[-1][1], "wb")
    assert set(df["article"]) == {"GOOD"}
    assert df["qty"].sum() == 15


def test_xyz_series_ignores_source_detail(db):
    """Источник ``detail`` (детализация) не должен дублировать продажи."""
    days = _months(2)
    for _, day in days:
        _sale(db, "A1", day, 4, source="v5")
        _sale(db, "A1", day, 4, source="detail")
    df = xyz_service.xyz_series(db, days[0][1], days[-1][1], "wb")
    assert df["qty"].sum() == 8


# ------------------------------------------------------------------ ABC
def test_abc_ranks_by_cumulative_revenue():
    """A — до 80 % выручки, B — 80–95 %, C — остальное."""
    df = pd.DataFrame({
        "article": ["A1", "A2", "A3", "A4"],
        "revenue": [50.0, 30.0, 15.0, 5.0],
    })
    ranks = xyz_service.abc_ranks(df)
    by_article = dict(zip(df["article"], ranks))
    assert by_article["A1"] == "A"
    assert by_article["A2"] == "A"     # накопленная доля ровно 80 %
    assert by_article["A3"] == "B"     # 95 %
    assert by_article["A4"] == "C"


def test_abc_ranks_without_revenue_is_c():
    df = pd.DataFrame({"article": ["A1", "A2"], "revenue": [0.0, 0.0]})
    assert set(xyz_service.abc_ranks(df)) == {"C"}


# ------------------------------------------------------------------ матрица
def test_abc_xyz_matrix_is_3x4_and_totals_cover_all_articles():
    ab = pd.DataFrame({
        "article": ["A1", "A2", "A3", "A4"],
        "revenue": [100.0, 50.0, 30.0, 10.0],
        "margin": [40.0, 10.0, 5.0, 1.0],
    })
    xyz_map = {"A1": xyz_service.XLVL, "A2": xyz_service.YLVL,
               "A3": xyz_service.ZLVL}          # A4 без класса → «Новинка»

    m = xyz_service.abc_xyz_matrix(ab, xyz_map)
    assert m["abc_rows"] == ["A", "B", "C"]
    assert m["xyz_cols"] == ["X", "Y", "Z", "NEW"]
    assert len(m["cells"]) == 12

    by_key = {(c["abc"], c["xyz"]): c for c in m["cells"]}
    # A1 (100) и A2 (50) — накопленная доля ≤ 80 % → A; A3 (30) → B; A4 (10) → C.
    assert by_key[("A", "X")]["articles"] == 1
    assert by_key[("A", "Y")]["articles"] == 1
    assert by_key[("B", "Z")]["articles"] == 1
    assert by_key[("C", "NEW")]["articles"] == 1

    # Итоги = сумма по всем ячейкам и включают ВСЕ артикулы.
    assert m["totals"]["articles"] == 4
    assert m["totals"]["revenue"] == 190.0
    assert m["totals"]["margin"] == 56.0


def test_abc_xyz_matrix_empty_input():
    m = xyz_service.abc_xyz_matrix(pd.DataFrame(), {})
    assert len(m["cells"]) == 12
    assert m["totals"]["articles"] == 0


# ------------------------------------------------------------------ строки
def test_xyz_rows_meta_and_abc_column(db):
    """Сводка строк: meta с порогами, ABC у каждой строки."""
    days = _months(6)
    for i, (_, day) in enumerate(days):
        _sale(db, "STAB", day, 10)
        _sale(db, "FRESH", day, 1 if i < 2 else 0)

    ab = pd.DataFrame({
        "article": ["STAB", "FRESH"],
        "name": ["Стабильный", "Новый"],
        "revenue": [700.0, 300.0],
        "margin": [200.0, 10.0],
    })
    res = xyz_service.xyz_rows(db, days[0][1], days[-1][1], "wb",
                                window_months=6, min_active_months=4, ab_rows=ab)
    rows = {r["article"]: r for r in res["rows"]}
    assert set(rows) == {"STAB", "FRESH"}
    assert rows["STAB"]["abc"] == "A"
    assert rows["FRESH"]["abc"] == "C"
    assert rows["STAB"]["xyz_class"] == xyz_service.XLVL
    assert rows["FRESH"]["xyz_class"] == xyz_service.NEWLVL
    assert rows["STAB"]["xyz_label"]
    assert res["count"] == 2

    meta = res["meta"]
    assert meta["window_months"] == 6
    assert meta["min_window_months"] == 6
    assert meta["min_active_months"] == 4
    assert meta["marketplace"] == "WB"
    assert meta["x_cv_threshold"] >= 0
    assert meta["y_cv_threshold"] >= meta["x_cv_threshold"]


def test_xyz_rows_window_extends_to_period_start(db):
    """Если период длиннее окна — окно расширяется до начала периода."""
    days = _months(12)
    for _, day in days:
        _sale(db, "A1", day, 3)

    res = xyz_service.xyz_rows(db, days[0][1], days[-1][1], "wb", window_months=3)
    assert res["meta"]["window_months"] == 12
    assert res["meta"]["date_from"] == str(days[0][1])
    assert res["meta"]["date_to"] == str(days[-1][1])


def test_xyz_rows_sorted_by_avg_qty_desc(db):
    days = _months(6)
    for _, day in days:
        _sale(db, "SMALL", day, 1)
        _sale(db, "BIG", day, 50)

    res = xyz_service.xyz_rows(db, days[0][1], days[-1][1], "wb", window_months=6)
    assert [r["article"] for r in res["rows"]] == ["BIG", "SMALL"]
