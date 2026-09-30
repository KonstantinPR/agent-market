"""API-тесты автопилота цен WB и флага докупаемости."""
import json
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import select

from app import models
from app.services.pricing import PRICING_DEFAULTS

TODAY = date.today()


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


def test_recommendations_promo_fields(db, api_client):
    _seed(db, "TST-1", "1001", stock=1000,
          sales=[(5, 1, 0), (14, 1, 0), (40, 1, 0)], funnel=(200, 3, 2, 0, 0))
    _active_promo_db(db)
    off = api_client.post("/api/pricing/recommendations", json={}).json()
    on = api_client.post("/api/pricing/recommendations",
                         json={"promo_enabled": True, "promo_push_pct": 2.0}).json()
    r_off = next(r for r in off["rows"] if r["article"] == "TST-1")
    r_on = next(r for r in on["rows"] if r["article"] == "TST-1")
    assert r_off["action"] == "LOWER"
    assert r_off["promo_count"] == 1
    assert r_off["promo_tier_pct"] == 50.0
    assert r_off["promo_cap_pct"] == 7.0
    assert r_off["promo_push_applied"] is False
    assert r_on["promo_push_applied"] is True
    assert r_on["target_discount"] == pytest.approx(r_off["target_discount"] + 2.0, abs=0.1)


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