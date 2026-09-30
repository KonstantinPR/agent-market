"""Юнит-тесты автопилота цен WB: правила R1-R10, скорость/сезонность, применение."""
import json
from datetime import date, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import select

from app import models
from app.services import pricing as pricing_service
from app.services.pricing import (
    _active_promotions,
    _parse_ranging,
    _promo_cap_pct,
    _promo_pass,
    merge_settings,
    project_velocity,
    recommendations,
)

TODAY = date.today()


def _seed(db, art, nm, *, name="Товар", net_cost=300, replenishable=False,
          stock=100, in_way=0, sales=(), funnel=(0, 0, 0, 0, 0), funnel_extra=None):
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
                            article=art, warehouse="Все", quantity=stock, in_way=in_way))
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
    # «живой» товар (недавние продажи) без данных об остатках -> SKIP
    _seed(db, "R1", "P1", stock=None, sales=[(3, 1, 0), *_fallback_sales()])
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
    rec = recommendations(db, prices_df=_prices(("P3", 1000, 0)), today=TODAY,
                          settings={"use_returns": True})
    assert _row(rec, "R3")["status"] == "skipped_returns"


def test_r4_dead_stock_lowers(db):
    _seed(db, "R4", "P4", stock=100, sales=[*_fallback_sales()])
    rec = recommendations(db, prices_df=_prices(("P4", 1000, 0)), today=TODAY,
                          settings={"prefer_raise": False})
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
    assert row["target_discount"] == pytest.approx(10.0, abs=0.5)  # 800 -> 920 (+15%) -> кап R2: 20/2 = 10


def test_r7_no_demand_in_normal_doc_zone_forces_raise(db):
    """DOC≈17.5 (нормальная зона), остаток есть — коррекция скидки обязательна: RAISE на шаг."""
    _seed(db, "R7", "P7", stock=5, replenishable=True,
          sales=[(2, 4, 0), *_fallback_sales()], funnel=(200, 1, 1, 0, 0))
    rec = recommendations(db, prices_df=_prices(("P7", 1000, 20)), today=TODAY)
    row = _row(rec, "R7")
    assert row["action"] == "RAISE"
    assert row["target_discount"] == pytest.approx(19.0, abs=0.5)  # 20 -> 19 (шаг 1 п.п.)
    assert "коррекция скидки обязательна" in row["reason"]


def test_rule2_half_discount_cap_binds_raise(db):
    """Половинный кап R2: даже при мощном RAISE скидку режем не более чем пополам (20 -> 10)."""
    _seed(db, "C2", "PC2", stock=4, replenishable=False,
          sales=[(2, 6, 0), (20, 1, 0), *_fallback_sales()], funnel=(100, 1, 1, 0, 900))
    rec = recommendations(db, prices_df=_prices(("PC2", 1000, 20)), today=TODAY)
    row = _row(rec, "C2")
    assert row["action"] == "RAISE"
    assert row["target_discount"] == pytest.approx(10.0, abs=0.5)  # raw 8.0 -> кап cur/2 = 10.0


def test_rule2_exception_below_cost_allows_full_raise(db):
    """Исключение R2: фактическая цена продажи ниже себестоимости → полный RAISE без капа (20 -> 8)."""
    _seed(db, "C3", "PC3", stock=4, replenishable=False,
          sales=[(2, 6, 0), (20, 1, 0), *_fallback_sales()], funnel=(100, 1, 1, 0, 250))
    rec = recommendations(db, prices_df=_prices(("PC3", 1000, 20)), today=TODAY)
    row = _row(rec, "C3")
    assert row["action"] == "RAISE"
    assert row["target_discount"] == pytest.approx(8.0, abs=0.5)  # 250 < net_cost 300 -> без капа


def test_r8_overstock_lowers(db):
    _seed(db, "R8", "P8", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), *_fallback_sales()], funnel=(200, 3, 2, 0, 0))
    rec = recommendations(db, prices_df=_prices(("P8", 2000, 10)), today=TODAY,
                          settings={"prefer_raise": False})
    row = _row(rec, "R8")
    assert row["action"] == "LOWER"
    assert row["target_discount"] == pytest.approx((1 - 1530 / 2000) * 100, abs=0.5)


def test_r9_many_carts_low_conv_skips(db):
    _seed(db, "R9", "P9", stock=50,
          sales=[(2, 2, 0), *_fallback_sales()], funnel=(400, 10, 1, 0, 0))
    rec = recommendations(db, prices_df=_prices(("P9", 1000, 10)), today=TODAY)
    assert _row(rec, "R9")["status"] == "skipped_carts"


def test_r10_normal_stock_forces_step(db):
    """Нормальные остатки (DOC между зонами): остаток есть → скидка меняется на минимальный шаг."""
    _seed(db, "H", "P10", stock=20,
          sales=[(2, 5, 0), (14, 5, 0), *_fallback_sales()], funnel=(200, 4, 4, 0, 0))
    rec = recommendations(db, prices_df=_prices(("P10", 1000, 10)), today=TODAY)
    row = _row(rec, "H")
    assert 14 <= row["doc"] < 60
    assert row["action"] == "LOWER"  # DOC=56 ≥ целевые 30 → идём вниз на шаг
    assert row["target_discount"] == pytest.approx(11.0, abs=0.5)
    assert "коррекция скидки обязательна" in row["reason"]


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


# ------------------------------------------------ T-14: блокировки качества и uplift


def test_t14_low_rating_blocks_raise(db):
    _seed(db, "Q1", "PQ1", stock=5, replenishable=True,
          sales=[(2, 12, 0), (20, 1, 0), *_fallback_sales()], funnel=(200, 10, 8, 0, 900),
          funnel_extra={"product_rating": 3.5})
    rec = recommendations(db, prices_df=_prices(("PQ1", 1000, 20)), today=TODAY,
                          settings={"use_quality": True})
    row = _row(rec, "Q1")
    assert row["action"] == "SKIP"
    assert row["status"] == "skipped_quality"
    assert "рейтинг" in row["reason"]


def test_t14_low_buyout_conv_blocks_raise(db):
    _seed(db, "Q2", "PQ2", stock=5, replenishable=True,
          sales=[(2, 12, 0), (20, 1, 0), *_fallback_sales()], funnel=(200, 10, 8, 0, 900),
          funnel_extra={"product_rating": 4.8, "conv_buyout_percent": 20.0})
    rec = recommendations(db, prices_df=_prices(("PQ2", 1000, 20)), today=TODAY,
                          settings={"use_quality": True})
    row = _row(rec, "Q2")
    assert row["status"] == "skipped_quality"
    assert "выкупа" in row["reason"]


def test_t14_high_cancel_ratio_blocks_raise(db):
    _seed(db, "Q3", "PQ3", stock=5, replenishable=True,
          sales=[(2, 12, 0), (20, 1, 0), *_fallback_sales()], funnel=(200, 10, 8, 3, 900),
          funnel_extra={"product_rating": 4.8})
    rec = recommendations(db, prices_df=_prices(("PQ3", 1000, 20)), today=TODAY,
                          settings={"use_quality": True})
    row = _row(rec, "Q3")
    assert row["status"] == "skipped_quality"
    assert "отмены" in row["reason"]


def test_t14_detail_return_rate_blocks_raise(db):
    _seed(db, "Q4", "PQ4", stock=5, replenishable=True,
          sales=[(2, 12, 0), (20, 1, 0), *_fallback_sales()], funnel=(200, 10, 8, 0, 900),
          funnel_extra={"product_rating": 4.8, "conv_buyout_percent": 50.0})
    db.add_all([
        models.WbDetailRow(op_key="sr:sale-q4", source="excel", article="Q4",
                           doc_type_name="Продажа", sale_dt=TODAY - timedelta(days=2),
                           quantity=10, retail_amount=5000.0, for_pay=4000.0),
        models.WbDetailRow(op_key="sr:ret-q4", source="excel", article="Q4",
                           doc_type_name="Возврат", sale_dt=TODAY - timedelta(days=2),
                           quantity=4, retail_amount=2000.0, for_pay=1600.0),
    ])
    db.commit()
    rec = recommendations(db, prices_df=_prices(("PQ4", 1000, 20)), today=TODAY,
                          settings={"use_quality": True})
    row = _row(rec, "Q4")
    assert row["status"] == "skipped_quality"
    assert "возвраты" in row["reason"]


def test_t14_strong_signals_apply_uplift(db):
    _seed(db, "B", "PB", stock=5, replenishable=True,
          sales=[(2, 12, 0), (20, 1, 0), *_fallback_sales()], funnel=(200, 10, 8, 0, 900),
          funnel_extra={"product_rating": 4.9, "conv_buyout_percent": 85.0})
    rec = recommendations(db, prices_df=_prices(("PB", 1000, 20)), today=TODAY,
                          settings={"use_quality": True})
    row = _row(rec, "B")
    assert row["action"] == "RAISE"
    # +12.5% (10% × uplift 1.25): 800 → 900, скидка 20 → 10
    assert row["target_discount"] == pytest.approx(10.0, abs=0.5)
    assert "сильные сигналы" in row["reason"]


def test_t14_no_uplift_without_strong_signals(db):
    _seed(db, "B2", "PB1", stock=5, replenishable=True,
          sales=[(2, 4, 0), (20, 1, 0), *_fallback_sales()], funnel=(200, 10, 8, 0, 900))
    rec = recommendations(db, prices_df=_prices(("PB1", 1000, 20)), today=TODAY,
                          settings={"use_quality": True})
    row = _row(rec, "B2")
    assert row["action"] == "RAISE"
    assert row["target_discount"] == pytest.approx(12.0, abs=0.5)


def test_t14_low_rating_does_not_block_lower(db):
    _seed(db, "Q5", "PQ5", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), *_fallback_sales()], funnel=(200, 3, 2, 0, 0),
          funnel_extra={"product_rating": 2.5})
    rec = recommendations(db, prices_df=_prices(("PQ5", 2000, 10)), today=TODAY,
                          settings={"use_quality": True})
    assert _row(rec, "Q5")["action"] == "LOWER"


def test_t14_unknown_quality_does_not_block_raise(db):
    """Нет данных о качестве (рейтинг/выкупы = 0) — RAISE не блокируется."""
    _seed(db, "Q6", "PQ6", stock=5, replenishable=True,
          sales=[(2, 12, 0), (20, 1, 0), *_fallback_sales()], funnel=(200, 10, 8, 0, 900))
    rec = recommendations(db, prices_df=_prices(("PQ6", 1000, 20)), today=TODAY,
                          settings={"use_quality": True})
    assert _row(rec, "Q6")["action"] == "RAISE"


# ------------------------------------------------------------------ применение


def test_apply_records_applied_and_returns_items(db):
    _seed(db, "R8", "881234", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), *_fallback_sales()], funnel=(200, 3, 2, 0, 0))
    fake = _FakeProvider()
    res = pricing_service.apply_recommendations(
        db, prices_df=_prices(("881234", 2000, 10)), provider=fake, today=TODAY,
        settings={"prefer_raise": False},
    )
    assert res["applied"] == ["R8"]
    assert res["pushed"] == 1
    assert fake.applied_prices == [{
        "nmID": 881234, "price": 2000, "discount": 23,
    }]
    log = db.execute(
        select(models.PriceChange).where(models.PriceChange.article == "R8")
    ).scalars().all()
    assert log and log[-1].status == "applied"
    assert log[-1].after_discount == pytest.approx((1 - 1530 / 2000) * 100, abs=0.5)
    assert log[-1].applied_at is not None


def test_apply_error_records_error_and_raises(db):
    _seed(db, "R8", "881234", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), *_fallback_sales()], funnel=(200, 3, 2, 0, 0))
    fake = _FakeProvider(raise_on_update=True)
    with pytest.raises(RuntimeError):
        pricing_service.apply_recommendations(
            db, prices_df=_prices(("881234", 2000, 10)), provider=fake, today=TODAY,
        )
    logs = db.execute(select(models.PriceChange)).scalars().all()
    assert any(log.status == "error" and "boom" in log.reason for log in logs)


def test_apply_cooldown_blocks_reapply(db):
    _seed(db, "R8", "881234", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), *_fallback_sales()], funnel=(200, 3, 2, 0, 0))
    pricing_service.apply_recommendations(
        db, prices_df=_prices(("881234", 2000, 10)), provider=_FakeProvider(), today=TODAY,
    )
    res = pricing_service.apply_recommendations(
        db, prices_df=_prices(("881234", 2000, 10)), provider=_FakeProvider(), today=TODAY,
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


# ------------------------------------------------ apply_rows (кнопка «Применить в WB»)


def _ui_row(art="R8", nm="123456", price=2000.0, cur_disc=10.0, tgt_disc=23.5,
            action="LOWER", status="suggested"):
    return {
        "article": art, "nm_id": nm, "name": "Товар", "price": price,
        "current_discount": cur_disc, "target_discount": tgt_disc,
        "target_vis": price * (1 - tgt_disc / 100),
        "action": action, "status": status, "reason": "правила R1-R10",
        "stock": 100, "replenishable": False,
    }


def test_apply_rows_pushes_exactly_visible(db):
    """Применяет ровно переданные строки (видимые в таблице), без пересчёта."""
    fake = _FakeProvider()
    rows = [_ui_row()]
    res = pricing_service.apply_rows(db, rows, provider=fake, today=TODAY)
    assert res["applied"] == ["R8"]
    assert res["pushed"] == 1
    assert fake.applied_prices == [{"nmID": 123456, "price": 2000, "discount": 23}]
    log = db.execute(select(models.PriceChange).where(models.PriceChange.article == "R8")).scalars().all()
    assert log and log[-1].status == "applied"
    assert log[-1].after_discount == pytest.approx(23.5)
    assert log[-1].applied_at is not None


def test_apply_rows_pushes_only_matching_rows(db):
    """HOLD/RAISE/LOWER: в WB уходят только строки с действием изменения."""
    fake = _FakeProvider()
    rows = [
        _ui_row(art="R8", nm="123456", action="LOWER"),
        _ui_row(art="R9", nm="111111", action="HOLD"),
        _ui_row(art="R10", nm="222222", action="RAISE"),
    ]
    res = pricing_service.apply_rows(db, rows, provider=fake, today=TODAY)
    assert fake.applied_prices == [
        {"nmID": 123456, "price": 2000, "discount": 23},
        {"nmID": 222222, "price": 2000, "discount": 23},
    ]
    applied = [r["article"] for r in res["rows"] if r["status"] == "applied"]
    assert applied == ["R8", "R10"]


def test_apply_rows_cooldown_blocks_reapply(db):
    """RAISE/LOWER не повторяются в кулдауне (HALVE — можно)."""
    fake = _FakeProvider()
    rows = [_ui_row()]
    pricing_service.apply_rows(db, rows, provider=fake, today=TODAY)
    res = pricing_service.apply_rows(db, [_ui_row()], provider=_FakeProvider(), today=TODAY)
    assert res["pushed"] == 0
    assert res["rows"][0]["status"] == "skipped_cooldown"


def test_apply_rows_error_records_error_and_raises(db):
    """Сбой WB API → журнал 'error' + исключение (как в apply_recommendations)."""
    rows = [_ui_row()]
    with pytest.raises(RuntimeError):
        pricing_service.apply_rows(db, rows, provider=_FakeProvider(raise_on_update=True),
                                   today=TODAY)
    logs = db.execute(select(models.PriceChange)).scalars().all()
    assert any(log.status == "error" and "boom" in log.reason for log in logs)


def test_pushed_items_integerizes_and_skips_without_nm(db):
    """upload/task: целые price/discount (0..99), строки без nm-карты исключаются."""
    s = pricing_service.merge_settings({})
    rows = [
        _ui_row(art="R8", nm="123456", action="LOWER", price=2000.6, tgt_disc=23.9),
        _ui_row(art="R9", nm="", action="LOWER", tgt_disc=10.0),
        _ui_row(art="R10", nm="654321", action="RAISE", price=50.0, tgt_disc=150.0),
    ]
    items = pricing_service._pushed_items(rows, s, set())
    assert items == [
        {"nmID": 123456, "price": 2000, "discount": 23},
        {"nmID": 654321, "price": 100, "discount": 99},
    ]
    assert rows[1]["status"] == "skipped_no_nm"
    assert "nm-карты" in rows[1]["reason"]


# ------------------------------------------------------------------ T-21: мёртвые/нулевые товары и противовес


def _seed_dead(db, art, nm, price=1000, discount=50):
    """Мёртвый товар: остаток 0, продажа только давно, воронка пустая."""
    _seed(db, art, nm, stock=0, net_cost=300,
          sales=[(60, 1, 0)], funnel=(0, 0, 0, 0, 0))
    return _prices((nm, price, discount))


def test_merge_settings_handles_new_bools():
    s = merge_settings({"show_zero": "true", "prefer_raise": False,
                        "prefer_raise_bias": 0.3, "dead_min_discount": 5})
    assert s["show_zero"] is True
    assert s["prefer_raise"] is False
    assert s["prefer_raise_bias"] == pytest.approx(0.3)
    assert s["dead_min_discount"] == 5


def test_merge_settings_defaults_prefer_raise_on():
    assert merge_settings()["prefer_raise"] is True
    assert merge_settings()["show_zero"] is False


def test_dead_product_hidden_by_default(db):
    _seed_dead(db, "D1", "P1")
    rec = recommendations(db, prices_df=_prices(("P1", 1000, 50)), today=TODAY)
    assert all(r["article"] != "D1" for r in rec["rows"])
    assert rec["hidden_dead"] == 1


def test_show_zero_reveals_dead_and_halves(db):
    _seed_dead(db, "D2", "P2")
    rec = recommendations(db, prices_df=_prices(("P2", 1000, 50)), today=TODAY,
                          settings={"show_zero": True})
    row = _row(rec, "D2")
    assert row["action"] == "HALVE"
    assert row["target_discount"] == pytest.approx(25.0, abs=0.1)
    assert row["target_vis"] == pytest.approx(750.0, abs=1)
    assert row["status"] == "suggested"


def test_halving_sequence_hits_floor(db):
    """50→25→12→6→3→1, а 1% уже не делится (HOLD)."""
    df_pairs = []
    for d in (50, 25, 12, 6, 3, 1):
        _seed(db, f"H{d}", f"PH{d}", stock=0, net_cost=300,
              sales=[(60, 1, 0)], funnel=(0, 0, 0, 0, 0))
        df_pairs.append((f"PH{d}", 1000, d))
    rec = recommendations(db, prices_df=_prices(*df_pairs), today=TODAY,
                          settings={"show_zero": True})
    for disc, expect in ((50, 25), (25, 12), (12, 6), (6, 3), (3, 1)):
        row = _row(rec, f"H{disc}")
        assert row["action"] == "HALVE"
        assert row["target_discount"] == pytest.approx(expect, abs=0.1)
    row = _row(rec, "H1")
    assert row["action"] == "HOLD"
    assert "минимальна" in row["reason"]


def test_apply_halves_dead_even_when_hidden(db):
    """Применение обязано обрабатывать мёртвых даже при show_zero=False."""
    _seed_dead(db, "D3", "771111")
    fake = _FakeProvider()
    res = pricing_service.apply_recommendations(
        db, prices_df=_prices(("771111", 1000, 50)), provider=fake, today=TODAY)
    assert res["pushed"] == 1
    assert "D3" in res["applied"]
    assert fake.applied_prices == [{
        "nmID": 771111, "price": 1000, "discount": pytest.approx(25.0, abs=0.1),
    }]


def test_halve_bypasses_cooldown_within_budget(db):
    """HALVE не тонет в кулдауне: второй прогон «Применить» делит скидку снова."""
    _seed_dead(db, "D4", "772222")
    fake = _FakeProvider()
    pricing_service.apply_recommendations(
        db, prices_df=_prices(("772222", 1000, 50)), provider=fake, today=TODAY)
    res = pricing_service.apply_recommendations(
        db, prices_df=_prices(("772222", 1000, 25)), provider=fake, today=TODAY)
    assert res["pushed"] == 1
    row = next(r for r in res["rows"] if r["article"] == "D4")
    assert row["action"] == "HALVE"
    assert row["status"] == "applied"
    assert row["target_discount"] == pytest.approx(12.0, abs=0.1)


def test_stock_not_lost_when_other_marketplace_has_newer_snapshot(db):
    """Регрессия: _latest_stock брал глобальный max(date) — на свежую дату Ozon
    WB-строк нет, и у живого WB-товара остаток превращался в 0/None."""
    _seed(db, "R9", "P9", stock=100, sales=[*_fallback_sales()])
    ozon = db.execute(
        select(models.Marketplace.id).where(models.Marketplace.code == "ozon")
    ).scalar_one()
    db.add(models.Stock(marketplace_id=ozon, date=TODAY, article="ozon-only", quantity=7))
    db.commit()
    rec = recommendations(db, prices_df=_prices(("P9", 1000, 0)), today=TODAY)
    row = _row(rec, "R9")
    assert row["stock"] == 100
    assert row["status"] != "skipped_no_stock"


def test_stock_includes_in_way(db):
    """«В пути» учитывается в остатке автопилота: quantity + in_way."""
    _seed(db, "R10", "P10", stock=5, in_way=3, sales=[*_fallback_sales()])
    rec = recommendations(db, prices_df=_prices(("P10", 1000, 0)), today=TODAY)
    assert _row(rec, "R10")["stock"] == 8


def test_sales_match_products_case_insensitive(db):
    """Регрессия: продажи WB в БД могут лежать в другом регистре артикула
    ('sh031-...'), чем products ('SH031-...') — без нормализации velocity=0
    и всё падает в SKIP/no_data."""
    _seed(db, "R11", "P11", stock=10, sales=((2, 3, 0),))
    try:
        db.add(models.Sale(
            marketplace_id=db.execute(
                select(models.Marketplace.id).where(models.Marketplace.code == "wb")
            ).scalar_one(),
            date=TODAY - timedelta(days=5), article="r11", source="v5",
            quantity=2, returns_qty=0,
            revenue=1000.0, commission=150.0, logistics=80.0, storage=20.0,
            services=10.0, income=750.0,
        ))
        db.commit()
    except Exception:
        pass
    rec = recommendations(db, prices_df=_prices(("P11", 1000, 0)), today=TODAY)
    row = _row(rec, "R11")
    assert row["velocity"] > 0
    assert row["status"] != "skipped_no_data"


def test_prefer_raise_widens_deficit_zone(db):
    """DOC≈15.8: с противовесом — мощный RAISE (зона дефицита шире); без — обязательный шаг вверх."""
    _seed(db, "PR1", "PPR1", stock=8, replenishable=False,
          sales=[(2, 5, 0), (20, 1, 0), *_fallback_sales()], funnel=(100, 1, 1, 0, 900))
    rec_on = recommendations(db, prices_df=_prices(("PPR1", 1000, 10)), today=TODAY)
    row_on = _row(rec_on, "PR1")
    assert row_on["action"] == "RAISE"
    assert row_on["target_discount"] == pytest.approx(5.0, abs=0.5)  # 900 -> 950 (дефицит, полный шаг)
    rec_off = recommendations(db, prices_df=_prices(("PPR1", 1000, 10)), today=TODAY,
                              settings={"prefer_raise": False})
    row_off = _row(rec_off, "PR1")
    assert row_off["action"] == "RAISE"
    assert row_off["target_discount"] == pytest.approx(9.0, abs=0.5)  # нормальная зона -> шаг 1 п.п.
    assert "коррекция скидки обязательна" in row_off["reason"]


def test_prefer_raise_softens_overstock_drop(db):
    """Перезапас: с противовесом шаг снижения мягче (1570.5 вместо 1530)."""
    _seed(db, "PR2", "PPR2", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), *_fallback_sales()], funnel=(200, 3, 2, 0, 0))
    rec = recommendations(db, prices_df=_prices(("PPR2", 2000, 10)), today=TODAY)
    row = _row(rec, "PR2")
    assert row["action"] == "LOWER"
    assert row["target_vis"] == pytest.approx(1570.5, abs=1)
    assert row["target_discount"] == pytest.approx((1 - 1570.5 / 2000) * 100, abs=0.5)


# ------------------------------------------------------ T-24: факторы решения + базис WB-карточек


def test_wb_basis_excludes_non_wb_articles(db):
    """Автопилот работает только с WB-карточками: продукт без nm-карты (Ozon/не-WB)
    не попадает в строки, а считается в non_wb."""
    _seed(db, "W1", "PW1", stock=10, sales=[(2, 1, 0), *_fallback_sales()])
    wb = db.execute(select(models.Marketplace.id).where(models.Marketplace.code == "wb")).scalar_one()
    db.add(models.Product(article="OZ-ONLY", name="Не из WB-карточек",
                          net_cost=1, replenishable=False))
    db.add(models.Sale(
        marketplace_id=wb, date=TODAY - timedelta(days=2), article="OZ-ONLY", source="v5",
        quantity=5, returns_qty=0,
        revenue=2500.0, commission=375.0, logistics=200.0, storage=50.0,
        services=25.0, income=1850.0,
    ))
    db.commit()
    rec = recommendations(db, prices_df=_prices(("PW1", 1000, 0)), today=TODAY)
    assert all(r["article"] != "OZ-ONLY" for r in rec["rows"])
    assert rec["non_wb"] == 1
    assert any(r["article"] == "W1" for r in rec["rows"])


def test_use_inventory_off_ignores_doc(db):
    """use_inventory=False: перезапас больше не снижает — остаток не влияет (но шаг обязателен)."""
    _seed(db, "I1", "PI1", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), *_fallback_sales()], funnel=(200, 3, 2, 0, 0))
    rec = recommendations(db, prices_df=_prices(("PI1", 2000, 10)), today=TODAY,
                          settings={"prefer_raise": False, "use_inventory": False})
    row = _row(rec, "I1")
    assert row["action"] == "LOWER"
    assert row["target_discount"] == pytest.approx(11.0, abs=0.5)
    assert "остаток не влияет" in row["reason"]


def test_use_sales_off_blocks_dead_stock_lowering(db):
    """use_sales=False: продажи не влияют — но остаток есть, коррекция скидки обязательна (LOWER на 1)."""
    _seed(db, "S1", "PS1", stock=100, sales=[*_fallback_sales()])
    rec = recommendations(db, prices_df=_prices(("PS1", 1000, 0)), today=TODAY,
                          settings={"use_sales": False})
    row = _row(rec, "S1")
    assert row["action"] == "LOWER"
    assert row["target_discount"] == pytest.approx(1.0, abs=0.5)
    assert "продажи не влияют" in row["reason"]


def test_use_orders_off_disables_hot_raise(db):
    """use_orders=False: нет «горячего спроса» — докупаемый дефицит всё равно поднимаем на шаг."""
    _seed(db, "O1", "PO1", stock=2, replenishable=True,
          sales=[(2, 5, 0), (20, 1, 0), *_fallback_sales()], funnel=(100, 10, 1, 0, 900))
    rec = recommendations(db, prices_df=_prices(("PO1", 1000, 10)), today=TODAY,
                          settings={"use_orders": False})
    row = _row(rec, "O1")
    assert row["action"] == "RAISE"
    assert row["target_discount"] == pytest.approx(9.0, abs=0.5)
    assert "темп важнее" in row["reason"]


def test_use_margin_off_relaxes_unit_econ_requirement(db):
    """use_margin=False: нехватка unit-экономики не блокирует решение."""
    _seed(db, "MU1", "PMU1", stock=10, sales=[(5, 2, 3)])
    rec_on = recommendations(db, prices_df=_prices(("PMU1", 1000, 0)), today=TODAY)
    assert _row(rec_on, "MU1")["status"] == "skipped_no_ratio"
    rec_off = recommendations(db, prices_df=_prices(("PMU1", 1000, 0)), today=TODAY,
                              settings={"use_margin": False})
    assert _row(rec_off, "MU1")["status"] != "skipped_no_ratio"


def test_use_replenishable_off_treats_as_last_units(db):
    """use_replenishable=False: докупаемый дефицит поднимаем как «последние единицы»."""
    _seed(db, "RPL1", "PRPL1", stock=2, replenishable=True,
          sales=[(2, 5, 0), (20, 1, 0), *_fallback_sales()], funnel=(100, 1, 1, 0, 900))
    rec = recommendations(db, prices_df=_prices(("PRPL1", 1000, 10)), today=TODAY,
                          settings={"use_replenishable": False})
    assert _row(rec, "RPL1")["action"] == "RAISE"


def test_use_quality_off_default_allows_raise_with_bad_rating(db):
    """use_quality=False (дефолт): низкий рейтинг не блокирует повышение."""
    _seed(db, "Q0", "PQ0", stock=2, replenishable=False,
          sales=[(2, 5, 0), (20, 1, 0), *_fallback_sales()],
          funnel=(100, 1, 1, 0, 900), funnel_extra={"product_rating": 1.0})
    rec = recommendations(db, prices_df=_prices(("PQ0", 1000, 10)), today=TODAY)
    assert _row(rec, "Q0")["action"] == "RAISE"


def test_use_season_off_removes_trend_from_decision(db):
    """use_season=False: скорость без экстраполяции тренда и без пометки в причине."""
    _seed(db, "SE1", "PSE1", stock=2000,
          sales=[(5, 1, 0), (14, 10, 0), *_fallback_sales()], funnel=(200, 3, 2, 0, 0))
    rec_on = recommendations(db, prices_df=_prices(("PSE1", 2000, 10)), today=TODAY,
                             settings={"prefer_raise": False})
    rec_off = recommendations(db, prices_df=_prices(("PSE1", 2000, 10)), today=TODAY,
                              settings={"prefer_raise": False, "use_season": False})
    on_row = _row(rec_on, "SE1")
    off_row = _row(rec_off, "SE1")
    assert on_row["action"] == off_row["action"] == "LOWER"
    assert "тренд" in on_row["reason"]           # сезонность учитывается
    assert "тренд" not in off_row["reason"]      # выключенный тренд не влияет
    assert off_row["v_proj"] == pytest.approx(off_row["velocity"], abs=1e-9)
    assert on_row["v_proj"] < off_row["v_proj"]  # экстраполяция смягчает спад


# ----------------------------------- Рейтинг по отзывам (ценный товар не раздают дёшево)


def test_rating_reviews_high_limits_overstock_lower(db):
    """Рейтинг по отзывам 5.0 → ценному товару скидку не увеличиваем: только обязательный шаг."""
    _seed(db, "RR1", "PRR1", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), *_fallback_sales()], funnel=(200, 3, 2, 0, 0),
          funnel_extra={"feedback_rating": 5.0})
    rec = recommendations(db, prices_df=_prices(("PRR1", 2000, 10)), today=TODAY,
                          settings={"prefer_raise": False})
    row = _row(rec, "RR1")
    assert row["action"] == "LOWER"
    assert row["target_discount"] == pytest.approx(11.0, abs=0.5)  # 10 -> 11 (минимальный шаг)
    assert "ценный товар" in row["reason"]


def test_rating_reviews_mid_softens_overstock_drop(db):
    """4.5 (≥ порог 4.0) → LOWER разрешён, но скидка меньше, чем без рейтинга."""
    _seed(db, "RR2", "PRR2", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), *_fallback_sales()], funnel=(200, 3, 2, 0, 0),
          funnel_extra={"feedback_rating": 4.5})
    rec = recommendations(db, prices_df=_prices(("PRR2", 2000, 10)), today=TODAY,
                          settings={"prefer_raise": False})
    row = _row(rec, "RR2")
    assert row["action"] == "LOWER"
    rated_disc = row["target_discount"]

    _seed(db, "RR2B", "PRR2B", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), *_fallback_sales()], funnel=(200, 3, 2, 0, 0))
    rec_base = recommendations(db, prices_df=_prices(("PRR2B", 2000, 10)), today=TODAY,
                               settings={"prefer_raise": False})
    base_disc = _row(rec_base, "RR2B")["target_discount"]
    assert rated_disc < base_disc


def test_rating_reviews_below_threshold_no_effect(db):
    """3.8 < порог 4.0 → поведение как у R8 (без ограничений)."""
    _seed(db, "RR3", "PRR3", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), *_fallback_sales()], funnel=(200, 3, 2, 0, 0),
          funnel_extra={"feedback_rating": 3.8})
    rec = recommendations(db, prices_df=_prices(("PRR3", 2000, 10)), today=TODAY,
                          settings={"prefer_raise": False})
    row = _row(rec, "RR3")
    assert row["action"] == "LOWER"
    assert row["target_discount"] == pytest.approx((1 - 1530 / 2000) * 100, abs=0.5)


def test_rating_reviews_unknown_no_effect(db):
    """Нет данных о рейтинге (0) → ограничений нет."""
    _seed(db, "RR4", "PRR4", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), *_fallback_sales()], funnel=(200, 3, 2, 0, 0))
    rec = recommendations(db, prices_df=_prices(("PRR4", 2000, 10)), today=TODAY,
                          settings={"prefer_raise": False})
    row = _row(rec, "RR4")
    assert row["action"] == "LOWER"
    assert row["target_discount"] == pytest.approx((1 - 1530 / 2000) * 100, abs=0.5)


def test_rating_reviews_high_limits_dead_stock_lower(db):
    """Мёртвый товар с рейтингом 5.0 → скидку не делим, но коррекция обязательна (шаг 1)."""
    _seed(db, "RR5", "PRR5", stock=100, sales=[*_fallback_sales()],
          funnel_extra={"feedback_rating": 5.0})
    rec = recommendations(db, prices_df=_prices(("PRR5", 1000, 0)), today=TODAY,
                          settings={"prefer_raise": False})
    row = _row(rec, "RR5")
    assert row["action"] == "LOWER"
    assert row["target_discount"] == pytest.approx(1.0, abs=0.5)
    assert "ценный товар" in row["reason"]


def test_rating_reviews_does_not_limit_raise(db):
    """Ценность по рейтингу не мешает RAISE при дефиците."""
    _seed(db, "RR6", "PRR6", stock=5, replenishable=False,
          sales=[(2, 6, 0), (20, 1, 0), *_fallback_sales()], funnel=(100, 1, 1, 0, 0),
          funnel_extra={"feedback_rating": 4.9})
    rec = recommendations(db, prices_df=_prices(("PRR6", 1000, 20)), today=TODAY)
    assert _row(rec, "RR6")["action"] == "RAISE"


def test_use_reviews_off_disables_rating_cap(db):
    """use_reviews=False: рейтинг 5.0 больше не держит скидку — перезапас LOWER как обычно."""
    _seed(db, "RR7", "PRR7", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), *_fallback_sales()], funnel=(200, 3, 2, 0, 0),
          funnel_extra={"feedback_rating": 5.0})
    rec = recommendations(db, prices_df=_prices(("PRR7", 2000, 10)), today=TODAY,
                          settings={"prefer_raise": False, "use_reviews": False})
    row = _row(rec, "RR7")
    assert row["action"] == "LOWER"
    assert row["target_discount"] == pytest.approx((1 - 1530 / 2000) * 100, abs=0.5)


# ------------------------------------------------------------------ «Старая версия» (mode=old)


def test_merge_settings_accepts_mode_string():
    """mode — строковый параметр, должен проходить через merge_settings как есть."""
    assert merge_settings({"mode": "old"})["mode"] == "old"
    assert merge_settings({})["mode"] == "new"


def test_legacy_mode_normalized_and_default_is_new(db):
    """Мусор в mode → "new"; дефолт — тоже "new"."""
    _seed(db, "OLDM", "PMDM", stock=10, sales=[(3, 1, 0), *_fallback_sales()])
    rec = recommendations(db, prices_df=_prices(("PMDM", 1000, 20)), today=TODAY,
                          settings={"mode": "whatever"})
    assert rec["settings"]["mode"] == "new"
    assert "старая модель" not in _row(rec, "OLDM")["reason"]


def test_legacy_mode_echo_in_settings(db):
    """mode=old проходит через recommendations() и возвращается в settings."""
    _seed(db, "OLDE", "PME", stock=10, sales=[(3, 1, 0), *_fallback_sales()])
    rec = recommendations(db, prices_df=_prices(("PME", 1000, 20)), today=TODAY,
                          settings={"mode": "old"})
    assert rec["settings"]["mode"] == "old"


def test_legacy_sold_out_resets_discount_to_quarter(db):
    """Старая модель: остаток 0 → скидка урезается до discount/4 (цена восстанавливается).
    Порт price_module.discount(reset_if_null=True): 30% → 7.5% → RAISE."""
    _seed(db, "OLDRAISE", "POR", stock=0, sales=[(3, 1, 0), *_fallback_sales()])
    rec = recommendations(db, prices_df=_prices(("POR", 1000, 30)), today=TODAY,
                          settings={"mode": "old"})
    row = _row(rec, "OLDRAISE")
    assert row["action"] == "RAISE"
    assert row["status"] == "suggested"
    assert row["target_discount"] == pytest.approx(7.5, abs=0.5)
    assert "старая модель" in row["reason"]


def test_legacy_rich_price_lowers_discount(db):
    """Старая модель: цена с большим запасом над себестоимостью → k>1 → скидка растёт.
    price=2000 / скидка 5% / net_cost=100: k_discount≈1.05, n_delta<0 → LOWER в диапазоне ~8-9%."""
    _seed(db, "OLDLOWER", "POL", stock=5, net_cost=100,
          sales=[*_fallback_sales()])
    rec = recommendations(db, prices_df=_prices(("POL", 2000, 5)), today=TODAY,
                          settings={"mode": "old"})
    row = _row(rec, "OLDLOWER")
    assert row["action"] == "LOWER"
    assert row["status"] == "suggested"
    assert row["target_discount"] == pytest.approx(8.6, abs=0.5)


def test_legacy_respects_floor_guard(db):
    """Стражи работают и в старой модели: целевая цена не ниже пола безубыточности."""
    _seed(db, "OLDFLOOR", "POF", stock=50, net_cost=800,
          sales=[*_fallback_sales()])
    rec = recommendations(db, prices_df=_prices(("POF", 1000, 5)), today=TODAY,
                          settings={"mode": "old", "max_discount_pct": 50})
    row = _row(rec, "OLDFLOOR")
    if row["status"] == "suggested":
        assert row["target_vis"] is not None
        assert row["target_vis"] >= 0
        assert 0 <= row["target_discount"] <= 100
    # договорные поля на месте в любом случае
    assert {"action", "status", "reason"}.issubset(row.keys())


# ------------------------------------------------------------ акции WB (T-31) R11


def _active_promo_row(promo_id=777, name="ХИТЫ ГОДА", desc="Промо-скидка не более 7%",
                      participation=30.0, tiers=None, starts=None, ends=None):
    now = datetime.utcnow()
    return models.Promotion(
        promo_id=promo_id, name=name, adv_type="auto", description=desc,
        starts_at=starts if starts is not None else now - timedelta(days=1),
        ends_at=ends if ends is not None else now + timedelta(days=5),
        participation_percent=participation,
        ranging_json=json.dumps(tiers or [
            {"participationRate": 20, "boost": 0, "condition": "до 20% участия"},
            {"participationRate": 50, "boost": 30, "condition": "50% участия"},
        ]),
    )


def test_promo_cap_pct_parses_cap():
    assert _promo_cap_pct("промо-скидка не более 5% (автоматически)") == 5.0
    assert _promo_cap_pct("скидка до 10 % для участия") == 10.0
    assert _promo_cap_pct("без ограничений") is None
    assert _promo_cap_pct("не более 3,5%") == 3.5


def test_parse_ranging_sorts_and_filters():
    raw = json.dumps([
        {"participationRate": 70, "boost": 5, "condition": "m"},
        {"participationRate": 0, "boost": 0, "condition": "не участвует"},
        {"participationRate": 40, "boost": 15, "condition": "m"},
    ])
    tiers = _parse_ranging(raw)
    assert [t["rate"] for t in tiers] == [40.0, 70.0]
    assert tiers[0]["boost"] == 15.0
    assert _parse_ranging("") == []
    assert _parse_ranging("not a json") == []


def _promo_rows():
    """Строки для _promo_pass, различающиеся залежатостью, себестоимостью и сбытом.

    STALE  — продаж нет 200 дн., себестоимость 15% цены, лежит 100 шт → лучший кандидат.
    FRESH  — продаётся 0.6 шт/дн (выше порога защиты), но не 30+ дней назад.
    PRICEY — залежатый, но себестоимость 95% цены → резать дорого, оценка ниже.
    RAISED — автопилот поднимает цену, разгрузка не трогает.
    """
    base = {
        "article": "X", "nm_id": "123", "price": 2000.0, "current_discount": 10.0,
        "stock": 100, "stock_wb": 0, "doc": None, "floor_price": 1000.0,
        "comm_rate": 0.15, "logistics_unit": 40.0, "storage_unit": 10.0,
        "other_unit": 5.0, "net_cost": 300.0,
        "velocity": 0.0, "v_proj": 0.0, "last_sale_days_ago": 200,
    }
    stale = dict(base, article="STALE", action="LOWER", target_discount=12.0,
                 target_vis=2000 * 0.88, status="suggested", reason="правила R1-R10")
    fresh = dict(base, article="FRESH", action="LOWER", target_discount=12.0,
                 target_vis=2000 * 0.88, status="suggested", reason="правила R1-R10",
                 velocity=0.6, v_proj=0.6, last_sale_days_ago=45)
    pricey = dict(base, article="PRICEY", action="LOWER", target_discount=12.0,
                  target_vis=2000 * 0.88, status="suggested", reason="правила R1-R10",
                  net_cost=1900.0)
    raise_ = dict(base, article="RAISED", action="RAISE", target_discount=8.0,
                  target_vis=2000 * 0.92)
    return [stale, fresh, pricey, raise_]


def _wb_promo(name="ХИТЫ ГОДА", participation=30.0, cap=None, in_total=0, not_total=0,
              tiers=None):
    """Акция WB в виде словаря, как его отдаёт _active_promotions."""
    return {"id": 777, "name": name, "participation": participation, "cap": cap,
            "in_total": in_total, "not_total": not_total, "adv_type": "auto",
            "tiers": tiers if tiers is not None else [
                {"rate": 10.0, "boost": 25.0, "condition": ""},
                {"rate": 50.0, "boost": 30.0, "condition": ""},
                {"rate": 70.0, "boost": 35.0, "condition": ""},
            ]}


def test_promo_pass_disabled_adds_info_only():
    rows = _promo_rows()
    _promo_pass(rows, [_wb_promo(cap=7.0, in_total=261, not_total=701)],
                merge_settings({"promo_enabled": False}), {})
    assert rows[0]["promo_count"] == 1
    assert rows[0]["promo_names"] == "ХИТЫ ГОДА"
    assert rows[0]["promo_part_pct"] == 30.0
    assert rows[0]["promo_tier_pct"] == 50.0
    assert rows[0]["promo_tier_boost"] == 30.0
    assert rows[0]["promo_need_rows"] == 220
    assert rows[0]["promo_cap_pct"] == 7.0
    assert rows[0]["promo_push_applied"] is False
    assert rows[0]["promo_score"] is None          # оценка только при включённой
    assert rows[0]["promo_score_confidence"] is None
    assert rows[0]["action"] == "LOWER"
    assert rows[3]["action"] == "RAISE"


def test_promo_need_rows_counts_from_wb_pool_not_our_assortment():
    """Нужно добирать не до «доли всех наших товаров», а до доли ПУЛА WB.

    Раньше needed считался от len(rows) — то есть от числа всех наших позиций,
    и оценка получалась завышенной в разы (для пула 962 требовалось 220 товаров,
    а не сотни).
    """
    rows = _promo_rows()
    # participation 27% → следующая ступень 50%. Пул WB = 261 + 701 = 962.
    _promo_pass(rows, [_wb_promo(participation=27.0, in_total=261, not_total=701)],
                merge_settings({"promo_enabled": False}), {})
    assert rows[0]["promo_need_rows"] == int(round(0.5 * 962)) - 261 == 220

    # Узкий пул: WB ждёт всего 1 товар, а не 31.
    rows = _promo_rows()
    _promo_pass(rows, [_wb_promo(participation=0.0, in_total=0, not_total=94,
                                 tiers=[{"rate": 1.0, "boost": 25.0, "condition": ""}])],
                merge_settings({"promo_enabled": False}), {})
    assert rows[0]["promo_need_rows"] == 1


def test_promo_pass_dedups_names_of_same_action():
    """WB отдаёт несколько promo_id на одно действие — имена схлопываются."""
    rows = _promo_rows()
    tier = [{"rate": 50.0, "boost": 30.0, "condition": ""}]
    promos = [
        _wb_promo(name="Осенние скидки", participation=30.0, tiers=tier),
        _wb_promo(name="Осенние скидки", participation=3.0, tiers=tier),
        _wb_promo(name="Экспресс-скидки", participation=1.0, tiers=tier),
    ]
    _promo_pass(rows, promos, merge_settings({"promo_enabled": False}), {})
    assert rows[0]["promo_names"] == "Осенние скидки, Экспресс-скидки"
    assert rows[0]["promo_count"] == 3  # кол-во акций считаем по promo_id


def test_promo_pass_names_capped_at_three():
    rows = _promo_rows()
    promos = [_wb_promo(name=f"Акция {i}") for i in range(5)]
    _promo_pass(rows, promos, merge_settings({"promo_enabled": False}), {})
    assert rows[0]["promo_names"] == "Акция 0, Акция 1, Акция 2…"


def _tier_promo(tiers=None, participation=30.0, name="ХИТЫ ГОДА"):
    """Акция с лестницей ступеней: 10% → ×25, 50% → ×30, 70% → ×35."""
    return [_wb_promo(name=name, participation=participation, tiers=tiers)]


def test_promo_pass_delta_is_zero_without_push():
    """promo_delta_discount = 0.0 у всех строк, кроме реально разгруженных."""
    rows = _promo_rows()
    _promo_pass(rows, _tier_promo(), merge_settings({"promo_enabled": False}), {})
    assert [r["promo_delta_discount"] for r in rows] == [0.0] * 4

    rows = _promo_rows()
    _promo_pass(rows, _tier_promo(), merge_settings({
        "promo_enabled": True, "promo_push_pct": 2.0, "min_delta_pp": 1.0,
        "promo_max_rows": 1, "promo_protect_velocity": 0.5,
    }), {})
    stale = rows[0]
    assert stale["promo_delta_discount"] == pytest.approx(2.0)   # 12 → 14
    assert stale["target_discount"] - stale["current_discount"] == pytest.approx(4.0)
    assert stale["delta_discount"] == pytest.approx(4.0)        # общая дельта 10 → 14
    assert all(r["promo_delta_discount"] == 0.0 for r in rows[1:])


def test_promo_pass_ranks_by_sacrifice_not_by_margin():
    """Отбор — по залежалости/себестоимости/остатку, а НЕ по марже.

    Раньше первым шёл товар с наименьшей маржой, но margin_per_one заполнен
    лишь у ~50 товаров из 3098 — то есть сортировка ранжировала нули.
    """
    rows = _promo_rows()
    # Намеренно запутываем старую эвристику: у PRICEY маржа выше, но он
    # залежавший; у STALE маржа ниже, но он залежавший и дешёвый по себестоимости.
    rows[0]["margin_per_one"] = 500.0
    rows[2]["margin_per_one"] = 1.0
    _promo_pass(rows, _tier_promo(), merge_settings({
        "promo_enabled": True, "promo_push_pct": 2.0, "min_delta_pp": 1.0,
        "promo_max_rows": 1,
    }), {})
    assert rows[0]["promo_push_applied"] is True      # STALE выигрывает
    assert rows[2]["promo_push_applied"] is False     # PRICEY проиграл


def test_promo_pass_protects_fast_movers():
    """Живой товар (сбыт > promo_protect_velocity) разгрузкой не трогается."""
    rows = _promo_rows()
    _promo_pass(rows, _tier_promo(), merge_settings({
        "promo_enabled": True, "promo_push_pct": 5.0, "min_delta_pp": 1.0,
        "promo_protect_velocity": 0.5,
    }), {})
    fresh = rows[1]
    assert fresh["promo_push_applied"] is False
    assert fresh["promo_delta_discount"] == 0.0
    assert fresh["action"] == "LOWER"          # остаётся решением R1-R10

    # Защиту можно отключить — тогда товар разгружается.
    rows = _promo_rows()
    _promo_pass(rows, _tier_promo(), merge_settings({
        "promo_enabled": True, "promo_push_pct": 5.0, "min_delta_pp": 1.0,
        "promo_protect_velocity": 0.0,
    }), {})
    assert rows[1]["promo_push_applied"] is True


def test_promo_pass_respects_min_stale_days():
    """Товар с недавними продажами не берётся, даже если формально залежал."""
    rows = _promo_rows()
    rows[0]["last_sale_days_ago"] = 10      # продавался 10 дн. назад
    rows[2]["last_sale_days_ago"] = 200     # а этот давно не продавался
    _promo_pass(rows, _tier_promo(), merge_settings({
        "promo_enabled": True, "promo_push_pct": 2.0, "min_delta_pp": 1.0,
        "promo_min_stale_days": 30, "promo_protect_velocity": 0.5,
    }), {})
    assert rows[0]["promo_push_applied"] is False
    assert rows[2]["promo_push_applied"] is True


def test_promo_pass_respects_max_rows():
    """Больше promo_max_rows товаров за проход не разгружается."""
    rows = _promo_rows() + [dict(_promo_rows()[0], article=f"MORE{i}") for i in range(5)]
    _promo_pass(rows, _tier_promo(), merge_settings({
        "promo_enabled": True, "promo_push_pct": 2.0, "min_delta_pp": 1.0,
        "promo_max_rows": 2, "promo_protect_velocity": 0.5,
    }), {})
    assert sum(1 for r in rows if r["promo_push_applied"]) == 2


def test_promo_pass_total_drop_capped_by_max_drop_pct():
    """Суммарное падение не превышает общий лимит max_drop_pct.

    Раньше потолок считался только от порога безубытка, поэтому шаг R4 (минус
    15 п.п.) плюс шаг разгрузки складывались и уходили за общий лимит.
    """
    rows = [_promo_rows()[0]]
    rows[0]["current_discount"] = 10.0
    rows[0]["target_discount"] = 24.0        # R4 уже снизил на 14
    _promo_pass(rows, _tier_promo(), merge_settings({
        "promo_enabled": True, "promo_push_pct": 20.0, "min_delta_pp": 1.0,
        "max_drop_pct": 15.0,
    }), {})
    r = rows[0]
    assert r["promo_push_applied"] is True
    assert r["target_discount"] - r["current_discount"] == pytest.approx(15.0, abs=0.05)


def test_promo_pass_score_confidence_marks_fallbacks():
    """Неполные данные по строке помечаются, а не выдаются за точный расчёт."""
    rows = _promo_rows()
    rows[0]["stock"] = None          # свой склад не заведён → фолбэк на остаток WB
    rows[0]["stock_wb"] = 40
    rows[0]["last_sale_days_ago"] = 9999   # продаж не было вовсе (или новинка)
    _promo_pass(rows, _tier_promo(), merge_settings({
        "promo_enabled": True, "promo_push_pct": 2.0, "min_delta_pp": 1.0,
        "promo_max_rows": 1,
    }), {})
    r = rows[0]
    assert r["promo_push_applied"] is True
    assert r["promo_score_confidence"] == "partial"

    rows = _promo_rows()
    _promo_pass(rows, _tier_promo(), merge_settings({
        "promo_enabled": True, "promo_push_pct": 2.0, "min_delta_pp": 1.0,
        "promo_max_rows": 1,
    }), {})
    assert rows[0]["promo_score_confidence"] == "full"


def test_promo_pass_score_prefers_cheap_to_cut_and_big_stock():
    """score растёт от залежалости, дешевизны реза и объёма остатка."""
    from app.services.pricing import _sacrifice_score

    dead, cheap = dict(_promo_rows()[0]), dict(_promo_rows()[0])
    cheap["article"] = "CHEAP"
    cheap["net_cost"] = 20.0            # себестоимость 1% цены
    assert _sacrifice_score(cheap)[0] > _sacrifice_score(dead)[0]

    big = dict(_promo_rows()[0])
    big["article"] = "BIG"
    big["stock"] = 200
    assert _sacrifice_score(big)[0] > _sacrifice_score(dead)[0]

    old = dict(_promo_rows()[0])
    old["article"] = "OLD"
    old["last_sale_days_ago"] = 365
    assert _sacrifice_score(old)[0] > _sacrifice_score(dead)[0]

    # Насыщение: «никогда не продавался» не перевешивает года залежания.
    never = dict(_promo_rows()[0])
    never["last_sale_days_ago"] = 9999
    assert _sacrifice_score(never)[0] == pytest.approx(_sacrifice_score(old)[0])


def test_promo_pass_pushes_even_when_tier_already_reached():
    """Разгрузка не про участие: если ступень уже взята, товар всё равно режется."""
    rows = [_promo_rows()[0]]
    _promo_pass(rows, _tier_promo(participation=90.0), merge_settings({
        "promo_enabled": True, "promo_push_pct": 2.0, "min_delta_pp": 1.0,
    }), {})
    assert rows[0]["promo_tier_pct"] is None       # ступени выше уже нет
    assert rows[0]["promo_push_applied"] is True   # но разгрузка работает
    assert rows[0]["target_discount"] == pytest.approx(14.0)


def test_promo_pass_reason_reports_factual_push():
    """Причина описывает, сколько добавили и почему товар выбран."""
    rows = [dict(_promo_rows()[0], current_discount=40.0, target_discount=41.0)]
    _promo_pass(rows, _tier_promo(), merge_settings({
        "promo_enabled": True, "promo_push_pct": 2.0, "min_delta_pp": 1.0,
    }), {})
    reason = rows[0]["reason"]
    assert "разгрузка" in reason
    assert "+2.0 п.п." in reason
    assert "нет продаж 200 дн." in reason
    assert "остаток 100 шт" in reason
    assert "себестоимость 15% цены" in reason
    assert "добор участия" not in reason and "до тира" not in reason


def test_promo_pass_keeps_r4_reason_and_appends_note():
    """При уже вынесенном решении R1-R10 разгрузка дописывает причину, не затирая её."""
    rows = [dict(_promo_rows()[0], reason="мёртвый запас: продаж нет 200 дн., остаток 100 шт")]
    _promo_pass(rows, _tier_promo(), merge_settings({
        "promo_enabled": True, "promo_push_pct": 2.0, "min_delta_pp": 1.0,
    }), {})
    reason = rows[0]["reason"]
    assert reason.startswith("мёртвый запас")
    assert "разгрузка" in reason
    # факты, уже названные R4, не дублируются
    assert reason.count("продаж нет") == 1 and "нет продаж" not in reason
    assert reason.count("остаток") == 1
    assert "себестоимость 15% цены" in reason   # новое — остаётся


def test_promo_pass_enabled_turns_hold_into_lower():
    rows = [dict(_promo_rows()[0], action="HOLD", target_discount=None)]
    _promo_pass(rows, _tier_promo(), merge_settings({
        "promo_enabled": True, "promo_push_pct": 2.0, "min_delta_pp": 1.0,
    }), {})
    hold = rows[0]
    assert hold["action"] == "LOWER"               # HOLD → LOWER с разгрузкой
    assert hold["promo_push_applied"] is True
    assert hold["target_discount"] == pytest.approx(12.0)  # 10 + push 2
    assert hold["delta_discount"] == pytest.approx(2.0)


def test_promo_pass_limits_below_floor_to_beyond_pp():
    # Пол по unit-экономике: цена 2000, floor 1400 → floor_disc=30%. Разгрузка
    # может уйти ниже пола только до promo_max_beyond_floor_pp (5 п.п.) → 35%.
    rows = [_promo_rows()[0]]
    rows[0]["floor_price"] = 1400.0
    rows[0]["target_discount"] = 33.0
    _promo_pass(rows, _tier_promo(), merge_settings({
        "promo_enabled": True, "promo_push_pct": 100.0, "min_delta_pp": 1.0,
        "max_drop_pct": 99.0,
    }), {})
    low = rows[0]
    assert low["target_discount"] == pytest.approx(35.0)   # 30 (пол) + 5 (beyond)
    assert low["promo_push_applied"] is True


def test_zero_days_since_last_sale_is_not_dead_stock(db):
    """Регрессия: продажа СЕГОДНЯ = 0 дней, и товар не должен считаться мёртвым.

    Раньше `f.get(...) or 9999` превращал 0 в 9999, из-за чего только что
    проданный товар получал статус «мёртвый запас» (R4) и максимальную
    залежалость для разгрузки (R11).
    """
    _seed(db, "TODAY-1", "9901", stock=100,
          sales=[(0, 1, 0), (40, 1, 0)], funnel=(200, 3, 2, 0, 0))
    db.commit()
    res = recommendations(db, prices_df=_prices(("9901", 2000, 10)), today=TODAY)
    row = _row(res, "TODAY-1")
    assert row["last_sale_days_ago"] == 0        # а не 9999 «никогда не продавался»
    assert "мёртвый запас" not in (row["reason"] or "")


def test_promo_pass_no_stock_is_not_candidate():
    """Разгружать нечего: без остатка (ни своего, ни WB) товар не берётся."""
    rows = [_promo_rows()[0]]
    rows[0]["stock"] = 0
    rows[0]["stock_wb"] = 0
    _promo_pass(rows, _tier_promo(), merge_settings({
        "promo_enabled": True, "promo_push_pct": 10.0, "min_delta_pp": 1.0,
    }), {})
    assert rows[0]["promo_push_applied"] is False
    assert rows[0]["promo_delta_discount"] == 0.0


def test_recommendations_apply_promo_push(db):
    """Интеграция: активная акция в БД поднимает целевую скидку LOWER-строки."""
    _seed(db, "R8", "881234", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), *_fallback_sales()], funnel=(200, 3, 2, 0, 0))
    db.add(_active_promo_row())
    db.commit()
    base = recommendations(db, prices_df=_prices(("881234", 2000, 10)), today=TODAY)
    promo = recommendations(db, prices_df=_prices(("881234", 2000, 10)), today=TODAY,
                            settings={"promo_enabled": True, "promo_push_pct": 2.0,
                                      # товар свежий (продажи 5 и 14 дней назад),
                                      # поэтому порог залежалости снимаем
                                      "promo_min_stale_days": 0})
    row_base = _row(base, "R8")
    row_promo = _row(promo, "R8")
    assert row_base["action"] == "LOWER"
    assert row_base["promo_count"] == 1
    assert row_base["promo_push_applied"] is False
    assert row_promo["promo_push_applied"] is True
    assert row_promo["target_discount"] == pytest.approx(
        row_base["target_discount"] + 2.0, abs=0.1
    )
    assert row_promo["delta_discount"] == pytest.approx(
        row_promo["target_discount"] - 10.0, abs=0.1
    )


def test_active_promotions_ignores_finished(db):
    now = datetime.utcnow()
    db.add(_active_promo_row(promo_id=1, starts=now - timedelta(days=10),
                             ends=now - timedelta(days=1)))
    db.add(_active_promo_row(promo_id=2, starts=now + timedelta(days=1),
                             ends=now + timedelta(days=9)))
    db.commit()
    active = _active_promotions(db)
    assert active == []