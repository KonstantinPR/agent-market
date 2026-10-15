"""Юнит-тесты автопилота цен Ozon (price-based движок по базовому артикулу)."""
from datetime import date, timedelta

import pandas as pd
import pytest
from sqlalchemy import select

from app import models
from app.services import pricing_ozon as oz

TODAY = date.today()


def _mp(db):
    return db.execute(
        select(models.Marketplace.id).where(models.Marketplace.code == "ozon")
    ).scalar_one()


def _seed(db, base, *, name="Товар", net_cost=300, price=1000, old_price=None,
          stock=100, in_way=0, sizes=(), sells=(), replenishable=False, value=0,
          set_name=True):
    """Засевает Ozon-товар: продукт, снимок цен (офферы), остаток, детализацию."""
    if set_name:
        db.add(models.Product(article=base, name=name, net_cost=net_cost,
                              value=value, replenishable=replenishable))
    offs = list(sizes) or [""]
    for sz in offs:
        offer = base if not sz else f"{base}-{sz}"
        db.add(models.PriceSnapshot(
            marketplace="ozon", article=offer, base_article=base, nm_id="P" + offer,
            size=sz, price=price, discounted_price=price, discount=0,
        ))
    if stock is not None:
        db.add(models.Stock(marketplace_id=_mp(db), date=TODAY - timedelta(days=1),
                            article=base, base_article=base, quantity=stock, in_way=in_way))
    for i, (offset, qty, ret) in enumerate(sells):
        d = TODAY - timedelta(days=offset)
        db.add(models.OzonDetailRow(
            op_key=f"{base}-{offset}-{i}", source="api", date=d,
            offer_id=base, base_article=base, name=name, quantity=qty, return_qty=ret,
            seller_price=price, amount=price * qty,
            commission=-price * qty * 0.10, standard_fee=-50.0, income=price * qty * 0.8,
        ))
    db.commit()


def _row(rec, art):
    return next(r for r in rec["rows"] if r["article"] == art)


# ----------------------------------------------------------- цены / группировка
def test_load_ozon_prices_from_db(db):
    _seed(db, "OZ-1", price=1200, sizes=("42", "43"))
    df, updated_at = oz.load_ozon_prices_from_db(db)
    assert df is not None and updated_at is not None
    norm = oz._normalize_ozon_prices(df)
    assert set(norm) == {"OZ-1-42", "OZ-1-43"}


def test_load_ozon_prices_empty(db):
    df, updated_at = oz.load_ozon_prices_from_db(db)
    assert df is None and updated_at is None


def test_resolve_ozon_prices_prefers_db(db, stub_oz):
    _seed(db, "OZ-1", price=1200)
    df, _u, source = oz.resolve_ozon_prices(db, provider=stub_oz)
    assert source == "db"


def test_resolve_ozon_prices_falls_back_to_api(db, stub_oz):
    df, _u, source = oz.resolve_ozon_prices(db, provider=stub_oz)
    assert source == "ozon_api" and df is not None and not df.empty


def test_normalize_provider_prices():
    df = pd.DataFrame({
        "offer_id": ["A"], "product_id": ["P1"],
        "price_price": [1000.0], "price_old_price": [1500.0],
    })
    norm = oz._normalize_ozon_prices(df)
    assert norm["A"]["price"] == 1000.0 and norm["A"]["old_price"] == 1500.0


def test_group_offers_representative_is_max(db):
    _seed(db, "TIE", price=1000, sizes=("42", "43"))
    df, _u = oz.load_ozon_prices_from_db(db)
    # поднимем цену одного оффера
    df.loc[df["offer_id"] == "TIE-43", "price"] = 1500.0
    groups = oz._group_offers(oz._normalize_ozon_prices(df), oz._offer_base_map(db))
    g = groups["TIE"]
    assert g["price"] == 1500.0 and g["offers_count"] == 2
    assert [o["offer_id"] for o in g["offers"]] == ["TIE-42", "TIE-43"]


# ----------------------------------------------------------- правила решения
def test_sold_out_holds(db):
    _seed(db, "OZ-1", stock=0, sells=[(1, 1, 0)])
    rec = oz.recommendations_ozon(db)
    r = _row(rec, "OZ-1")
    assert r["action"] == "HOLD" and r["status"] == "hold"


def test_stock_excludes_in_way(db):
    """«В пути» не входит в остаток Ozon: stock = quantity, in_way отдельно."""
    _seed(db, "OZONE", stock=5, in_way=3, sells=[(1, 1, 0)])
    rec = oz.recommendations_ozon(db)
    r = _row(rec, "OZONE")
    assert r["stock"] == 5
    assert r["in_way"] == 3


def test_no_stock_data_skips(db):
    _seed(db, "OZ-1", stock=None, sells=[(1, 1, 0)])
    rec = oz.recommendations_ozon(db)
    r = _row(rec, "OZ-1")
    assert r["action"] == "SKIP" and r["status"] == "skipped_no_stock"


def test_dead_stock_lowers(db):
    _seed(db, "OZ-1", stock=100, price=1000, sells=[])
    rec = oz.recommendations_ozon(db)
    r = _row(rec, "OZ-1")
    assert r["action"] == "LOWER"
    assert r["target_price"] < r["price"]
    assert r["target_price"] >= r["floor_price"]


def test_overstock_lowers(db):
    _seed(db, "OZ-1", stock=100, price=1000, sells=[(1, 2, 0)])
    rec = oz.recommendations_ozon(db)
    r = _row(rec, "OZ-1")
    assert r["action"] == "LOWER" and r["target_price"] >= r["floor_price"]


def test_lower_soften_divides_ozon_drop(db):
    """R18: lower_soften=4 → снижение цены Ozon в 4 раза мягче, чем при 1."""
    _seed(db, "OZS", stock=100, price=1000, sells=[(1, 2, 0)])
    hard = _row(oz.recommendations_ozon(db, settings={"lower_soften": 1.0}), "OZS")
    soft = _row(oz.recommendations_ozon(db, settings={"lower_soften": 4.0}), "OZS")
    assert hard["action"] == soft["action"] == "LOWER"
    assert soft["target_price"] > hard["target_price"]
    assert (soft["price"] - soft["target_price"]) == pytest.approx(
        (hard["price"] - hard["target_price"]) / 4.0, abs=0.5)


def test_lower_soften_keeps_ozon_min_step(db):
    """Обязательный шаг цены (min_delta_pct/oz_price_step) не смягчается."""
    _seed(db, "OZM", stock=100, price=1000, sells=[])  # мёртвый запас → обязательный шаг
    rec = oz.recommendations_ozon(db, settings={"lower_soften": 4.0})
    r = _row(rec, "OZM")
    assert r["action"] == "LOWER"
    assert r["price"] - r["target_price"] == pytest.approx(10.0, abs=0.5)  # шаг 1% = 10 ₽


def test_deficit_not_replenishable_raises(db):
    _seed(db, "OZ-1", stock=2, price=1000, sells=[(1, 3, 0)], replenishable=False)
    rec = oz.recommendations_ozon(db)
    r = _row(rec, "OZ-1")
    assert r["action"] == "RAISE" and r["target_price"] > r["price"]


def test_lower_never_below_floor(db):
    # очень большой перезапас, но пол высокий -> цена не должна уйти ниже пола
    _seed(db, "OZ-1", stock=1000, price=1000, net_cost=900, sells=[(1, 1, 0)])
    rec = oz.recommendations_ozon(db)
    r = _row(rec, "OZ-1")
    assert r["floor_price"] is not None
    assert r["target_price"] >= r["floor_price"] - 1e-6


def test_floor_from_unit_economics(db):
    _seed(db, "OZONE", stock=100, price=1000, net_cost=300, sells=[(1, 2, 0)])
    rec = oz.recommendations_ozon(db)
    r = _row(rec, "OZONE")
    # comm_rate = 10% -> floor = 300 / (1 - 0.1 - 0.1) = 375
    assert r["floor_price"] == 375.0


def test_returns_skip_when_enabled(db):
    _seed(db, "OZONE", stock=100, price=1000, sells=[(1, 1, 1)])
    rec = oz.recommendations_ozon(db, settings={"use_returns": True})
    r = _row(rec, "OZONE")
    assert r["action"] == "SKIP" and r["status"] == "skipped_returns"


# ----------------------------------------------------------- применение
def test_expand_offers_preserves_relative_difference(db):
    _seed(db, "TIE", price=1000, sizes=("42", "43"))
    df, _u = oz.load_ozon_prices_from_db(db)
    df.loc[df["offer_id"] == "TIE-43", "price"] = 1200.0
    groups = oz._group_offers(oz._normalize_ozon_prices(df), oz._offer_base_map(db))
    r = groups["TIE"]
    r["target_price"] = r["price"] * 0.9
    pushed = oz._expand_offers(r)
    by = {p["offer_id"]: p["price"] for p in pushed}
    assert by["TIE-42"] == 900.0 and by["TIE-43"] == 1080.0


def test_apply_rows_ozon_pushes_and_journals(db, stub_oz):
    _seed(db, "OZ-1", stock=100, price=1000, sells=[])
    rec = oz.recommendations_ozon(db)
    rows = rec["rows"]
    res = oz.apply_rows_ozon(db, rows, provider=stub_oz)
    assert res["pushed"] == 1
    assert stub_oz.applied_prices[0]["offer_id"] == "OZ-1"
    assert stub_oz.applied_prices[0]["price"] < 1000
    changes = db.execute(select(models.PriceChange)).scalars().all()
    assert any(c.action == "LOWER" and c.status == "applied" for c in changes)


def test_apply_cooldown_skips(db, stub_oz):
    _seed(db, "OZ-1", stock=100, price=1000, sells=[])
    db.add(models.PriceChange(article="OZ-1", calculated_at=date.today(),
                              applied_at=date.today(), action="LOWER", status="applied"))
    db.commit()
    rec = oz.recommendations_ozon(db)
    res = oz.apply_rows_ozon(db, rec["rows"], provider=stub_oz)
    assert res["pushed"] == 0


def test_apply_min_prices_raises_to_floor(db, stub_oz):
    _seed(db, "OZ-1", stock=100, price=300, net_cost=300, sells=[(1, 2, 0)])
    rec = oz.recommendations_ozon(db)
    row = _row(rec, "OZ-1")
    assert row["price"] < row["floor_price"]
    res = oz.apply_min_prices_ozon(db, rows=[row], provider=stub_oz)
    assert res["pushed"] == 1
    assert stub_oz.applied_prices[0]["price"] == row["floor_price"]
