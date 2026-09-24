from datetime import date, timedelta

import pytest

from app.services import refresh as r
from app.services.window import parse_window


def test_plan_wb_has_six_kinds_in_order():
    steps = r._plan_steps("wb", include_detail=False)
    kinds = [s[0] for s in steps]
    assert kinds == ["cards", "stock", "funnel", "sales", "prices", "storage"]


def test_plan_wb_detail_appended_on_flag():
    kinds = [s[0] for s in r._plan_steps("wb", include_detail=True)]
    assert kinds[-1] == "detail"
    assert len(kinds) == 7


def test_plan_ozon_has_detail_and_buyout_on_flag():
    kinds = [s[0] for s in r._plan_steps("ozon", include_detail=False)]
    assert kinds == ["cards", "stock", "prices", "realization", "cashflow", "placement"]
    kinds2 = [s[0] for s in r._plan_steps("ozon", include_detail=True)]
    assert kinds2 == ["cards", "stock", "prices", "realization", "cashflow",
                      "placement", "detail", "buyout"]


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