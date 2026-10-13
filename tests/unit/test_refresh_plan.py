from datetime import date

import pytest

from app.services import refresh as r
from app.services.window import parse_window


def test_plan_wb_has_six_kinds_in_order():
    steps = r._plan_steps("wb", include_detail=False)
    kinds = [s[0] for s in steps]
    assert kinds == ["cards", "stock", "funnel", "prices", "storage",
                     "promotions"]


def test_plan_wb_detail_appended_on_flag():
    kinds = [s[0] for s in r._plan_steps("wb", include_detail=True)]
    assert kinds == ["cards", "stock", "funnel", "prices", "storage",
                     "promotions", "detail", "storage_charges"]
    assert kinds[-1] == "storage_charges"
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


def test_plan_steps_filters_by_kinds():
    kinds = [k for k, _, _ in r._plan_steps("ozon", include_detail=True,
                                            kinds=["placement"])]
    assert kinds == ["placement"]


def test_plan_steps_kinds_can_include_detail_extras():
    kinds = [k for k, _, _ in r._plan_steps("wb", include_detail=True,
                                            kinds=["funnel", "detail"])]
    assert kinds == ["funnel", "detail"]
    kinds_oz = [k for k, _, _ in r._plan_steps("ozon", include_detail=True,
                                               kinds=["buyout", "accrual"])]
    assert kinds_oz == ["buyout", "accrual"]


def test_run_step_retries_on_rate_limit():
    from app.providers.errors import WbApiError

    calls = {"n": 0}
    slept = []

    def fn(db):
        calls["n"] += 1
        if calls["n"] == 1:
            raise WbApiError("лимит", status_code=429, retry_after=7)
        return {"rows": 1, "db_rows": 1, "window": "w"}

    step = {"kind": "prices", "note": ""}
    out = r._run_step(fn, None, step, sleep=lambda s: slept.append(s))
    assert out["rows"] == 1
    assert calls["n"] == 2
    assert slept == [7.0]
    assert "повторяем" in step["note"]


def test_run_step_gives_up_after_attempts():
    from app.providers.errors import OzonApiError

    calls = {"n": 0}

    def fn(db):
        calls["n"] += 1
        raise OzonApiError("лимит", status_code=429)

    step = {"kind": "placement", "note": ""}
    with pytest.raises(OzonApiError):
        r._run_step(fn, None, step, sleep=lambda s: None)
    assert calls["n"] == r.RATE_LIMIT_ATTEMPTS


def test_run_step_does_not_retry_other_errors():
    calls = {"n": 0}

    def fn(db):
        calls["n"] += 1
        raise ValueError("нет данных")

    with pytest.raises(ValueError):
        r._run_step(fn, None, {"kind": "x", "note": ""}, sleep=lambda s: None)
    assert calls["n"] == 1


def test_failed_steps_last_picks_latest_job(monkeypatch):
    jobs = {
        1: {"id": 1, "cab_id": 5, "api": "ozon", "cab_name": "К", "date_from": None,
            "date_to": None, "include_detail": True,
            "steps": [{"kind": "placement", "status": "failed"},
                      {"kind": "prices", "status": "ok"}]},
        2: {"id": 2, "cab_id": 5, "api": "ozon", "cab_name": "К", "date_from": None,
            "date_to": None, "include_detail": True,
            "steps": [{"kind": "placement", "status": "failed"},
                      {"kind": "prices", "status": "failed"}]},
        3: {"id": 3, "cab_id": 7, "api": "wb", "cab_name": "В", "date_from": None,
            "date_to": None, "include_detail": False,
            "steps": [{"kind": "cards", "status": "ok"}]},
    }
    monkeypatch.setattr(r, "_JOBS", jobs)
    out = r.failed_steps_last()
    assert len(out) == 1
    assert out[0]["cab_id"] == 5
    assert out[0]["kinds"] == ["placement", "prices"]



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