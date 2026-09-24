import io
from datetime import date

import pandas as pd

from app.providers.ozon import OzonProvider

DETAIL_COLS = {
    "date", "posting_number", "offer_id", "name", "sku", "barcode",
    "quantity", "seller_price", "amount", "commission_ratio", "commission",
    "standard_fee", "income", "return_qty", "return_total",
}
BUYOUT_COLS = {
    "posting_number", "offer_id", "name", "sku", "quantity", "seller_price",
    "buyout_price", "amount", "deduction_by_category_percent", "vat_percent",
}


def _prov():
    return OzonProvider(testing_mode=True)


def test_realization_posting_mock_columns_and_keys():
    df = _prov().get_realization_posting(8, 2026)
    assert not df.empty
    assert DETAIL_COLS.issubset(df.columns)
    assert (df["offer_id"].astype(str).str.strip() != "").all()
    assert (df["posting_number"].astype(str).str.strip() != "").all()
    assert (df["quantity"] >= 0).all()


def test_parse_realization_posting_rows_math():
    prov = _prov()
    data = {
        "header": {"stop_date": "2026-08-31T00:00:00Z"},
        "rows": [
            {
                "item": {"offer_id": "JBG-1", "name": "Товар", "sku": 111, "barcode": "222"},
                "order": {"posting_number": "123-456-1", "created_date": "2026-08-14T10:00:00Z"},
                "seller_price_per_instance": 1000.0,
                "commission_ratio": 0.15,
                "delivery_commission": {
                    "quantity": 2, "amount": 2000.0, "bonus": 100.0,
                    "standard_fee": 300.0, "total": 1800.0,
                },
                "return_commission": {},
            },
            {
                "item": {"offer_id": "JBG-2", "name": "Товар 2", "sku": 333, "barcode": "444"},
                "order": {"posting_number": "789-012-1", "created_date": "2026-08-20T10:00:00Z"},
                "commission_ratio": 0.1,
                "delivery_commission": {"quantity": 0, "amount": 0, "standard_fee": 0},
                "return_commission": {"quantity": 1, "total": 777.0},
            },
        ],
    }
    df = prov._parse_realization_posting_rows(data, date(2026, 8, 28))
    assert len(df) == 2
    sale = df.iloc[0]
    assert sale["offer_id"] == "JBG-1"
    assert sale["posting_number"] == "123-456-1"
    assert str(sale["date"]) == "2026-08-14"  # дата из created_date
    assert sale["quantity"] == 2
    assert sale["seller_price"] == 2000.0
    assert sale["standard_fee"] == 300.0
    assert sale["income"] == 1800.0
    assert sale["commission"] == -300.0  # standard_fee как комиссия
    ret = df.iloc[1]
    assert ret["return_qty"] == 1
    assert ret["return_total"] == 777.0
    assert ret["income"] == -777.0  # возврат без продажи: минус к перечислению


def test_get_sales_detail_iterates_months(monkeypatch):
    prov = _prov()
    prov.testing = False  # реальная ветка: помесячный цикл
    calls = []

    def fake(m, y):
        calls.append((m, y))
        return pd.DataFrame([{
            "date": date(y, m, 15), "posting_number": f"p-{m}", "offer_id": "A",
            "name": "", "sku": "", "barcode": "", "quantity": 1, "seller_price": 100.0,
            "amount": 100.0, "commission_ratio": 0.0, "commission": 0.0,
            "standard_fee": 0.0, "income": 100.0, "return_qty": 0, "return_total": 0,
        }])

    monkeypatch.setattr(prov, "get_realization_posting", fake)
    df = prov.get_sales_detail(date(2026, 7, 10), date(2026, 9, 5))
    assert calls == [(7, 2026), (8, 2026), (9, 2026)]
    assert len(df) == 3
    assert df["posting_number"].tolist() == ["p-7", "p-8", "p-9"]


def test_realization_posting_404_report_not_found_skips_month(monkeypatch):
    prov = _prov()
    prov.testing = False
    import requests

    class Resp:
        status_code = 404
        text = '{"code":5,"message":"Report was not found"}'

    def boom(url, payload, num_retries=4):
        raise requests.HTTPError("404 Client Error: Not Found for url: ...", response=Resp())

    monkeypatch.setattr(prov, "_post", boom)
    df = prov.get_realization_posting(9, 2026)
    assert df.empty
    assert prov._OzonProvider__last_month_skipped == (9, 2026)


def test_buyout_mock_columns():
    df = _prov().get_buyout(date(2026, 8, 1), date(2026, 8, 31))
    assert not df.empty
    assert BUYOUT_COLS.issubset(df.columns)
    assert (df["posting_number"].astype(str).str.strip() != "").all()


def test_parse_realization_report_file_xlsx():
    raw = pd.DataFrame([
        {"Номер отправления": "123-456-1", "Наименование": "Товар A",
         "Артикул продавца": "A1", "SKU": "111", "Штрихкод": "222",
         "Количество": 2, "Цена": 1000.0, "Комиссия": -200.0,
         "К перечислению": 1800.0},
        {"Номер отправления": "789-012-1", "Наименование": "Товар B",
         "Артикул продавца": "B2", "SKU": "333", "Штрихкод": "444",
         "Количество": 1, "Цена": 500.0, "Комиссия": -50.0,
         "К перечислению": 450.0},
    ])
    buf = io.BytesIO()
    raw.to_excel(buf, index=False)
    df = _prov()._parse_realization_report_file(buf.getvalue(), 8, 2026)
    assert not df.empty
    row = df.iloc[0]
    assert row["offer_id"] == "A1"
    assert row["posting_number"] == "123-456-1"
    assert row["quantity"] == 2
    assert row["income"] == 1800.0
    assert str(row["date"]) == "2026-08-28"


def test_parse_realization_report_file_unknown_headers_gives_empty():
    raw = pd.DataFrame([{"RandomColumn": 1, "Other": 2}])
    buf = io.BytesIO()
    raw.to_excel(buf, index=False)
    df = _prov()._parse_realization_report_file(buf.getvalue(), 8, 2026)
    assert df.empty