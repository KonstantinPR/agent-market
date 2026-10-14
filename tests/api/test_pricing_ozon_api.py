"""API-тесты автопилота цен Ozon (/api/pricing-ozon/*)."""
import io
from datetime import date, timedelta

import pandas as pd
from sqlalchemy import select

from app import models
from app.services.pricing_ozon import OZON_PRICING_DEFAULTS

TODAY = date.today()


def _mp(db):
    return db.execute(
        select(models.Marketplace.id).where(models.Marketplace.code == "ozon")
    ).scalar_one()


def _seed(db, base, *, name="Товар", net_cost=300, price=1000, stock=100,
          sizes=(), sells=(), replenishable=False):
    db.add(models.Product(article=base, name=name, net_cost=net_cost,
                          replenishable=replenishable))
    for sz in (list(sizes) or [""]):
        offer = base if not sz else f"{base}-{sz}"
        db.add(models.PriceSnapshot(
            marketplace="ozon", article=offer, base_article=base, nm_id="P" + offer,
            size=sz, price=price, discounted_price=price, discount=0))
    if stock is not None:
        db.add(models.Stock(marketplace_id=_mp(db), date=TODAY - timedelta(days=1),
                            article=base, base_article=base, quantity=stock))
    for i, (offset, qty, ret) in enumerate(sells):
        db.add(models.OzonDetailRow(
            op_key=f"{base}-{offset}-{i}", source="api", date=TODAY - timedelta(days=offset),
            offer_id=base, base_article=base, name=name, quantity=qty, return_qty=ret,
            seller_price=price, amount=price * qty,
            commission=-price * qty * 0.10, standard_fee=-50.0, income=price * qty * 0.8))
    db.commit()


def test_ozon_defaults(api_client):
    r = api_client.get("/api/pricing-ozon/defaults")
    assert r.status_code == 200
    d = r.json()["defaults"]
    assert d["window_days"] == OZON_PRICING_DEFAULTS["window_days"]
    assert d["oz_price_step"] == 1.0
    assert d["use_orders"] is False


def test_ozon_recommendations_read_only(db, api_client):
    _seed(db, "OZONE", stock=100, price=1000, sells=[])
    r = api_client.post("/api/pricing-ozon/recommendations", json={})
    body = r.json()
    assert r.status_code == 200 and body["rows"]
    assert any(row["action"] == "LOWER" for row in body["rows"])
    assert db.execute(select(models.PriceChange)).first() is None


def test_ozon_apply_pushes_and_writes_history(db, api_client):
    _seed(db, "OZONE", stock=100, price=1000, sells=[])
    r = api_client.post("/api/pricing-ozon/apply", json={})
    body = r.json()
    assert r.status_code == 200
    assert body["applied"] == ["OZONE"]
    assert body["pushed"] == 1
    hist = api_client.get("/api/pricing/history").json()
    row = hist["rows"][0]
    assert row["article"] == "OZONE" and row["status"] == "applied"
    assert row["action"] == "LOWER" and row["after_discount"] < row["before_discount"]


def test_ozon_apply_error_returns_502(db, api_client, stub_oz):
    _seed(db, "OZONE", stock=100, price=1000, sells=[])
    stub_oz.update_prices_error = RuntimeError("boom: Ozon недоступен")
    r = api_client.post("/api/pricing-ozon/apply", json={})
    assert r.status_code == 502 and "boom" in r.json()["detail"]
    hist = api_client.get("/api/pricing/history").json()
    assert any(x["status"] == "error" for x in hist["rows"])


def test_ozon_apply_cooldown_blocks_second(db, api_client):
    _seed(db, "OZONE", stock=100, price=1000, sells=[])
    assert api_client.post("/api/pricing-ozon/apply", json={}).json()["pushed"] == 1
    second = api_client.post("/api/pricing-ozon/apply", json={}).json()
    assert second["pushed"] == 0


def test_ozon_apply_min_pushes_floor(db, api_client, stub_oz):
    _seed(db, "OZONE", stock=100, price=300, net_cost=300, sells=[(1, 2, 0)])
    rec = api_client.post("/api/pricing-ozon/recommendations", json={}).json()
    row = next(r for r in rec["rows"] if r["article"] == "OZONE")
    r = api_client.post("/api/pricing-ozon/apply-min", json={"ui_rows": [row]})
    assert r.status_code == 200 and r.json()["pushed"] == 1
    assert stub_oz.applied_prices[0]["price"] == row["floor_price"]
    hist = api_client.get("/api/pricing/history").json()
    assert any(x["action"] == "MIN" for x in hist["rows"])


def test_ozon_export_excel(db, api_client):
    _seed(db, "OZONE", stock=100, price=1000, sells=[])
    r = api_client.post("/api/pricing-ozon/export", json={})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats")
    assert int(r.headers["x-count"]) >= 1
    df = pd.read_excel(io.BytesIO(r.content))
    assert not df.empty
