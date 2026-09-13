import pandas as pd
import pytest

from app.services import refresh as r


@pytest.fixture(autouse=True)
def _no_nm_map(monkeypatch):
    monkeypatch.setattr(r, "_wb_nmid_to_article", lambda: {"1001": "ART-1"})


def _real_df():
    return pd.DataFrame([
        {
            "product.nmId": "1001",
            "product.vendorCode": "SK012",
            "product.title": "Шуба",
            "statistic.selected.period.start": "2026-08-13",
            "statistic.selected.period.end": "2026-09-12",
            "statistic.selected.openCount": 120,
            "statistic.selected.cartCount": 15,
            "statistic.selected.orderCount": 4,
            "statistic.selected.buyoutCount": 3,
            "statistic.selected.orderSum": 12000,
            "statistic.selected.buyoutSum": 9000.0,
            "statistic.selected.cancelCount": 1,
            "statistic.selected.avgPrice": 3000,
        },
        {
            "product.nmId": "1002",
            "product.vendorCode": "SK015",
            "product.title": "Туфли",
            "statistic.selected.period.start": "2026-08-13",
            "statistic.selected.period.end": "2026-09-12",
            "statistic.selected.openCount": 90,
            "statistic.selected.cartCount": 8,
            "statistic.selected.orderCount": 2,
            "statistic.selected.buyoutCount": 2,
            "statistic.selected.orderSum": 4000,
            "statistic.selected.buyoutSum": 4000.0,
            "statistic.selected.cancelCount": 0,
            "statistic.selected.avgPrice": 2000,
        },
    ])


def test_maps_real_namespaced_columns(monkeypatch):
    monkeypatch.setattr(r, "_wb_nmid_to_article", lambda: {})
    out = r._funnel_to_db(_real_df(), "2026-08-13", "2026-09-12")
    assert out["article"].tolist() == ["SK012", "SK015"]
    assert out["nm_id"].tolist() == ["1001", "1002"]
    assert out["views"].tolist() == [120, 90]
    assert out["adds"].tolist() == [15, 8]
    assert out["orders"].tolist() == [4, 2]
    assert out["cancelled"].tolist() == [1, 0]
    assert out["buyouts"].tolist() == [3, 2]
    assert out["avg_price"].tolist() == [3000.0, 2000.0]
    assert out["revenue"].tolist() == [12000.0, 4000.0]
    assert out["buyout_sum"].tolist() == [9000.0, 4000.0]


def test_vendor_code_preferred_over_nm_map():
    # nm-карта вернула бы ART-1 для 1001, но vendorCode из ответа важнее
    out = r._funnel_to_db(_real_df(), "2026-08-13", "2026-09-12")
    assert out["article"].tolist() == ["SK012", "SK015"]


def test_fallback_to_nm_map_when_no_vendor():
    df = pd.DataFrame([
        {"nmID": "1001", "viewsCount": 5, "orderCount": 1},
    ])
    out = r._funnel_to_db(df, "2026-08-13", "2026-09-12")
    assert out["article"].tolist() == ["ART-1"]
    assert out["nm_id"].tolist() == ["1001"]
    assert out["views"].tolist() == [5]
    assert out["orders"].tolist() == [1]


def test_no_identifiers_yields_empty_articles():
    df = pd.DataFrame([{"viewsCount": 7, "orderCount": 0}])
    out = r._funnel_to_db(df, "2026-08-13", "2026-09-12")
    assert out["article"].tolist() == [""]
    assert out["nm_id"].tolist() == [""]


def test_empty_df_passthrough():
    assert r._funnel_to_db(pd.DataFrame(), "2026-08-13", "2026-09-12").empty


def test_missing_buyout_columns_defaults_to_zero(monkeypatch):
    monkeypatch.setattr(r, "_wb_nmid_to_article", lambda: {})
    df = pd.DataFrame([
        {"product.nmId": "2001", "product.vendorCode": "X-1",
         "statistic.selected.openCount": 5, "statistic.selected.cartCount": 1,
         "statistic.selected.orderCount": 1},
    ])
    out = r._funnel_to_db(df, "2026-01-01", "2026-01-31")
    assert out["buyouts"].tolist() == [0]
    assert out["buyout_sum"].tolist() == [0.0]