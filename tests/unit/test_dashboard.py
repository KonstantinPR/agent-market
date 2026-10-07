"""Тесты сервиса дашборда (app/services/dashboard.py)."""
from datetime import date, timedelta

from sqlalchemy import select

from app import models
from app.services.dashboard import (
    dashboard_kpis, top_products, prefix_margin, price_delta,
    stocks_summary, freshness, prefix_group,
)

TODAY = date(2026, 9, 20)


def _wb_row(op_key, article, day, qty, amount, income, net_cost=None,
            doc_type="Продажа"):
    return models.WbDetailRow(
        op_key=op_key, source="excel", article=article, doc_type_name=doc_type,
        sale_dt=day, quantity=qty, retail_amount=amount, for_pay=income,
    )


def _oz_row(op_key, article, day, qty=1, price=1000.0, income=850.0, amount=None):
    return models.OzonDetailRow(
        op_key=op_key, source="api", date=day, posting_number="p" + op_key,
        offer_id=article, name="Товар " + article, sku="", barcode="",
        quantity=qty, seller_price=price, amount=amount or price * qty,
        commission_ratio=0.0, commission=-100.0, standard_fee=-50.0,
        income=income, return_qty=0, return_total=0.0,
    )


# ------------------------------------------------------------------ префиксы
def test_prefix_group_mapping():
    assert prefix_group("SHK-802-1") == "SH"
    assert prefix_group("SH-120") == "SH"
    assert prefix_group("SK-1") == "SK"
    assert prefix_group("SN-99") == "SK"
    assert prefix_group("SF-1") == "SF"
    assert prefix_group("JBG-802-1052S2-24") == "J"
    assert prefix_group("JZ2-1") == "JZ"
    assert prefix_group("JZ-1") == "JZ"
    assert prefix_group("MIT-1") == "MIT"
    assert prefix_group("AN-U1") == "MIT"
    assert prefix_group("IBZ-1") == "IBZ"
    assert prefix_group("TIE-1") == "TIE"
    assert prefix_group("FATA-1") == "F"
    assert prefix_group("V-90") == "F"
    assert prefix_group("SOHOCOAT-32") == "SOHO"
    assert prefix_group("SOHO-32") == "SOHO"
    assert prefix_group("SOHO-FRNT-2S") == "SOHO-FRNT"
    assert prefix_group("MHSB-1") == "MHS"
    assert prefix_group("SEL-1") == "STL"
    assert prefix_group("KP-1") == "KR"
    assert prefix_group("BOL-1") == "BIJ"
    assert prefix_group("Y00-1") == "Y00"
    assert prefix_group("ABC-555") == "остальные"
    assert prefix_group("") == "остальные"
    assert prefix_group(None) == "остальные"


# ------------------------------------------------------------------ KPI
def test_dashboard_kpis_combines_wb_and_ozon(db):
    db.add(models.Product(article="WB-1", name="ВБ товар", net_cost=100.0))
    db.add(models.Product(article="OZ-1", name="Озон товар", net_cost=300.0))
    db.add(_wb_row("w1", "WB-1", TODAY, qty=2, amount=2000.0, income=1800.0))
    db.add(_oz_row("o1", "OZ-1", TODAY))
    db.commit()

    k = dashboard_kpis(db, TODAY - timedelta(days=10), TODAY, None, "all")
    tot = k["total"]
    assert tot["sells"] == 3
    assert tot["revenue"] == 3000.0
    assert tot["income"] == 2650.0
    assert tot["margin"] == 2150.0              # 1600 + 550
    assert tot["margin_pct"] == 81.13           # 2150/2650*100
    assert tot["margin_gross"] == 2650.0        # 1800 + 850 (до себестоимости)
    assert tot["articles"] == 2
    by_mp = {m["marketplace"]: m for m in k["per_mp"]}
    assert by_mp["wb"]["margin"] == 1600.0      # 1800 - 100*2
    assert by_mp["wb"]["margin_gross"] == 1800.0
    assert by_mp["ozon"]["margin"] == 550.0     # 850 - 300
    assert by_mp["ozon"]["margin_gross"] == 850.0
    assert k["compare"] is None


def test_dashboard_kpis_compare_prev_period(db):
    db.add(models.Product(article="WB-1", name="ВБ", net_cost=100.0))
    db.add(_wb_row("w1", "WB-1", TODAY, qty=2, amount=2000.0, income=1800.0))
    db.add(_wb_row("w0", "WB-1", TODAY - timedelta(days=10), qty=1,
                   amount=600.0, income=500.0))
    db.commit()
    prev = (TODAY - timedelta(days=14), TODAY - timedelta(days=7))
    k = dashboard_kpis(db, TODAY, TODAY, prev, "wb")
    assert k["total"]["margin"] == 1600.0
    cmp = k["compare"]
    assert cmp["prev"]["margin"] == 400.0       # 500 - 100
    assert cmp["delta_ru"] == 1200.0
    assert cmp["delta_pct"] == 300.0            # 1200/400*100


def test_dashboard_kpis_filters_marketplace(db):
    db.add(models.Product(article="WB-1", name="ВБ", net_cost=0.0))
    db.add(models.Product(article="OZ-1", name="Оз", net_cost=0.0))
    db.add(_wb_row("w1", "WB-1", TODAY, qty=2, amount=2000.0, income=1800.0))
    db.add(_oz_row("o1", "OZ-1", TODAY))
    db.commit()
    k = dashboard_kpis(db, TODAY, TODAY, None, "wb")
    assert [m["marketplace"] for m in k["per_mp"]] == ["wb"]
    assert k["total"]["sells"] == 2


# ------------------------------------------------------------------ топы
def test_top_products_profit_and_loss(db):
    db.add(models.Product(article="WB-1", name="ВБ", net_cost=100.0))
    db.add(models.Product(article="OZ-1", name="Оз", net_cost=300.0))
    db.add(_wb_row("w1", "WB-1", TODAY, qty=2, amount=2000.0, income=1800.0))
    db.add(_oz_row("o1", "OZ-1", TODAY))
    # Убыток: нет себестоимости в products → default_net_cost 500 > income 100.
    db.add(_oz_row("o2", "OZ-XL-9", TODAY, price=400.0, income=100.0, amount=400.0))
    db.commit()

    profit = top_products(db, TODAY - timedelta(days=10), TODAY, "all", limit=10,
                          kind="profit")
    assert profit["count"] == 3
    assert [r["article"] for r in profit["rows"]][:2] == ["WB-1", "OZ"]

    loss = top_products(db, TODAY - timedelta(days=10), TODAY, "all", limit=10,
                        kind="loss")
    assert loss["rows"][0]["article"] == "OZ-XL"
    assert loss["rows"][0]["margin"] == -400.0


def test_top_products_ozon_grouped_by_size(db):
    """Ozon в топах дашборда свёрнут по товару: OZ-1 и OZ-L → база OZ."""
    db.add(models.Product(article="OZ-1", name="Оз", net_cost=300.0))
    db.add(_oz_row("o1", "OZ-1", TODAY))
    db.add(_oz_row("o2", "OZ-L", TODAY, price=400.0, income=100.0, amount=400.0))
    db.commit()

    p = top_products(db, TODAY - timedelta(days=10), TODAY, "ozon", limit=10,
                     kind="profit")
    assert p["count"] == 1
    oz = p["rows"][0]
    assert oz["article"] == "OZ"
    assert oz["sells"] == 2
    assert oz["sizes_count"] == 2
    assert oz["offers_count"] == 2
    # себестоимость базы не заведена → берётся у артикула размера (OZ-1 = 300)
    assert oz["margin"] == 950.0 - 300.0 * 2


def test_top_products_limit_offset(db):
    db.add(models.Product(article="WB-1", name="ВБ", net_cost=100.0))
    for i in range(3):
        db.add(_wb_row(f"w{i}", f"WB-{i}", TODAY, qty=1, amount=1000.0,
                       income=500.0))
    db.commit()
    p1 = top_products(db, TODAY, TODAY, "wb", limit=2, kind="profit")
    assert len(p1["rows"]) == 2
    assert p1["count"] == 3


# ------------------------------------------------------------------ группы
def test_prefix_margin_groups(db):
    db.add(models.Product(article="TIE-1", name="Галстук", net_cost=0.0))
    db.add(models.Product(article="JBG-1", name="Джинсы", net_cost=0.0))
    db.add(models.Product(article="ABC-1", name="Прочее", net_cost=0.0))
    db.add(_wb_row("w1", "TIE-1", TODAY, qty=1, amount=1000.0, income=900.0))
    db.add(_wb_row("w2", "JBG-1", TODAY, qty=2, amount=2000.0, income=1800.0))
    db.add(_wb_row("w3", "ABC-1", TODAY, qty=1, amount=500.0, income=400.0))
    db.commit()
    g = prefix_margin(db, TODAY, TODAY, "wb", limit=10)
    rec = {r["prefix"]: r for r in g["rows"]}
    assert set(rec.keys()) == {"TIE", "J", "остальные"}
    assert rec["J"]["sells"] == 2
    assert rec["J"]["revenue"] == 2000.0
    assert rec["J"]["income"] == 1800.0
    assert rec["J"]["margin_gross"] == 1800.0
    assert rec["TIE"]["margin_gross"] == 900.0
    assert rec["остальные"]["margin_gross"] == 400.0
    # первые по прибыли: J (1800) > TIE (900) > остальные (400)
    assert [r["prefix"] for r in g["rows"]] == ["J", "TIE", "остальные"]


# ------------------------------------------------------------------ дельты цены
def test_price_delta_up_and_down(db):
    cur_from = TODAY
    cur_to = TODAY
    prev_from = TODAY - timedelta(days=10)
    prev_to = TODAY - timedelta(days=10)

    # WB: подорожал (1000 -> 1200), подешевел (1000 -> 800)
    db.add(_wb_row("c1", "UP-1", cur_from, qty=2, amount=2400.0, income=2000.0))
    db.add(_wb_row("c2", "DOWN-1", cur_from, qty=2, amount=1600.0, income=1400.0))
    db.add(_wb_row("p1", "UP-1", prev_from, qty=2, amount=2000.0, income=1800.0))
    db.add(_wb_row("p2", "DOWN-1", prev_from, qty=2, amount=2000.0, income=1800.0))
    # Ozon: подешевел (1000 -> 500)
    db.add(_oz_row("o1", "OZ-DN", cur_from, qty=1, price=500.0, income=400.0, amount=500.0))
    db.add(_oz_row("o2", "OZ-DN", prev_from, qty=1, price=1000.0, income=850.0, amount=1000.0))
    db.commit()

    res = price_delta(db, cur_from, cur_to, (prev_from, prev_to), "all", limit=10)
    up = {r["article"]: r for r in res["up"]["rows"]}
    down = {r["article"]: r for r in res["down"]["rows"]}
    assert up["UP-1"]["delta_pct"] == 20.0      # 1200/1000 - 1
    assert round(up["UP-1"]["avg_prev"], 2) == 1000.0
    assert down["DOWN-1"]["delta_pct"] == -20.0
    assert down["OZ-DN"]["delta_pct"] == -50.0
    # топ «снижение» — самый сильный спад (OZ-DN, -50%) первый
    assert res["down"]["rows"][0]["article"] == "OZ-DN"


def test_price_delta_requires_prev_window(db):
    db.add(_wb_row("c1", "UP-1", TODAY, qty=2, amount=2400.0, income=2000.0))
    db.commit()
    res = price_delta(db, TODAY, TODAY, None, "wb", limit=10)
    assert res["up"]["rows"] == []
    assert res["down"]["rows"] == []


# --------------------------------------- полные списки (limit=None → всё)
def test_top_products_full_list_without_limit(db):
    db.add(models.Product(article="WB-1", name="ВБ", net_cost=100.0))
    for i in range(5):
        db.add(_wb_row(f"w{i}", f"WB-{i}", TODAY, qty=1, amount=1000.0,
                       income=500.0))
    db.commit()
    p = top_products(db, TODAY, TODAY, "wb", kind="profit")
    assert p["count"] == 5
    assert len(p["rows"]) == 5
    # лимитированный запрос по-прежнему режет
    assert len(top_products(db, TODAY, TODAY, "wb", limit=2)["rows"]) == 2


def test_prefix_margin_full_list_without_limit(db):
    db.add(models.Product(article="TIE-1", name="Галстук", net_cost=0.0))
    db.add(models.Product(article="JBG-1", name="Джинсы", net_cost=0.0))
    db.add(_wb_row("w1", "TIE-1", TODAY, qty=1, amount=1000.0, income=900.0))
    db.add(_wb_row("w2", "JBG-1", TODAY, qty=2, amount=2000.0, income=1800.0))
    db.commit()
    g = prefix_margin(db, TODAY, TODAY, "wb")
    assert len(g["rows"]) == g["count"] == 2


def test_price_delta_full_lists_without_limit(db):
    db.add(_wb_row("c1", "UP-1", TODAY, qty=2, amount=2400.0, income=2000.0))
    db.add(_wb_row("c2", "DOWN-1", TODAY, qty=2, amount=1600.0, income=1400.0))
    db.add(_wb_row("p1", "UP-1", TODAY - timedelta(days=10), qty=2,
                   amount=2000.0, income=1800.0))
    db.add(_wb_row("p2", "DOWN-1", TODAY - timedelta(days=10), qty=2,
                   amount=2000.0, income=1800.0))
    db.commit()
    res = price_delta(db, TODAY, TODAY, (TODAY - timedelta(days=14),
                                         TODAY - timedelta(days=7)), "wb")
    assert len(res["up"]["rows"]) == res["up"]["count"] == 2
    assert len(res["down"]["rows"]) == res["down"]["count"] == 2


# ------------------------------------------------------------------ склад и свежесть
def test_stocks_summary(db):
    wb = db.execute(
        select(models.Marketplace.id).where(models.Marketplace.code == "wb")
    ).scalar_one()
    oz = db.execute(
        select(models.Marketplace.id).where(models.Marketplace.code == "ozon")
    ).scalar_one()
    db.add(models.Product(article="WB-1", name="ВБ", net_cost=100.0))
    db.add(models.Product(article="OZ-1", name="Оз", net_cost=0.0))
    db.add(models.Stock(marketplace_id=wb, date=TODAY, article="WB-1",
                        warehouse="МСК", chrt_id="", quantity=5, quantity_full=5,
                        in_way=2))
    db.add(models.Stock(marketplace_id=oz, date=TODAY, article="OZ-1",
                        warehouse="Все", chrt_id="", quantity=3, quantity_full=3,
                        in_way=0))
    db.commit()

    s = stocks_summary(db, TODAY, "all")
    assert s["wb"]["quantity"] == 5
    assert s["wb"]["quantity_full"] == 5
    assert s["wb"]["in_way"] == 2
    assert s["wb"]["value"] == 500.0            # 5 * 100
    assert s["wb"]["value_unknown"] == 0
    assert s["ozon"]["quantity_full"] == 3
    assert s["ozon"]["value_unknown"] == 1      # нет себестоимости
    assert s["ozon"]["date"] == str(TODAY)


def test_stocks_summary_uses_last_snapshot(db):
    wb = db.execute(
        select(models.Marketplace.id).where(models.Marketplace.code == "wb")
    ).scalar_one()
    db.add(models.Product(article="WB-1", name="ВБ", net_cost=10.0))
    db.add(models.Stock(marketplace_id=wb, date=TODAY - timedelta(days=5),
                        article="WB-1", warehouse="Все", chrt_id="",
                        quantity=8, quantity_full=8, in_way=1))
    db.add(models.Stock(marketplace_id=wb, date=TODAY, article="WB-1",
                        warehouse="Все", chrt_id="", quantity=2, quantity_full=2,
                        in_way=0))
    db.commit()
    s = stocks_summary(db, TODAY, "wb")
    assert s["wb"]["quantity_full"] == 2        # срез ровно на date_to
    assert s["wb"]["value"] == 20.0


def test_freshness_lists_pulls(db):
    db.add(models.ApiPull(api="wb", kind="detail", last_success_at=TODAY,
                          rows=10, db_rows=8, window="сегодня"))
    db.add(models.ApiPull(api="ozon", kind="cards", last_success_at=date(2026, 9, 1),
                          rows=2, db_rows=2, window=""))
    db.commit()
    out = freshness(db)
    assert len(out) == 2
    assert out[0]["api"] == "wb"                # новейшая первой
    assert out[0]["kind"] == "detail"
    assert out[1]["kind"] == "cards"