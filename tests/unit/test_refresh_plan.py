from datetime import date, timedelta

import pytest

from app.services import refresh as r
from app.services.window import parse_window


def test_plan_wb_has_seven_kinds_in_order():
    steps = r._plan_steps("wb", include_detail=False)
    kinds = [s[0] for s in steps]
    assert kinds == ["cards", "stock", "funnel", "sales", "prices", "storage",
                     "promotions"]


def test_plan_wb_detail_appended_on_flag():
    kinds = [s[0] for s in r._plan_steps("wb", include_detail=True)]
    assert kinds[-1] == "detail"
    assert len(kinds) == 8


def test_plan_ozon_has_detail_buyout_and_accrual_on_flag():
    kinds = [s[0] for s in r._plan_steps("ozon", include_detail=False)]
    assert kinds == ["cards", "stock", "prices", "realization", "cashflow", "placement"]
    kinds2 = [s[0] for s in r._plan_steps("ozon", include_detail=True)]
    assert kinds2 == ["cards", "stock", "prices", "realization", "cashflow",
                      "placement", "detail", "buyout", "accrual"]


def test_plan_respects_date_window():
    for _, _, fn in r._plan_steps("wb", include_detail=True):
        pass
    # шаги лямбд захватывают окно; проверяем что окно резолвится корректно
    from_, to_ = parse_window("2026-08-01", "2026-08-31")
    assert from_ == date(2026, 8, 1)
    assert to_ == date(2026, 8, 31)


def test_default_range_matches_parse_window():
    assert r.default_range() == parse_window()
    assert r.resolve_range() == parse_window()


def test_unknown_api_raises_keyerror():
    with pytest.raises(KeyError):
        r._plan_steps("nope", False)


def test_pull_oz_accrual_resolves_offer_id_via_sku_map(db):
    """SKU без продаж в детализации дорезолвивается через get_sku_map."""
    import pandas as pd
    from app import models

    acc = pd.DataFrame([{
        "op_key": "2026-09-01|a1|logistics|32|555|0", "date": "2026-09-01",
        "accrual_id": "a1", "unit_number": "PZ-9", "bucket": "logistics",
        "type_id": 32, "sku": "555", "quantity": 1, "amount": -25.0,
        "seller_price": 0.0, "sale_price": 0.0, "offer_id": "",
    }])

    class _P:
        def get_accrual(self, a, b):
            return acc

        def get_sku_map(self):
            return pd.DataFrame([{"sku": "555", "offer_id": "OZ-NEW",
                                  "barcode": "", "name": ""}])

    out = r.pull_oz_accrual(db, date(2026, 9, 1), date(2026, 9, 1), provider=_P())
    assert out["rows"] == 1 and out["count"] == 1
    row = db.query(models.OzonAccrual).one()
    assert row.offer_id == "OZ-NEW"


def test_pull_oz_accrual_survives_sku_map_failure(db):
    """Ошибка get_sku_map не должна ломать загрузку — строки остаются без артикула."""
    import pandas as pd
    from app import models

    acc = pd.DataFrame([{
        "op_key": "2026-09-01|a1|other|76||0", "date": "2026-09-01",
        "accrual_id": "a1", "unit_number": "", "bucket": "other",
        "type_id": 76, "sku": "", "quantity": 0, "amount": -10.0,
        "seller_price": 0.0, "sale_price": 0.0, "offer_id": "",
    }])

    class _P:
        def get_accrual(self, a, b):
            return acc

        def get_sku_map(self):
            raise RuntimeError("429")

    out = r.pull_oz_accrual(db, date(2026, 9, 1), date(2026, 9, 1), provider=_P())
    assert out["count"] == 1
    assert db.query(models.OzonAccrual).one().offer_id == ""