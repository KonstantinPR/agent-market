"""Юнит-тесты фильтрации экспорта по видимым колонкам («Вид таблицы» → cols)."""
import pandas as pd

from app.services.excel_io import project_export

RENAME = {
    "article": "Артикул",
    "name": "Наименование",
    "sells": "Продано, шт",
    "revenue": "Выручка, руб",
    "commission": "Комиссия, руб",
}


def _df():
    return pd.DataFrame({
        "article": ["AAA", "BBB"],
        "name": ["Кольцо", "Цепь"],
        "sells": [10, 4],
        "revenue": [1200.0, 800.0],
        "commission": [120.0, 80.0],
    })


def test_no_cols_returns_full():
    df, rename = project_export(_df(), RENAME, None)
    assert list(df.columns) == list(RENAME.keys())
    assert rename == RENAME


def test_cols_subset_filters_and_renames():
    df, rename = project_export(_df(), RENAME, "article,name,sells")
    assert list(df.columns) == ["article", "name", "sells"]
    assert rename == {"article": "Артикул", "name": "Наименование", "sells": "Продано, шт"}


def test_cols_preserves_key_order():
    df, rename = project_export(_df(), RENAME, "revenue,article,nonexistent")
    assert list(df.columns) == ["revenue", "article"]
    assert list(rename.keys()) == ["revenue", "article"]


def test_unknown_keys_ignored():
    df, rename = project_export(_df(), RENAME, "article,zzz,comission_typo,name")
    assert list(df.columns) == ["article", "name"]
    assert rename == {"article": "Артикул", "name": "Наименование"}


def test_empty_cols_returns_full():
    df, rename = project_export(_df(), RENAME, "")
    assert list(df.columns) == list(RENAME.keys())
    assert rename == RENAME


def test_key_in_rename_but_not_in_df_is_skipped():
    rename = dict(RENAME, srid="SRID")
    df, out_rename = project_export(_df(), rename, "article,srid")
    assert list(df.columns) == ["article"]
    assert out_rename == {"article": "Артикул"}


def test_rows_data_intact():
    df, _ = project_export(_df(), RENAME, "article,sells")
    assert df.loc[0, "sells"] == 10
    assert df.loc[1, "article"] == "BBB"