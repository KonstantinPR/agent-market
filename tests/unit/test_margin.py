import pandas as pd

from app.services.margin import compute_margin, empty_margin_df, margin_columns


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
    assert row["margin"] == 400.0  # income - net_cost * sells
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