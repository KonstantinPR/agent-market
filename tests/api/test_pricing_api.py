"""API-тесты автопилота цен WB и флага докупаемости."""
import io
import json
from datetime import date, datetime, timedelta

import pandas as pd
import pytest
from sqlalchemy import select

from app import models
from app.services.pricing import PRICING_DEFAULTS

TODAY = date.today()


def _read_xlsx(r):
    return pd.read_excel(io.BytesIO(r.content))


def _seed(db, art, nm, *, stock, sales, funnel=(0, 0, 0, 0, 0), replenishable=False):
    """Товар TST-n с nm-картой, остатком, воронкой, продажами (см. unit/test_pricing._seed)."""
    wb = db.execute(select(models.Marketplace.id).where(models.Marketplace.code == "wb")).scalar_one()
    db.add(models.Product(article=art, name=art, net_cost=300, replenishable=replenishable))
    db.add(models.NmArticle(nm_id=nm, article=art))
    db.add(models.Stock(marketplace_id=wb, date=TODAY - timedelta(days=1),
                        article=art, warehouse="Все", quantity=stock))
    views, adds, orders, cancelled, avg_price = funnel
    db.add(models.FunnelMetric(
        date_from=TODAY - timedelta(days=7), date_to=TODAY - timedelta(days=1),
        nm_id=nm, article=art, views=views, adds=adds, orders=orders,
        cancelled=cancelled, avg_price=avg_price,
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


def test_defaults(api_client):
    r = api_client.get("/api/pricing/defaults")
    assert r.status_code == 200
    assert r.json()["defaults"]["window_days"] == PRICING_DEFAULTS["window_days"]
    assert r.json()["defaults"]["cooldown_days"] == 3
    # T-14: новые (пороговые) параметры присутствуют в дефолтах
    assert r.json()["defaults"]["min_rating_for_raise"] == 4.2
    assert r.json()["defaults"]["raise_boost_pct"] == 25.0


def test_recommendations_read_only(db, api_client):
    _seed(db, "TST-1", "1001", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), (40, 1, 0)], funnel=(200, 3, 2, 0, 0))
    r = api_client.post("/api/pricing/recommendations", json={})
    body = r.json()
    assert r.status_code == 200
    assert body["rows"]
    assert any(row["action"] == "LOWER" for row in body["rows"])
    # read-only: не пишет журнал
    assert db.execute(select(models.PriceChange)).first() is None


def test_recommendations_applies_settings(db, api_client):
    _seed(db, "TST-1", "1001", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), (40, 1, 0)], funnel=(200, 3, 2, 0, 0))
    r = api_client.post("/api/pricing/recommendations", json={"window_days": 7})
    assert r.json()["settings"]["window_days"] == 7


def test_recommendations_honors_dates(db, api_client):
    """T-13: период берётся из дат запроса (шапки), а не из window_days."""
    _seed(db, "TST-1", "1001", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), (40, 1, 0)], funnel=(200, 3, 2, 0, 0))
    r = api_client.post("/api/pricing/recommendations",
                        json={"date_from": "2026-08-01", "date_to": "2026-08-14"})
    body = r.json()
    assert body["date_from"] == "2026-08-01"
    assert body["date_to"] == "2026-08-14"
    assert body["window_days"] == 14


def test_apply_pushes_and_writes_history(db, api_client):
    _seed(db, "TST-1", "1001", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), (40, 1, 0)], funnel=(200, 3, 2, 0, 0))
    r = api_client.post("/api/pricing/apply", json={})
    body = r.json()
    assert r.status_code == 200
    assert body["applied"] == ["TST-1"]
    assert body["pushed"] == 1
    # журнал
    hist = api_client.get("/api/pricing/history").json()
    assert hist["rows"]
    row = hist["rows"][0]
    assert row["article"] == "TST-1"
    assert row["status"] == "applied"
    assert row["action"] == "LOWER"
    assert row["before_discount"] == 0.0 and row["after_discount"] > 0


def test_apply_error_returns_502_and_logs(db, api_client, stub_wb):
    _seed(db, "TST-1", "1001", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), (40, 1, 0)], funnel=(200, 3, 2, 0, 0))
    stub_wb.update_prices_error = RuntimeError("boom: WB API недоступен")
    r = api_client.post("/api/pricing/apply", json={})
    assert r.status_code == 502
    assert "boom" in r.json()["detail"]
    hist = api_client.get("/api/pricing/history").json()
    assert any(x["status"] == "error" for x in hist["rows"])


def test_apply_cooldown_blocks_second(db, api_client):
    _seed(db, "TST-1", "1001", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), (40, 1, 0)], funnel=(200, 3, 2, 0, 0))
    first = api_client.post("/api/pricing/apply", json={}).json()
    assert first["pushed"] == 1
    second = api_client.post("/api/pricing/apply", json={}).json()
    assert second["pushed"] == 0
    assert any(r.get("status") == "skipped_cooldown"
               for r in second["rows"] if r["action"] in ("RAISE", "LOWER"))


def test_apply_min_prices_pushes_floor(db, api_client, stub_wb):
    """Минимальные цены: только строки ниже пола, price=пол, discount=0, action=MIN."""
    _seed(db, "TST-1", "1001", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), (40, 1, 0)], funnel=(200, 3, 2, 0, 0))
    rec = api_client.post("/api/pricing/recommendations", json={}).json()
    row = next(r for r in rec["rows"] if r["article"] == "TST-1")
    floor = float(row["floor_price"])
    row["price"] = floor + 300.0
    row["current_vis"] = floor - 10.0
    row["current_discount"] = 75.0
    r = api_client.post("/api/pricing/apply-min", json={"ui_rows": [row]})
    assert r.status_code == 200
    body = r.json()
    assert body["pushed"] == 1
    assert stub_wb.applied_prices[0]["discount"] == 0
    assert stub_wb.applied_prices[0]["price"] >= floor - 1e-6
    hist = api_client.get("/api/pricing/history").json()
    assert any(x["action"] == "MIN" for x in hist["rows"])


def test_apply_min_prices_nothing_below_floor(db, api_client):
    _seed(db, "TST-1", "1001", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), (40, 1, 0)], funnel=(200, 3, 2, 0, 0))
    rec = api_client.post("/api/pricing/recommendations", json={}).json()
    row = next(r for r in rec["rows"] if r["article"] == "TST-1")
    row["current_vis"] = float(row["floor_price"]) + 100.0
    r = api_client.post("/api/pricing/apply-min", json={"ui_rows": [row]})
    assert r.status_code == 200
    assert r.json()["pushed"] == 0


def test_export_excel(db, api_client):
    _seed(db, "TST-1", "1001", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), (40, 1, 0)], funnel=(200, 3, 2, 0, 0))
    r = api_client.post("/api/pricing/export", json={})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert int(r.headers["x-count"]) >= 1
    assert "filename=" in r.headers["content-disposition"]
    assert b"PK" in r.content[:4]
    assert db.execute(select(models.PriceChange)).first() is None


def test_export_contains_funnel_columns(db, api_client):
    """T-46: в Excel-выгрузку автопилота попали поля воронки продаж."""
    _seed(db, "TST-1", "1001", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), (40, 1, 0)], funnel=(200, 3, 2, 0, 0))
    r = api_client.post("/api/pricing/export", json={})
    assert r.status_code == 200
    df = _read_xlsx(r)
    for col in ("Просмотры", "Добавления в корзину", "Заказы", "Отмены",
                "Просмотр→Корзина, %", "Корзина→Заказ, %", "Заказов в день"):
        assert col in df.columns, col
    row = df.iloc[0]
    assert int(row["Просмотры"]) == 200
    assert int(row["Заказы"]) == 2
    assert int(row["Добавления в корзину"]) == 3


def test_replenishable_toggle(db, api_client):
    _seed(db, "TST-1", "1001", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), (40, 1, 0)], funnel=(200, 3, 2, 0, 0))
    r = api_client.post("/api/products/replenishable",
                        json={"article": "TST-1", "value": True})
    assert r.status_code == 200
    assert r.json()["replenishable"] is True
    prods = api_client.get("/api/products").json()["rows"]
    row = next(p for p in prods if p["article"] == "TST-1")
    assert row["replenishable"] is True


def test_replenishable_not_found(api_client):
    r = api_client.post("/api/products/replenishable",
                        json={"article": "NO-SUCH", "value": True})
    assert r.status_code == 404


def test_replenishable_empty_article_400(api_client):
    r = api_client.post("/api/products/replenishable", json={"article": "", "value": True})
    assert r.status_code == 400


# ------------------------------------------------------------ акции WB (T-31)


def _active_promo_db(db, promo_id=777, participation=30.0):
    now = datetime.utcnow()
    db.add(models.Promotion(
        promo_id=promo_id, name="ХИТЫ ГОДА", adv_type="auto",
        description="промо-скидка не более 7%",
        starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=5),
        participation_percent=participation,
        ranging_json=json.dumps([
            {"participationRate": 20, "boost": 0},
            {"participationRate": 50, "boost": 30},
        ]),
    ))
    db.commit()


def test_defaults_include_promo(db, api_client):
    r = api_client.get("/api/pricing/defaults")
    d = r.json()["defaults"]
    assert d["promo_enabled"] is False
    assert d["promo_push_pct"] == 2.0
    assert d["promo_max_beyond_floor_pp"] == 5.0
    # пороги отбора разгрузки
    assert d["promo_max_rows"] == 30
    assert d["promo_min_stale_days"] == 30
    assert d["promo_protect_velocity"] == 0.5


def test_recommendations_promo_fields(db, api_client):
    _seed(db, "TST-1", "1001", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), (40, 1, 0)], funnel=(200, 3, 2, 0, 0))
    _active_promo_db(db)
    off = api_client.post("/api/pricing/recommendations", json={}).json()
    on = api_client.post("/api/pricing/recommendations",
                         json={"promo_enabled": True, "promo_push_pct": 2.0,
                               # товар свежий — порог залежалости снимаем
                               "promo_min_stale_days": 0}).json()
    r_off = next(r for r in off["rows"] if r["article"] == "TST-1")
    r_on = next(r for r in on["rows"] if r["article"] == "TST-1")
    assert r_off["action"] == "LOWER"
    assert r_off["promo_count"] == 1
    assert r_off["promo_tier_pct"] == 50.0
    assert r_off["promo_cap_pct"] == 7.0
    assert r_off["promo_push_applied"] is False
    assert r_off["promo_score"] is None          # выключено — оценки нет
    assert r_on["promo_push_applied"] is True
    assert r_on["target_discount"] == pytest.approx(r_off["target_discount"] + 2.0, abs=0.1)
    # вклад акции отделён от общей дельты
    assert r_off["promo_delta_discount"] == 0.0
    assert r_on["promo_delta_discount"] == pytest.approx(2.0, abs=0.1)
    assert r_on["promo_delta_discount"] == pytest.approx(
        r_on["target_discount"] - r_off["target_discount"], abs=0.1)
    # оценка разгрузки и её полнота — вместо прежних gap/boost
    assert r_on["promo_score"] > 0
    assert r_on["promo_score_confidence"] in ("full", "partial")
    assert r_on["last_sale_days_ago"] == 5
    assert "+2.0 п.п." in r_on["reason"]
    assert "разгрузка" in r_on["reason"]
    assert "добор участия" not in r_on["reason"]
    # удалённые диагностические поля больше не отдаём
    for gone in ("promo_gap_pp", "promo_boost_gain"):
        assert gone not in r_on


def test_export_contains_promo_columns(db, api_client):
    _seed(db, "TST-1", "1001", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), (40, 1, 0)], funnel=(200, 3, 2, 0, 0))
    _active_promo_db(db)
    r = api_client.post("/api/pricing/export", json={"promo_enabled": True})
    assert r.status_code == 200
    df = _read_xlsx(r)
    # спека R11 (T-51/T-34): разгрузка склада, а не добор участия
    for col in ("Вклад разгрузки в скидку, п.п.", "Оценка жертвенности",
                "Полнота оценки", "Дней без продаж"):
        assert col in df.columns, col
    # старые диагностики доборучастия (gap/boost) убраны
    for col in ("Разрыв до тира, п.п.", "Буст своего уровня, ×"):
        assert col not in df.columns, col


def test_promo_refresh_api(db, api_client):
    r = api_client.post("/api/promo/refresh")
    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == 1 and body["db_rows"] == 1
    p = db.execute(select(models.Promotion)).scalar_one()
    assert p.promo_id == 7001
    assert float(p.participation_percent) == 25.0
    pulls = db.execute(
        select(models.ApiPull).where(models.ApiPull.kind == "promotions")
    ).scalars().all()
    assert pulls and pulls[-1].api == "wb"


def test_promo_list_api(db, api_client):
    _active_promo_db(db, promo_id=8001, participation=15.0)
    r = api_client.get("/api/promo/list")
    body = r.json()
    assert body["promotions"]
    row = next(p for p in body["promotions"] if p["promo_id"] == 8001)
    assert row["name"] == "ХИТЫ ГОДА"
    assert row["participation_percent"] == 15.0
    assert row["cap_pct"] == 7.0
    assert row["tiers"][0]["rate"] == 20.0


# ------------------------------------ номенклатура акций / потолок (T-41 / R19)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_promo_refresh_api_fetches_nomenclatures_for_regular(db, api_client, stub_wb):
    """Refresh тянет planDiscount только для активных regular-акций (auto — 422)."""
    now = datetime.utcnow()
    stub_wb.promo_list = [
        {"id": 9100, "name": "ЛЕТО", "type": "regular",
         "startDateTime": _iso(now - timedelta(days=1)),
         "endDateTime": _iso(now + timedelta(days=5))},
        {"id": 9200, "name": "АВТО", "type": "auto",
         "startDateTime": _iso(now - timedelta(days=1)),
         "endDateTime": _iso(now + timedelta(days=5))},
    ]
    stub_wb.promo_nomenclatures = {
        9100: {
            True: [{"id": "1001", "inaction": True, "price": 4200,
                    "planprice": 4116, "discount": 0, "plandiscount": 2}],
            False: [{"id": "1002", "inaction": False, "price": 2000,
                     "planprice": 1860, "discount": 3, "plandiscount": 7}],
        },
        9200: {
            True: [{"id": "9999", "inaction": True, "plandiscount": 50}],
            False: [],
        },
    }
    r = api_client.post("/api/promo/refresh")
    assert r.status_code == 200
    assert r.json()["promo_nom_rows"] == 2
    noms = db.execute(select(models.PromoNomenclature)).scalars().all()
    assert sorted((n.promo_id, n.nm_id) for n in noms) == [(9100, "1001"), (9100, "1002")]


def test_recommendations_promo_plan_cap_api(db, api_client):
    """promo_plan_pct/promo_cap_applied в ответе и потолок по плану WB."""
    _seed(db, "TST-1", "1001", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), (40, 1, 0)], funnel=(200, 3, 2, 0, 0))
    plain = api_client.post("/api/pricing/recommendations",
                            json={"promo_enabled": True}).json()
    r_plain = next(r for r in plain["rows"] if r["article"] == "TST-1")
    assert r_plain["promo_plan_pct"] is None
    assert r_plain["promo_cap_applied"] is False

    now = datetime.utcnow()
    db.add(models.Promotion(
        promo_id=9300, name="ЛЕТО", adv_type="regular", description="",
        starts_at=now - timedelta(days=1), ends_at=now + timedelta(days=5),
        participation_percent=10.0, ranging_json=""))
    db.add(models.PromoNomenclature(
        promo_id=9300, nm_id="1001", in_action=False,
        price=2000, plan_price=1980, discount=0, plan_discount=1.0))
    db.commit()
    capped = api_client.post("/api/pricing/recommendations",
                             json={"promo_enabled": True}).json()
    row = next(r for r in capped["rows"] if r["article"] == "TST-1")
    assert row["promo_plan_pct"] == 1.0
    assert row["action"] == "LOWER"
    assert row["target_discount"] == pytest.approx(1.0, abs=0.01)
    assert row["promo_cap_applied"] is True


# --- T-48: минимальная цена автоакций WB ------------------------------------

def test_minpromo_status_empty(db, api_client):
    """До первой выгрузки статус пустой (last_export_at=None, expired=False)."""
    r = api_client.get("/api/pricing/minpromo-status")
    assert r.status_code == 200
    body = r.json()
    assert body["last_export_at"] is None
    assert body["days_ago"] is None
    assert body["validity_days"] == 30
    assert body["expired"] is False


def test_minpromo_export_writes_template_and_journal(db, api_client):
    """Экспорт: файл-шаблон WB + журнал MINPRICE/exported + заголовки X-Count/Total."""
    _seed(db, "TST-1", "1001", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), (40, 1, 0)], funnel=(200, 3, 2, 0, 0))
    r = api_client.post("/api/pricing/minpromo-export", json={"minpromo_block": True,
                                                              "minpromo_block_days": 7.0})
    assert r.status_code == 200
    assert r.headers["Content-Type"].startswith("application/vnd.openxmlformats")
    assert int(r.headers["X-Count"]) >= 1
    assert int(r.headers["X-Total"]) >= int(r.headers["X-Count"])
    assert r.headers["X-Truncated"] in ("0", "1")

    df = _read_xlsx(r)
    assert list(df.columns) == [
        "Артикул WB",
        "Новая минимальная цена для применения скидки по автоакции",
        "Новая блокировка применения скидки по автоакции",
    ]
    # Целые и не ниже 100 ₽; блокировка проставлена по настройкам
    assert (df.iloc[:, 1] >= 100).all()
    assert (df.iloc[:, 1] == df.iloc[:, 1].astype(int)).all()
    assert (df.iloc[:, 2] == "на 7 дней").all()

    # Журнал: запись MINPRICE/exported (без applied_at — в WB ещё не загружено)
    rec = db.execute(select(models.PriceChange)
                     .where(models.PriceChange.action == "MINPRICE")).scalars().first()
    assert rec is not None
    assert rec.status == "exported"
    assert rec.applied_at is None

    # Статус после выгрузки: дата есть, срок не истёк
    st = api_client.get("/api/pricing/minpromo-status").json()
    assert st["last_export_at"] is not None
    assert st["days_ago"] == 0
    assert st["expired"] is False


def test_minpromo_export_honors_ui_rows(db, api_client):
    """ui_rows (видимые строки) ограничивают файл — как в apply-min."""
    _seed(db, "TST-1", "1001", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), (40, 1, 0)], funnel=(200, 3, 2, 0, 0))
    _seed(db, "TST-2", "1002", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), (40, 1, 0)], funnel=(200, 3, 2, 0, 0))
    all_rows = api_client.post("/api/pricing/recommendations", json={}).json()["rows"]
    only = [r for r in all_rows if r["article"] == "TST-1"]
    r = api_client.post("/api/pricing/minpromo-export", json={"ui_rows": only})
    assert r.status_code == 200
    df = _read_xlsx(r)
    assert len(df) == 1  # только TST-1


def test_minpromo_defaults_in_defaults_endpoint(api_client):
    """minpromo-ключи есть в /pricing/defaults (merge_settings их знает)."""
    d = api_client.get("/api/pricing/defaults").json()["defaults"]
    assert d["minpromo_mode"] == "floor_reserve"
    assert d["minpromo_reserve_pct"] == 5.0
    assert d["minpromo_block"] is False
    assert d["minpromo_block_days"] == 30.0