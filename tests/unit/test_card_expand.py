"""Тесты разбивки dimensions/characteristics/subject при экспорте карточек."""
import pandas as pd

from app.services.refresh import expand_wb_card_export


def test_expand_dimensions():
    df = pd.DataFrame([
        {"vendorCode": "A1", "dimensions": {"length": 10, "width": 20, "height": 30, "weightGross": 500, "weightNet": 480}},
        {"vendorCode": "A2", "dimensions": None},
        {"vendorCode": "A3", "title": "Без dims"},
    ])
    out = expand_wb_card_export(df)
    assert "Длина, мм" in out.columns
    assert out.loc[0, "Длина, мм"] == 10
    assert out.loc[0, "Вес брутто, г"] == 500
    assert pd.isna(out.loc[1, "Длина, мм"])
    assert pd.isna(out.loc[2, "Длина, мм"])


def test_expand_characteristics():
    df = pd.DataFrame([
        {
            "vendorCode": "B1",
            "characteristics": [
                {"name": "Цвет", "value": "Серый"},
                {"name": "Материал", "value": "Хлопок"},
                {"name": "Цвет", "value": "Серый"},
            ],
        },
        {"vendorCode": "B2", "characteristics": []},
    ])
    out = expand_wb_card_export(df)
    assert "Хар-ка: Цвет" in out.columns
    assert "Хар-ка: Материал" in out.columns
    assert out.loc[0, "Хар-ка: Цвет"] == "Серый"
    assert out.loc[0, "Хар-ка: Материал"] == "Хлопок"
    assert out.loc[1, "Хар-ка: Цвет"] == ""


def test_expand_subject():
    df = pd.DataFrame([
        {"vendorCode": "C1", "subject": {"id": 1, "name": "Галстуки", "parentId": 10, "parentName": "Аксессуары"}},
        {"vendorCode": "C2", "subject": {"name": "Платья"}},
    ])
    out = expand_wb_card_export(df)
    assert out.loc[0, "Предмет"] == "Галстуки"
    assert out.loc[0, "Предмет (родитель)"] == "Аксессуары"
    assert out.loc[1, "Предмет"] == "Платья"
    assert out.loc[1, "Предмет (родитель)"] == ""


def test_expand_empty_df():
    assert expand_wb_card_export(pd.DataFrame()) is None or expand_wb_card_export(pd.DataFrame()).empty


def test_expand_preserves_original_columns():
    df = pd.DataFrame([
        {"vendorCode": "X1", "dimensions": {"length": 5}, "title": "Test"},
    ])
    out = expand_wb_card_export(df)
    assert "vendorCode" in out.columns
    assert "title" in out.columns
    assert "Длина, мм" in out.columns