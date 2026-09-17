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
            "product.brandName": "Бренд A",
            "product.subjectName": "Куртки",
            "product.productRating": 8.2,
            "product.feedbackRating": 4.6,
            "product.stocks.wb": 12,
            "product.stocks.mp": 3,
            "product.stocks.balanceSum": 36000.0,
            "statistic.selected.period.start": "2026-08-13",
            "statistic.selected.period.end": "2026-09-12",
            "statistic.selected.openCount": 120,
            "statistic.selected.cartCount": 15,
            "statistic.selected.orderCount": 4,
            "statistic.selected.buyoutCount": 3,
            "statistic.selected.orderSum": 12000,
            "statistic.selected.buyoutSum": 9000.0,
            "statistic.selected.cancelCount": 1,
            "statistic.selected.cancelSum": 1000.0,
            "statistic.selected.avgPrice": 3000,
            "statistic.selected.avgOrdersCountPerDay": 0.42,
            "statistic.selected.shareOrderPercent": 12.5,
            "statistic.selected.addToWishlist": 8,
            "statistic.selected.timeToReady.days": 1,
            "statistic.selected.timeToReady.hours": 4,
            "statistic.selected.timeToReady.mins": 30,
            "statistic.selected.localizationPercent": 100,
            "statistic.selected.conversions.addToCartPercent": 12.5,
            "statistic.selected.conversions.cartToOrderPercent": 26.7,
            "statistic.selected.conversions.buyoutPercent": 75.0,
            "statistic.selected.wbClub.orderCount": 1,
            "statistic.selected.wbClub.orderSum": 3000.0,
            "statistic.selected.wbClub.buyoutCount": 1,
            "statistic.selected.wbClub.buyoutSum": 3000.0,
            "statistic.selected.wbClub.cancelCount": 0,
            "statistic.selected.wbClub.cancelSum": 0,
            "statistic.selected.wbClub.avgPrice": 3000.0,
            "statistic.selected.wbClub.buyoutPercent": 100.0,
            "statistic.selected.wbClub.avgOrderCountPerDay": 0.1,
        },
        {
            "product.nmId": "1002",
            "product.vendorCode": "SK015",
            "product.title": "Туфли",
            "product.brandName": "Бренд B",
            "product.subjectName": "Обувь",
            "product.productRating": 7.0,
            "product.feedbackRating": 4.8,
            "product.stocks.wb": 5,
            "product.stocks.mp": 0,
            "product.stocks.balanceSum": 8000.0,
            "statistic.selected.period.start": "2026-08-13",
            "statistic.selected.period.end": "2026-09-12",
            "statistic.selected.openCount": 90,
            "statistic.selected.cartCount": 8,
            "statistic.selected.orderCount": 2,
            "statistic.selected.buyoutCount": 2,
            "statistic.selected.orderSum": 4000,
            "statistic.selected.buyoutSum": 4000.0,
            "statistic.selected.cancelCount": 0,
            "statistic.selected.cancelSum": 0,
            "statistic.selected.avgPrice": 2000,
            "statistic.selected.avgOrdersCountPerDay": 0.21,
            "statistic.selected.shareOrderPercent": 5.0,
            "statistic.selected.addToWishlist": 3,
            "statistic.selected.timeToReady.days": 0,
            "statistic.selected.timeToReady.hours": 2,
            "statistic.selected.timeToReady.mins": 15,
            "statistic.selected.localizationPercent": 100,
            "statistic.selected.conversions.addToCartPercent": 8.9,
            "statistic.selected.conversions.cartToOrderPercent": 25.0,
            "statistic.selected.conversions.buyoutPercent": 100.0,
            "statistic.selected.wbClub.orderCount": 0,
            "statistic.selected.wbClub.orderSum": 0,
            "statistic.selected.wbClub.buyoutCount": 0,
            "statistic.selected.wbClub.buyoutSum": 0,
            "statistic.selected.wbClub.cancelCount": 0,
            "statistic.selected.wbClub.cancelSum": 0,
            "statistic.selected.wbClub.avgPrice": 0,
            "statistic.selected.wbClub.buyoutPercent": 0,
            "statistic.selected.wbClub.avgOrderCountPerDay": 0,
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


def test_maps_extended_api_fields(monkeypatch):
    monkeypatch.setattr(r, "_wb_nmid_to_article", lambda: {})
    out = r._funnel_to_db(_real_df(), "2026-08-13", "2026-09-12")
    assert out["subject_name"].tolist() == ["Куртки", "Обувь"]
    assert out["brand_name"].tolist() == ["Бренд A", "Бренд B"]
    assert out["product_rating"].tolist() == [8.2, 7.0]
    assert out["feedback_rating"].tolist() == [4.6, 4.8]
    assert out["stock_wb"].tolist() == [12, 5]
    assert out["stock_mp"].tolist() == [3, 0]
    assert out["stock_balance_sum"].tolist() == [36000.0, 8000.0]
    assert out["cancel_sum"].tolist() == [1000.0, 0]
    assert out["avg_orders_per_day"].tolist() == [0.42, 0.21]
    assert out["share_order_percent"].tolist() == [12.5, 5.0]
    assert out["add_to_wishlist"].tolist() == [8, 3]
    assert out["time_to_ready_min"].tolist() == [1710, 135]
    assert out["localization_percent"].tolist() == [100, 100]
    assert out["conv_to_cart_percent"].tolist() == [12.5, 8.9]
    assert out["conv_cart_to_order_percent"].tolist() == [26.7, 25.0]
    assert out["conv_buyout_percent"].tolist() == [75.0, 100.0]
    assert out["wb_club_order_count"].tolist() == [1, 0]
    assert out["wb_club_order_sum"].tolist() == [3000.0, 0]
    assert out["wb_club_buyout_count"].tolist() == [1, 0]
    assert out["wb_club_buyout_sum"].tolist() == [3000.0, 0]
    assert out["wb_club_avg_price"].tolist() == [3000.0, 0]
    assert out["wb_club_buyout_percent"].tolist() == [100.0, 0]


def test_raw_json_preserves_original_object():
    out = r._funnel_to_db(_real_df(), "2026-08-13", "2026-09-12")
    raw = out["raw_json"].tolist()
    assert "subjectName" in raw[0]
    assert "statistic" in raw[0]
    assert isinstance(raw[0], str) and raw[0].startswith("{")


def test_legacy_df_without_extras_defaults_to_empty(monkeypatch):
    monkeypatch.setattr(r, "_wb_nmid_to_article", lambda: {})
    df = pd.DataFrame([
        {"product.nmId": "2001", "product.vendorCode": "X-1",
         "statistic.selected.openCount": 5, "statistic.selected.cartCount": 1,
         "statistic.selected.orderCount": 1},
    ])
    out = r._funnel_to_db(df, "2026-01-01", "2026-01-31")
    for c in ["stock_wb", "stock_mp", "cancel_sum", "add_to_wishlist",
              "time_to_ready_min", "wb_club_order_count", "wb_club_order_sum",
              "conv_to_cart_percent", "stock_balance_sum"]:
        assert out.get(c, None) is not None
    assert out["subject_name"].tolist() == [""]
    assert out["brand_name"].tolist() == [""]
    assert out["time_to_ready_min"].tolist() == [0]


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