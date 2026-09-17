"""Юнит-тесты автопилота цен WB: правила R1-R10, скорость/сезонность, применение."""
from datetime import date, timedelta

import pandas as pd
import pytest
from sqlalchemy import select

from app import models
from app.services import pricing as pricing_service
from app.services.pricing import merge_settings, project_velocity, recommendations

TODAY = date.today()


def _seed(db, art, nm, *, name="Товар", net_cost=300, replenishable=False,
          stock=100, sales=(), funnel=(0, 0, 0, 0, 0), funnel_extra=None):
    """Засевает товар: nm-карта, остаток, воронка, продажи.

    funnel=(views, adds, orders, cancelled, avg_price) для последнего среза воронки.
    funnel_extra — дополнительные поля среза воронки (рейтинг, выкупы и т.п.).
    sales=список кортежей (offset_days, quantity, returns_qty).
    """
    wb = db.execute(select(models.Marketplace.id).where(models.Marketplace.code == "wb")).scalar_one()
    db.add(models.Product(article=art, name=name, net_cost=net_cost,
                          replenishable=replenishable))
    db.add(models.NmArticle(nm_id=nm, article=art))
    if stock is not None:
        db.add(models.Stock(marketplace_id=wb, date=TODAY - timedelta(days=1),
                            article=art, warehouse="Все", quantity=stock))
    views, adds, orders, cancelled, avg_price = funnel
    db.add(models.FunnelMetric(
        date_from=TODAY - timedelta(days=7), date_to=TODAY - timedelta(days=1),
        nm_id=nm, article=art, views=views, adds=adds, orders=orders,
        cancelled=cancelled, avg_price=avg_price,
        **(funnel_extra or {}),
    ))
    for offset, qty, ret in sales:
        d = TODAY - timedelta(days=offset)
        db.add(models.Sale(
            marketplace_id=wb, date=d, article=art, source="v5",
            quantity=qty, returns_qty=ret,
            revenue=qty * 500.0, commission=qty * 75.0,
            logistics=qty * 40.0, storage=qty * 10.0,
            services=qty * 5.0, income=qty * 350.0,
        ))
    db.commit()
    return nm


def _prices(*pairs):
    """pairs: (nm, price, discount) -> DataFrame для провайдера."""
    return pd.DataFrame({
        "nmID": [p[0] for p in pairs],
        "price": [p[1] for p in pairs],
        "discount": [p[2] for p in pairs],
    })


def _row(rec, art):
    return next(r for r in rec["rows"] if r["article"] == art)


def _fallback_sales():
    """Продажа за пределами 14-дневного окна (внутри fallback 90 дн.) — даёт unit-экономику."""
    return [(40, 1, 0)]


# ------------------------------------------------------------------ project_velocity


def test_project_velocity_growth_is_damped():
    v = project_velocity(10.0, 2.0, season_adj=True, damp=0.5)
    assert 10.0 < v < 20.0  # g=5 -> clamp 2 -> *sqrt(2)


def test_project_velocity_decline_reduced():
    v = project_velocity(2.0, 10.0, season_adj=True, damp=0.5)
    assert 0.0 < v < 2.0


def test_project_velocity_flat_unchanged():
    assert project_velocity(5.0, 5.0) == 5.0


def test_project_velocity_prev_zero_returns_now():
    assert project_velocity(3.0, 0.0) == 3.0


def test_project_velocity_season_off_returns_now():
    assert project_velocity(10.0, 2.0, season_adj=False) == 10.0


# ------------------------------------------------------------------ merge_settings


def test_merge_settings_overrides_known_keys():
    s = merge_settings({"window_days": 7, "season_adj": "true", "i_dont_exist": 42})
    assert s["window_days"] == 7
    assert s["season_adj"] is True
    assert "i_dont_exist" not in s
    assert s["target_doc"] == 30


def test_merge_settings_ignores_garbage_value():
    assert merge_settings({"window_days": "abc"})["window_days"] == 14


# ------------------------------------------------------------------ правила R1-R10


def test_r1_no_stock_skips(db):
    _seed(db, "R1", "P1", stock=None, sales=[(40, 0, 0)])
    rec = recommendations(db, prices_df=_prices(("P1", 1000, 0)), today=TODAY)
    assert _row(rec, "R1")["status"] == "skipped_no_stock"


def test_r2_no_unit_economics_skips(db):
    # история есть (запись в продажах), но продаж/реального дохода нет -> нет unit-экономики
    _seed(db, "R2", "P2", stock=10, sales=[(40, 0, 0)])
    rec = recommendations(db, prices_df=_prices(("P2", 1000, 0)), today=TODAY)
    assert _row(rec, "R2")["status"] == "skipped_no_ratio"


def test_r3_high_returns_skips(db):
    _seed(db, "R3", "P3", stock=50,
          sales=[(3, 6, 2), *_fallback_sales()], funnel=(300, 5, 3, 2, 0))
    rec = recommendations(db, prices_df=_prices(("P3", 1000, 0)), today=TODAY)
    assert _row(rec, "R3")["status"] == "skipped_returns"


def test_r4_dead_stock_lowers(db):
    _seed(db, "R4", "P4", stock=100, sales=[*_fallback_sales()])
    rec = recommendations(db, prices_df=_prices(("P4", 1000, 0)), today=TODAY)
    row = _row(rec, "R4")
    assert row["action"] == "LOWER"
    assert row["target_discount"] == pytest.approx(15.0, abs=0.5)  # 1000 -> 850
    assert row["target_vis"] == pytest.approx(850.0, abs=1)


def test_r5_replenishable_hot_raises_limited(db):
    _seed(db, "R5", "P5", stock=5, replenishable=True,
          sales=[(2, 4, 0), (20, 1, 0), *_fallback_sales()], funnel=(200, 10, 8, 0, 900))
    rec = recommendations(db, prices_df=_prices(("P5", 1000, 20)), today=TODAY)
    row = _row(rec, "R5")
    assert row["action"] == "RAISE"
    assert row["target_discount"] == pytest.approx(12.0, abs=0.5)  # 800 -> 880 (+10%)


def test_r6_non_replenishable_deficit_raises_more(db):
    _seed(db, "R6", "P6", stock=4, replenishable=False,
          sales=[(2, 6, 0), (20, 1, 0), *_fallback_sales()], funnel=(100, 1, 1, 0, 0))
    rec = recommendations(db, prices_df=_prices(("P6", 1000, 20)), today=TODAY)
    row = _row(rec, "R6")
    assert row["action"] == "RAISE"
    assert row["target_discount"] == pytest.approx(8.0, abs=0.5)  # 800 -> 920 (+15%)


def test_r7_replenishable_deficit_no_demand_holds(db):
    _seed(db, "R7", "P7", stock=5, replenishable=True,
          sales=[(2, 4, 0), *_fallback_sales()], funnel=(200, 1, 1, 0, 0))
    rec = recommendations(db, prices_df=_prices(("P7", 1000, 20)), today=TODAY)
    assert _row(rec, "R7")["action"] == "HOLD"


def test_r8_overstock_lowers(db):
    _seed(db, "R8", "P8", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), *_fallback_sales()], funnel=(200, 3, 2, 0, 0))
    rec = recommendations(db, prices_df=_prices(("P8", 2000, 10)), today=TODAY)
    row = _row(rec, "R8")
    assert row["action"] == "LOWER"
    assert row["target_discount"] == pytest.approx((1 - 1530 / 2000) * 100, abs=0.5)


def test_r9_many_carts_low_conv_skips(db):
    _seed(db, "R9", "P9", stock=50,
          sales=[(2, 2, 0), *_fallback_sales()], funnel=(400, 10, 1, 0, 0))
    rec = recommendations(db, prices_df=_prices(("P9", 1000, 10)), today=TODAY)
    assert _row(rec, "R9")["status"] == "skipped_carts"


def test_r10_normal_stock_holds(db):
    _seed(db, "H", "P10", stock=20,
          sales=[(2, 5, 0), (14, 5, 0), *_fallback_sales()], funnel=(200, 4, 4, 0, 0))
    rec = recommendations(db, prices_df=_prices(("P10", 1000, 10)), today=TODAY)
    row = _row(rec, "H")
    assert row["action"] == "HOLD"
    assert 14 <= row["doc"] < 60


def test_min_delta_blocks_change_when_cannot_lower_below_floor(db):
    # полка дороже цены: floor > price, снижать скидку некуда -> HOLD
    _seed(db, "M", "P11", stock=100, net_cost=800, sales=[*_fallback_sales()])
    rec = recommendations(db, prices_df=_prices(("P11", 1000, 0)), today=TODAY)
    assert _row(rec, "M")["action"] == "HOLD"


def test_eff_uses_min_of_vis_and_avg_price(db):
    _seed(db, "A", "P12", stock=6, replenishable=True,
          sales=[(2, 5, 0), *_fallback_sales()], funnel=(200, 3, 4, 0, 920))
    rec = recommendations(db, prices_df=_prices(("P12", 1000, 12)), today=TODAY)
    row = _row(rec, "A")
    assert row["eff"] == pytest.approx(880.0, abs=1)
    assert row["avg_price"] == pytest.approx(920.0, abs=1)


def test_no_funnel_data_zero_conv_and_velocity(db):
    _seed(db, "Z", "P13", stock=20, sales=[(2, 3, 0), *_fallback_sales()])
    rec = recommendations(db, prices_df=_prices(("P13", 1000, 10)), today=TODAY)
    assert _row(rec, "Z")["conv_pct"] == 0.0


def test_insufficient_history_returns_note(db):
    _seed(db, "X", "P1", stock=1, sales=[(0, 1, 0)])
    rec = recommendations(db, prices_df=_prices(("P1", 1000, 0)), today=TODAY)
    assert rec["rows"] == []
    assert "Мало истории" in rec["note"]


# ------------------------------------------------------------------ T-13: окно из шапки + обогащение данных


def test_merge_settings_passes_valid_dates():
    s = merge_settings({"date_from": "2026-08-01T10:00", "date_to": "не дата"})
    assert s["date_from"] == "2026-08-01"
    assert "date_to" not in s


def test_recommendations_honors_date_window(db):
    _seed(db, "E2", "P2", stock=0, sales=[(2, 2, 0), *_fallback_sales()])
    rec = recommendations(
        db, prices_df=_prices(("P2", 1000, 0)), today=TODAY,
        settings={"date_from": "2026-08-01", "date_to": "2026-08-14"},
    )
    assert rec["date_from"] == "2026-08-01"
    assert rec["date_to"] == "2026-08-14"
    assert rec["window_days"] == 14
    assert rec["settings"]["date_from"] == "2026-08-01"


def test_enriched_columns_from_funnel_and_detail(db):
    """T-13: рейтинг/выкупы из воронки и фактические деньги из детализации попадают в строку."""
    _seed(db, "E1", "P1", stock=None, sales=[(2, 4, 0), *_fallback_sales()],
          funnel=(100, 1, 1, 0, 500),
          funnel_extra={"product_rating": 4.7, "buyouts": 5,
                        "conv_buyout_percent": 80.0, "stock_wb": 900})
    db.add_all([
        models.WbDetailRow(op_key="sr:sale-e1", source="excel", article="E1",
                           doc_type_name="Продажа", sale_dt=TODAY - timedelta(days=2),
                           quantity=4, retail_amount=4000.0, for_pay=3200.0),
        models.WbDetailRow(op_key="sr:ret-e1", source="excel", article="E1",
                           doc_type_name="Возврат", sale_dt=TODAY - timedelta(days=2),
                           quantity=1, retail_amount=1000.0, for_pay=800.0),
    ])
    db.commit()
    rec = recommendations(db, prices_df=_prices(("P1", 1000, 10)), today=TODAY)
    row = _row(rec, "E1")
    assert row["product_rating"] == pytest.approx(4.7)
    assert row["buyouts"] == 5
    assert row["conv_buyout_percent"] == pytest.approx(80.0)
    assert row["stock_wb"] == 900
    # Детализация: 4 продажи − 1 возврат (нетто 3), доход 3200-800, без расходов,
    # себестоимость 300. return_rate = возвраты/(нетто-продажи+возвраты).
    assert row["return_rate"] == pytest.approx(25.0, abs=0.1)
    assert row["margin_pct"] == pytest.approx(50.0, abs=1.0)
    assert row["margin_per_one"] == pytest.approx(500.0, abs=5.0)
    assert row["revenue_per_one"] == pytest.approx(1000.0, abs=5.0)
    assert row["detail_sells"] == 3
    assert row["detail_returns_qty"] == 1


def test_row_skip_has_enriched_defaults(db):
    """SKIP-строка (нет карточки/цен) получает default-значения новых колонок."""
    _seed(db, "E3", "P3", stock=5, sales=[(2, 1, 0), *_fallback_sales()])
    rec = recommendations(db, prices_df=_prices(("NOPE", 1000, 0)), today=TODAY)
    row = _row(rec, "E3")
    assert row["action"] == "SKIP"
    assert row["return_rate"] == 0.0
    assert row["product_rating"] is None


# ------------------------------------------------------------------ применение


def test_apply_records_applied_and_returns_items(db):
    _seed(db, "R8", "P8", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), *_fallback_sales()], funnel=(200, 3, 2, 0, 0))
    fake = _FakeProvider()
    res = pricing_service.apply_recommendations(
        db, prices_df=_prices(("P8", 2000, 10)), provider=fake, today=TODAY,
    )
    assert res["applied"] == ["R8"]
    assert res["pushed"] == 1
    assert fake.applied_prices == [{
        "nmID": "P8", "price": 2000.0, "discount": pytest.approx((1 - 1530 / 2000) * 100, abs=0.5),
    }]
    log = db.execute(
        select(models.PriceChange).where(models.PriceChange.article == "R8")
    ).scalars().all()
    assert log and log[-1].status == "applied"
    assert log[-1].after_discount == pytest.approx((1 - 1530 / 2000) * 100, abs=0.5)
    assert log[-1].applied_at is not None


def test_apply_error_records_error_and_raises(db):
    _seed(db, "R8", "P8", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), *_fallback_sales()], funnel=(200, 3, 2, 0, 0))
    fake = _FakeProvider(raise_on_update=True)
    with pytest.raises(RuntimeError):
        pricing_service.apply_recommendations(
            db, prices_df=_prices(("P8", 2000, 10)), provider=fake, today=TODAY,
        )
    logs = db.execute(select(models.PriceChange)).scalars().all()
    assert any(log.status == "error" and "boom" in log.reason for log in logs)


def test_apply_cooldown_blocks_reapply(db):
    _seed(db, "R8", "P8", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), *_fallback_sales()], funnel=(200, 3, 2, 0, 0))
    pricing_service.apply_recommendations(
        db, prices_df=_prices(("P8", 2000, 10)), provider=_FakeProvider(), today=TODAY,
    )
    res = pricing_service.apply_recommendations(
        db, prices_df=_prices(("P8", 2000, 10)), provider=_FakeProvider(), today=TODAY,
    )
    assert res["pushed"] == 0
    assert all(r["status"] == "skipped_cooldown"
               for r in res["rows"] if r["action"] in ("RAISE", "LOWER"))


class _FakeProvider:
    def __init__(self, raise_on_update=False):
        self.raise_on_update = raise_on_update
        self.applied_prices = None

    def update_prices(self, items):
        if self.raise_on_update:
            raise RuntimeError("boom: WB недоступен")
        self.applied_prices = items
        return {"task_id": "task-1"}