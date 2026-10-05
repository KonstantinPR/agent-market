"""Тесты сервиса «Потребность в товаре» (app/services/replenish.py)."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app import models
from app.services.replenish import WB_SORT_VELOCITY_DAYS, replenish_rows, wb_sorting_plan

TODAY = date(2026, 9, 20)
D0 = TODAY - timedelta(days=29)  # окно 30 дней


def _mp_id(db, code):
    return db.execute(
        select(models.Marketplace.id).where(models.Marketplace.code == code)
    ).scalar_one()


def _wb_row(op_key, article, day, qty, amount=1000.0, income=850.0,
            doc_type="Продажа"):
    return models.WbDetailRow(
        op_key=op_key, source="excel", article=article, doc_type_name=doc_type,
        sale_dt=day, quantity=qty, retail_amount=amount, for_pay=income,
    )


def _oz_row(op_key, article, day, qty=1, ret=0, income=850.0):
    return models.OzonDetailRow(
        op_key=op_key, source="api", date=day, posting_number="p" + op_key,
        offer_id=article, name="Товар " + article, sku="", barcode="",
        quantity=qty, seller_price=1000.0, amount=1000.0 * qty,
        commission_ratio=0.0, commission=-100.0, standard_fee=-50.0,
        income=income, return_qty=ret, return_total=0.0,
    )


def _card(db, code, vendor="", barcode=""):
    db.add(models.MarketplaceCard(
        marketplace_id=_mp_id(db, code), chrt_id="c:" + vendor + barcode,
        vendor_code=vendor, nm_id="", barcode=barcode,
    ))


def _stock(db, code, article, date_, qty=0, qf=0, way=0):
    db.add(models.Stock(
        marketplace_id=_mp_id(db, code), date=date_, article=article,
        warehouse="Стек", quantity=qty, quantity_full=qf, in_way=way,
    ))


# ------------------------------------------------------------------ расчёт
def test_replenish_wb_ship_and_buy(db):
    """WB продажи без возвратов: отгрузка со склада + докупка до целевого."""
    db.add(models.Product(article="WB-1", name="ВБ", net_cost=100.0, replenishable=True))
    db.add(models.CustomStock(article="WB-1", quantity=50, net_cost=100))
    _card(db, "wb", vendor="WB-1")
    db.add(_wb_row("w1", "WB-1", TODAY, qty=60, income=54000.0,
                   amount=60000.0))
    db.commit()

    r = replenish_rows(db, D0, TODAY, target_days=30, span_days=30)
    assert r["meta"]["count"] == 1
    row = r["rows"][0]
    assert row["article"] == "WB-1"
    assert row["demand"] == 2.0            # 60/30, без возвратов
    assert row["demand_wb"] == 2.0
    assert row["demand_oz"] == 0.0
    assert row["actual"] is True
    assert row["actual_mp"] == "wb"
    assert row["our_stock"] == 50
    assert row["wb_def"] == 60
    assert row["ship_wb"] == 50            # покрываем дефицит WB из нашего склада
    assert row["ship_oz"] == 0
    assert row["need_buy"] == 10           # 30 дн × 2/дн − (50 + 0 + 0)
    assert row["status"] == "urgent"
    assert r["meta"]["need_total"] == 10
    assert r["meta"]["ship_total"] == 50


def test_replenish_returns_net_demand(db):
    """Возвраты вычитаются: WB — уже в sells, Ozon — отдельным полем."""
    db.add(models.Product(article="NET-1", name="Нетто", net_cost=100.0))
    _card(db, "wb", vendor="NET-1")
    _card(db, "ozon", vendor="NET-1")
    db.add(_wb_row("n1", "NET-1", TODAY, qty=100, income=85000.0,
                   amount=100000.0))
    db.add(_wb_row("n2", "NET-1", TODAY, qty=40, income=-34000.0,
                   amount=-40000.0, doc_type="Возврат"))
    db.add(_oz_row("o1", "NET-1", TODAY, qty=90, ret=30))
    db.commit()

    r = replenish_rows(db, D0, TODAY, target_days=30, span_days=30)
    row = r["rows"][0]
    assert row["demand_wb"] == 2.0          # (100 − 40)/30
    assert row["demand_oz"] == 2.0          # (90 − 30)/30
    assert row["demand"] == 4.0
    assert row["return_rate"] == round(70 / 220 * 100, 1)
    assert row["need_buy"] == 120           # 30 × 4, остатков нет нигде


def test_replenish_nostock_and_urgency_ordering(db):
    """Нет остатков нигде → nostock, идёт первым в сортировке по срочности."""
    db.add(models.Product(article="NS-1", name="Нет склада", net_cost=100.0))
    db.add(models.Product(article="OK-1", name="Норма", net_cost=50.0))
    _card(db, "wb", vendor="NS-1")
    _card(db, "ozon", vendor="OK-1")
    db.add(_wb_row("a1", "NS-1", TODAY, qty=30, income=27000.0, amount=30000.0))
    db.add(_wb_row("a2", "OK-1", TODAY, qty=30, income=27000.0, amount=30000.0))
    _stock(db, "ozon", "OK-1", TODAY, qty=40, qf=37, way=3)
    db.commit()

    r = replenish_rows(db, D0, TODAY, target_days=30, span_days=30)
    assert r["rows"][0]["article"] == "NS-1"
    assert r["rows"][0]["status"] == "nostock"
    assert r["rows"][0]["need_buy"] == 30    # 30 дн × 1/дн
    ok = {x["article"]: x for x in r["rows"]}["OK-1"]
    assert ok["status"] == "normal"          # Ozon-склад 40 ≥ 30 дней запаса
    assert ok["oz_avail"] == 40
    assert ok["need_buy"] == 0
    assert ok["ship_oz"] == 0
    assert r["meta"]["nostock"] == 1
    assert r["meta"]["urgent"] == 0


def test_replenish_inactive_hidden_by_default(db):
    """Без карточки на WB/Ozon — неактуальный, из выдачи скрыт."""
    db.add(models.Product(article="GHOST", name="Призрак", net_cost=100.0))
    db.add(_wb_row("g1", "GHOST", TODAY, qty=60, income=54000.0, amount=60000.0))
    db.commit()

    r = replenish_rows(db, D0, TODAY, target_days=30, span_days=30)
    assert r["rows"] == []
    assert r["meta"]["inactive_total"] == 1

    r2 = replenish_rows(db, D0, TODAY, target_days=30, span_days=30,
                        show_inactive=True)
    assert len(r2["rows"]) == 1
    assert r2["rows"][0]["status"] == "inactive"
    assert r2["rows"][0]["actual"] is False


def test_replenish_barcode_match_actual(db):
    """Актуальность и по баркоду карточки (артикул не совпадает)."""
    db.add(models.Product(article="BAR-1", name="По баркоду",
                          barcode="ABC123456789", net_cost=100.0))
    _card(db, "ozon", vendor="", barcode="ABC123456789")
    db.add(_oz_row("z1", "BAR-1", TODAY, qty=60))
    db.commit()

    r = replenish_rows(db, D0, TODAY, target_days=30, span_days=30)
    row = r["rows"][0]
    assert row["actual"] is True
    assert row["actual_mp"] == "ozon"
    assert row["demand_oz"] == 2.0


def test_replenish_sort_by_margin(db):
    db.add(models.Product(article="M1", name="Дорого", net_cost=100.0))
    db.add(models.Product(article="M2", name="Дёшево", net_cost=400.0))
    _card(db, "wb", vendor="M1")
    _card(db, "wb", vendor="M2")
    db.add(_wb_row("m1", "M1", TODAY, qty=30, income=60000.0, amount=60000.0))
    db.add(_wb_row("m2", "M2", TODAY, qty=30, income=60000.0, amount=60000.0))
    db.commit()

    by_margin = replenish_rows(db, D0, TODAY, target_days=30, span_days=30,
                               sort="margin")
    arts = [x["article"] for x in by_margin["rows"]]
    assert arts[0] == "M1"                   # маржа/шт выше


def test_replenish_marketplace_filter_and_article_like(db):
    db.add(models.Product(article="TIE-1", name="Галстук", net_cost=100.0))
    db.add(models.Product(article="SH-9", name="Штора", net_cost=100.0))
    _card(db, "wb", vendor="TIE-1")
    _card(db, "ozon", vendor="SH-9")
    db.add(_wb_row("t1", "TIE-1", TODAY, qty=30, income=27000.0, amount=30000.0))
    db.add(_oz_row("s1", "SH-9", TODAY, qty=30))
    db.commit()

    only_oz = replenish_rows(db, D0, TODAY, target_days=30, span_days=30,
                             marketplace="ozon")
    assert [x["article"] for x in only_oz["rows"]] == ["SH-9"]
    like = replenish_rows(db, D0, TODAY, target_days=30, span_days=30,
                          article_like="tie")
    assert [x["article"] for x in like["rows"]] == ["TIE-1"]


# ------------------------------------------------------------------ размеры
def _wb_row_size(op_key, article, day, qty, tech_size="", sku="",
                 amount=1000.0, income=850.0, doc_type="Продажа"):
    return models.WbDetailRow(
        op_key=op_key, source="excel", article=article, doc_type_name=doc_type,
        sale_dt=day, quantity=qty, retail_amount=amount, for_pay=income,
        tech_size=tech_size, sku=sku,
    )


def _stock_size(db, code, article, date_, qty=0, qf=0, way=0, size="", barcode=""):
    db.add(models.Stock(
        marketplace_id=_mp_id(db, code), date=date_, article=article,
        warehouse="Стек", quantity=qty, quantity_full=qf, in_way=way,
        size=size, barcode=barcode,
    ))


def _psize(db, article, size, barcode):
    db.add(models.ProductSize(article=article, size=size, barcode=barcode))


def test_size_view_splits_by_size(db):
    """Разрез по размерам: WB-спрос, остатки и отгрузка со склада — по размеру."""
    db.add(models.Product(article="SZ", name="Свитшот", net_cost=100.0))
    _card(db, "wb", vendor="SZ")
    db.add(models.CustomStock(article="SZ", quantity=100, net_cost=100))
    _psize(db, "SZ", "S", "BCODE-S")
    _psize(db, "SZ", "L", "BCODE-L")
    db.add(_wb_row_size("s1", "SZ", TODAY, qty=60, tech_size="S",
                        income=54000.0, amount=60000.0))
    db.add(_wb_row_size("s2", "SZ", TODAY, qty=30, tech_size="L",
                        income=27000.0, amount=30000.0))
    _stock_size(db, "wb", "SZ", TODAY, qty=10, qf=10, size="S", barcode="BCODE-S")
    db.commit()

    r = replenish_rows(db, D0, TODAY, target_days=30, span_days=30, view="sizes")
    assert r["meta"]["unit"] == "sizes"
    assert r["meta"]["count"] == 2
    by_size = {x["size"]: x for x in r["rows"]}
    s = by_size["S"]
    assert s["article"] == "SZ"
    assert s["barcode"] == "BCODE-S"
    assert s["wb_vel"] == 2.0
    assert s["wb_sells"] == 60
    assert s["wb_avail"] == 10
    assert s["wb_def"] == 50            # 30 дн × 2 − остаток 10
    assert s["ship_wb"] == 50
    l = by_size["L"]
    assert l["barcode"] == "BCODE-L"
    assert l["wb_vel"] == 1.0
    assert l["wb_avail"] == 0
    assert l["wb_def"] == 30            # остатков по L нет
    assert l["ship_wb"] == 30           # приоритет отгрузки — худшему запасу в днях (L)
    # плоский разрез: «у нас» — по всему артикулу на каждой строке
    assert s["our_stock"] == 100 and l["our_stock"] == 100
    assert r["meta"]["ship_total"] == 80
    assert r["meta"]["need_total"] == 0  # Ozon/докупка по размерам не считаются


def test_size_view_barcode_from_wb_and_stock(db):
    """Баркод размера из отчёта WB (sku) и из остатков склада, без каталога."""
    db.add(models.Product(article="SK", name="Без каталога", net_cost=100.0))
    _card(db, "wb", vendor="SK")
    db.add(_wb_row_size("k1", "SK", TODAY, qty=30, tech_size="M", sku="SKU-M",
                        income=27000.0, amount=30000.0))
    db.add(_wb_row_size("k2", "SK", TODAY, qty=30, tech_size="XL", sku="SKU-XL",
                        income=27000.0, amount=30000.0))
    _stock_size(db, "wb", "SK", TODAY, qty=5, qf=5, size="M", barcode="BAR-M")
    db.commit()

    r = replenish_rows(db, D0, TODAY, target_days=30, span_days=30, view="sizes")
    by_size = {x["size"]: x for x in r["rows"]}
    assert by_size["M"]["barcode"] == "SKU-M"   # sku отчёта имеет приоритет
    assert by_size["XL"]["barcode"] == "SKU-XL"


def test_size_view_nostock_inactive_and_filter(db):
    """nostock по размеру без остатков; неактуальные скрыты; фильтр по артикулу."""
    db.add(models.Product(article="EMPTY", name="Пусто", net_cost=100.0))
    db.add(models.Product(article="GHOST", name="Призрак", net_cost=100.0))
    _card(db, "wb", vendor="EMPTY")
    _psize(db, "EMPTY", "S", "B-E")
    _psize(db, "EMPTY", "L", "B-E2")
    _psize(db, "GHOST", "S", "B-G")
    db.add(_wb_row_size("e1", "EMPTY", TODAY, qty=30, tech_size="S",
                        income=27000.0, amount=30000.0))
    db.add(_wb_row_size("e2", "EMPTY", TODAY, qty=30, tech_size="L",
                        income=27000.0, amount=30000.0))
    db.add(_wb_row_size("g1", "GHOST", TODAY, qty=30, tech_size="S",
                        income=27000.0, amount=30000.0))
    db.commit()

    r = replenish_rows(db, D0, TODAY, target_days=30, span_days=30, view="sizes")
    # GHOST скрыт (нет карточки), его 1 размер ушёл в inactive_total
    assert r["meta"]["count"] == 2
    assert r["meta"]["inactive_total"] == 1
    assert all(x["article"] == "EMPTY" for x in r["rows"])
    by_size = {x["size"]: x for x in r["rows"]}
    assert by_size["S"]["status"] == "nostock"
    assert by_size["L"]["status"] == "nostock"
    assert r["meta"]["nostock"] == 2

    r2 = replenish_rows(db, D0, TODAY, target_days=30, span_days=30,
                        view="sizes", article_like="ghost", show_inactive=True)
    assert [x["article"] for x in r2["rows"]] == ["GHOST"]
    assert r2["rows"][0]["status"] == "inactive"


def test_api_replenish_size_view(db, api_client):
    db.add(models.Product(article="SZ", name="Свитшот", net_cost=100.0))
    _card(db, "wb", vendor="SZ")
    _psize(db, "SZ", "S", "BCODE-S")
    _psize(db, "SZ", "L", "BCODE-L")
    db.add(_wb_row_size("s1", "SZ", TODAY, qty=60, tech_size="S",
                        income=54000.0, amount=60000.0))
    db.add(_wb_row_size("s2", "SZ", TODAY, qty=30, tech_size="L",
                        income=27000.0, amount=30000.0))
    _stock_size(db, "wb", "SZ", TODAY, qty=10, qf=10, size="S", barcode="BCODE-S")
    db.commit()

    resp = api_client.get("/api/replenish", params={
        "date_from": D0.isoformat(), "date_to": TODAY.isoformat(),
        "target_days": 30, "window_days": 30, "view": "sizes",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["meta"]["unit"] == "sizes"
    sizes = [x["size"] for x in data["rows"]]
    assert sizes == ["L", "S"]           # L без остатка — первым по срочности
    assert data["rows"][0]["barcode"] == "BCODE-L"

    xp = api_client.get("/api/export/replenish", params={
        "date_from": D0.isoformat(), "date_to": TODAY.isoformat(), "view": "sizes",
    })
    assert xp.status_code == 200
    assert xp.content.startswith(b"PK")


# ------------------------------------------------------------------ API
def test_api_replenish(db, api_client):
    db.add(models.Product(article="WB-1", name="ВБ", net_cost=100.0))
    db.add(models.CustomStock(article="WB-1", quantity=50, net_cost=100))
    _card(db, "wb", vendor="WB-1")
    db.add(_wb_row("w1", "WB-1", TODAY, qty=60, income=54000.0, amount=60000.0))
    db.commit()

    resp = api_client.get("/api/replenish", params={
        "date_from": D0.isoformat(), "date_to": TODAY.isoformat(),
        "target_days": 30, "window_days": 30,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["date_from"] == D0.isoformat()
    assert data["rows"][0]["article"] == "WB-1"
    assert data["rows"][0]["need_buy"] == 10
    assert data["meta"]["count"] == 1


def test_api_replenish_export(db, api_client):
    db.add(models.Product(article="WB-1", name="ВБ", net_cost=100.0))
    db.add(models.CustomStock(article="WB-1", quantity=50, net_cost=100))
    _card(db, "wb", vendor="WB-1")
    db.add(_wb_row("w1", "WB-1", TODAY, qty=60, income=54000.0, amount=60000.0))
    db.commit()

    resp = api_client.get("/api/export/replenish", params={
        "date_from": D0.isoformat(), "date_to": TODAY.isoformat(),
    })
    assert resp.status_code == 200
    assert resp.content.startswith(b"PK")
    assert resp.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


# ------------------------------------------------- план подсортировки на WB
def _card_size(db, code, vendor, size, barcode=""):
    db.add(models.MarketplaceCard(
        marketplace_id=_mp_id(db, code), chrt_id=f"c:{vendor}:{size}",
        vendor_code=vendor, nm_id="", barcode=barcode, size=size,
    ))


def _wb_size_row(op_key, article, size, day, qty, doc_type="Продажа"):
    return models.WbDetailRow(
        op_key=op_key, source="excel", article=article, doc_type_name=doc_type,
        sale_dt=day, quantity=qty, retail_amount=1000.0, for_pay=850.0,
        tech_size=size, sku="",
    )


def _plan_size(plan, article, size):
    items = {s["size"]: s for s in plan[article]["sizes"]}
    return items[size]


def test_plan_lists_every_card_size_even_without_sales(db):
    """Главная боль: размеры без продаж в окне тоже должны попасть в план."""
    _card_size(db, "wb", "PLAN-1", "42", "111")
    _card_size(db, "wb", "PLAN-1", "44", "222")
    _card_size(db, "wb", "PLAN-1", "46", "333")
    db.add(_wb_size_row("p1", "PLAN-1", "42", TODAY, qty=180))
    db.commit()

    plan = wb_sorting_plan(db, TODAY, target_days=30)
    assert [s["size"] for s in plan["PLAN-1"]["sizes"]] == ["42", "44", "46"]
    assert _plan_size(plan, "PLAN-1", "42")["to_sort"] > 0
    # у 44 и 46 продаж нет вообще — остаются в плане, но добавлять нечего
    assert _plan_size(plan, "PLAN-1", "44")["to_sort"] == 0
    assert _plan_size(plan, "PLAN-1", "46")["to_sort"] == 0
    assert _plan_size(plan, "PLAN-1", "42")["barcode"] == "111"


def test_plan_velocity_spans_long_window(db):
    """Продажа 60 дней назад обязана учитываться: окно скорости — 180 дней."""
    _card_size(db, "wb", "PLAN-2", "42")
    db.add(_wb_size_row("p2", "PLAN-2", "42", TODAY - timedelta(days=59), qty=180))
    db.commit()

    plan = wb_sorting_plan(db, TODAY, target_days=30)
    s = _plan_size(plan, "PLAN-2", "42")
    assert s["net"] == 180
    assert s["vel"] == pytest.approx(1.0, abs=0.01)
    assert s["to_sort"] == 30                    # 30 дн × 1 шт/дн


def test_plan_ignores_sales_older_than_velocity_window(db):
    _card_size(db, "wb", "PLAN-3", "42")
    db.add(_wb_size_row("p3", "PLAN-3", "42",
                        TODAY - timedelta(days=WB_SORT_VELOCITY_DAYS + 10), qty=500))
    db.commit()

    plan = wb_sorting_plan(db, TODAY, target_days=30)
    assert _plan_size(plan, "PLAN-3", "42")["to_sort"] == 0


def test_plan_subtracts_marketplace_stock_and_in_way(db):
    """Остаток WB и наш товар в пути уменьшают подсортировку."""
    _card_size(db, "wb", "PLAN-4", "42")
    db.add(_wb_size_row("p4", "PLAN-4", "42", TODAY, qty=180))
    db.add(models.Stock(
        marketplace_id=_mp_id(db, "wb"), date=TODAY, article="PLAN-4", size="42",
        warehouse="Стек", quantity=0, quantity_full=5, in_way=4,
    ))
    db.commit()

    plan = wb_sorting_plan(db, TODAY, target_days=30)
    s = _plan_size(plan, "PLAN-4", "42")
    assert s["avail"] == 9                        # 5 на складе WB + 4 в пути
    assert s["to_sort"] == 21                     # 30 − 9


def test_plan_never_negative_on_returns_only_size(db):
    """Возвраты могут дать отрицательное нетто — план всё равно не отрицательный."""
    _card_size(db, "wb", "PLAN-5", "42")
    # возврат в WB приходит положительным количеством и вычитается из нетто
    db.add(_wb_size_row("p5", "PLAN-5", "42", TODAY, qty=1, doc_type="Возврат"))
    db.commit()

    plan = wb_sorting_plan(db, TODAY, target_days=30)
    s = _plan_size(plan, "PLAN-5", "42")
    assert s["net"] == 0 or s["net"] >= 0
    assert s["to_sort"] == 0
    assert plan["PLAN-5"]["total"] >= 0


def test_plan_total_is_sum_and_filtered_by_articles(db):
    _card_size(db, "wb", "PLAN-6", "42")
    _card_size(db, "wb", "PLAN-7", "42")
    db.add(_wb_size_row("p6", "PLAN-6", "42", TODAY, qty=180))
    db.add(_wb_size_row("p7", "PLAN-7", "42", TODAY, qty=90))
    db.commit()

    plan = wb_sorting_plan(db, TODAY, target_days=30, articles=["PLAN-6"])
    assert set(plan) == {"PLAN-6"}
    assert plan["PLAN-6"]["total"] == sum(
        s["to_sort"] for s in plan["PLAN-6"]["sizes"])


def test_plan_keeps_sold_size_missing_from_card(db):
    """Размер вне карточек не теряем: он приносил продажи, значит важен."""
    db.add(_wb_size_row("p8", "PLAN-8", "42", TODAY, qty=180))
    db.commit()

    plan = wb_sorting_plan(db, TODAY, target_days=30)
    assert _plan_size(plan, "PLAN-8", "42")["to_sort"] == 30


def test_plan_sizes_are_sorted_naturally(db):
    """9 должно идти раньше 10, а не после."""
    for sz in ("10", "9", "42", "2XL"):
        _card_size(db, "wb", "PLAN-9", sz)
    db.commit()

    plan = wb_sorting_plan(db, TODAY, target_days=30)
    got = [s["size"] for s in plan["PLAN-9"]["sizes"]]
    assert got == ["2XL", "9", "10", "42"]


def test_plan_ignores_ozon_cards(db):
    """Пока считаем только WB — карточки Ozon не должны попадать в план."""
    _card_size(db, "ozon", "PLAN-10", "42")
    db.add(_wb_size_row("p9", "PLAN-10", "42", TODAY, qty=180))
    db.commit()

    plan = wb_sorting_plan(db, TODAY, target_days=30, articles=["PLAN-10"])
    sizes = plan["PLAN-10"]["sizes"]
    assert [s["size"] for s in sizes] == ["42"]   # только из продаж WB
    assert _plan_size(plan, "PLAN-10", "42")["to_sort"] == 30
