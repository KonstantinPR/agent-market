"""Тесты сервиса «Потребность в товаре» (app/services/replenish.py)."""
import io
from datetime import date, timedelta

import pytest
from openpyxl import load_workbook
from sqlalchemy import select

from app import models
from app.services.replenish import (
    WB_SORT_VELOCITY_DAYS, apply_sort_budget, profit_factor, replenish_rows,
    wb_profit_factors, wb_sorting_plan,
)

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


def _card(db, code, vendor="", barcode="", size=""):
    db.add(models.MarketplaceCard(
        marketplace_id=_mp_id(db, code),
        chrt_id=f"c:{vendor}:{size}{barcode}",
        vendor_code=vendor, nm_id="", barcode=barcode, size=size,
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
    assert row["wb_sells"] == 60           # продано WB за окно, шт
    assert row["demand_oz"] == 0.0
    assert row["actual"] is True
    assert row["actual_mp"] == "wb"
    assert row["our_stock"] == 50
    # «Дослать»/«WB дефицит» — из плана подсортировки как PDF: скорость 180 дн
    # (60 × 2.0 коэффициента / 180 дн × 30), не из окна спроса 30 дн.
    assert row["to_sort"] == 20
    assert row["wb_def"] == 20
    assert row["ship_wb"] == 20            # покрываем плановый дефицит WB из нашего склада
    assert row["ship_oz"] == 0
    assert row["need_buy"] == 10           # 30 дн × 2/дн − (50 + 0 + 0)
    assert row["status"] == "urgent"
    assert r["meta"]["need_total"] == 10
    assert r["meta"]["ship_total"] == 20


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
    assert row["wb_sells"] == 60           # нетто WB: 100 продаж − 40 возвратов
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


def test_replenish_empty_wb_card_gets_one_to_sort(db):
    """Главная боль: пустая карточка WB без продаж в окне не должна давать 0.

    «Дослать»/«WB дефицит» из плана подсортировки (как PDF) добавляет 1 шт в
    пустой размер карточки, и «Отгрузить» покрывает её с нашего склада.
    Раньше в окне без WB-продаж дефицит был 0, хотя размер на карточке пуст.
    """
    db.add(models.Product(article="DRY-1", name="Пусто на WB", net_cost=100.0))
    db.add(models.CustomStock(article="DRY-1", quantity=5, net_cost=100))
    _card(db, "wb", vendor="DRY-1", size="42")
    db.commit()

    r = replenish_rows(db, D0, TODAY, target_days=30, span_days=30)
    row = r["rows"][0]
    assert row["wb_sells"] == 0
    assert row["wb_avail"] == 0
    assert row["to_sort"] == 1
    assert row["wb_def"] == 1
    assert row["ship_wb"] == 1          # пол покрывается с нашего склада
    assert row["ship_oz"] == 0
    assert row["need_buy"] == 0
    assert row["status"] == "normal"


def test_replenish_to_sort_matches_sorting_plan(db):
    """«Дослать» в таблице — ровно итог плана подсортировки, что печатает PDF."""
    db.add(models.Product(article="MCH-1", name="Совпадение", net_cost=50.0))
    _card(db, "wb", vendor="MCH-1", size="42")
    db.add(_wb_row("mch1", "MCH-1", TODAY, qty=180, income=170000.0,
                   amount=180000.0))
    db.commit()

    r = replenish_rows(db, D0, TODAY, target_days=30, span_days=30)
    row = r["rows"][0]
    # фильтр по уникальному артикулу — план строится по тем же данным ключей
    plan = wb_sorting_plan(
        db, TODAY, target_days=30, articles=["MCH-1"],
        profit_factor=wb_profit_factors(db, TODAY, articles=["MCH-1"]),
    )
    assert plan["MCH-1"]["total"] > 0
    assert row["to_sort"] == plan["MCH-1"]["total"]
    assert row["to_sort"] == row["wb_def"]


def test_to_sort_zero_when_wb_excluded(db):
    """Фильтр «только Ozon»: WB-плана нет, «Дослать» пусто, дефицит Ozon свой."""
    db.add(models.Product(article="OZ-ONLY", name="Только Ozon", net_cost=100.0))
    _card(db, "ozon", vendor="OZ-ONLY")
    db.add(_oz_row("zo1", "OZ-ONLY", TODAY, qty=30))
    db.commit()

    r = replenish_rows(db, D0, TODAY, target_days=30, span_days=30,
                       marketplace="ozon")
    assert len(r["rows"]) == 1
    row = r["rows"][0]
    assert row["to_sort"] == 0
    assert row["wb_def"] == 0
    assert row["oz_def"] == 30


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
    _card(db, "wb", vendor="SZ", size="S")
    _card(db, "wb", vendor="SZ", size="L")
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
    # «Дослать»/«WB дефицит» — из плана подсортировки (180 дн, ×2 прибыльность):
    # S: 20 − остаток 10 = 10; L: 20 − 0 = 10.
    assert s["wb_def"] == 10
    assert s["to_sort"] == 10
    assert s["ship_wb"] == 10
    sz_l = by_size["L"]
    assert sz_l["barcode"] == "BCODE-L"
    assert sz_l["wb_vel"] == 1.0
    assert sz_l["wb_avail"] == 0
    assert sz_l["wb_def"] == 10
    assert sz_l["to_sort"] == 10
    assert sz_l["ship_wb"] == 10
    # плоский разрез: «у нас» — по всему артикулу на каждой строке
    assert s["our_stock"] == 100 and sz_l["our_stock"] == 100
    assert r["meta"]["ship_total"] == 20
    assert r["meta"]["need_total"] == 0  # Ozon/докупка по размерам не считаются


def test_size_view_barcode_from_wb_and_stock(db):
    """Баркод размера из отчёта WB (sku) и из остатков склада, без каталога."""
    db.add(models.Product(article="SK", name="Без каталога", net_cost=100.0))
    _card(db, "wb", vendor="SK", size="M")
    _card(db, "wb", vendor="SK", size="XL")
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
    _card(db, "wb", vendor="EMPTY", size="S")
    _card(db, "wb", vendor="EMPTY", size="L")
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


def test_size_view_includes_card_sizes_without_sales_or_stock(db):
    """Размеры карточки WB без продаж и остатков видны в разрезе по умолчанию."""
    db.add(models.Product(article="ZERO", name="Нулевые", net_cost=100.0))
    _card(db, "wb", vendor="ZERO", size="S", barcode="B-S")
    _card(db, "wb", vendor="ZERO", size="L", barcode="B-L")
    db.add(_wb_row_size("z1", "ZERO", TODAY, qty=60, tech_size="S",
                        income=54000.0, amount=60000.0))
    db.commit()

    r = replenish_rows(db, D0, TODAY, target_days=30, span_days=30, view="sizes")
    assert r["meta"]["count"] == 2
    by_size = {x["size"]: x for x in r["rows"]}
    assert set(by_size) == {"S", "L"}
    sz_l = by_size["L"]
    assert sz_l["wb_sells"] == 0
    assert sz_l["wb_vel"] == 0.0
    assert sz_l["wb_avail"] == 0
    assert sz_l["barcode"] == "B-L"           # баркод мёртвого размера — из карточки
    assert sz_l["status"] == "normal"

    r2 = replenish_rows(db, D0, TODAY, target_days=30, span_days=30,
                        view="sizes", hide_zero_sizes=True)
    assert [x["size"] for x in r2["rows"]] == ["S"]
    assert r2["meta"]["count"] == 1


def test_size_view_floor_gives_one_to_empty_card_size(db):
    """Пустой размер карточки получает «Дослать» 1 (правило пола из PDF)."""
    db.add(models.Product(article="FLR-1", name="Пол", net_cost=100.0))
    db.add(models.CustomStock(article="FLR-1", quantity=3, net_cost=100))
    _card(db, "wb", vendor="FLR-1", size="S", barcode="B-FS")
    _card(db, "wb", vendor="FLR-1", size="L", barcode="B-FL")
    db.commit()

    r = replenish_rows(db, D0, TODAY, target_days=30, span_days=30, view="sizes")
    by_size = {x["size"]: x for x in r["rows"]}
    assert set(by_size) == {"S", "L"}
    for s in ("S", "L"):
        assert by_size[s]["wb_sells"] == 0
        assert by_size[s]["wb_avail"] == 0
        assert by_size[s]["to_sort"] == 1
        assert by_size[s]["wb_def"] == 1
        assert by_size[s]["ship_wb"] == 1   # пол обоих размеров покрыт (склад 3)


def test_size_view_hide_zero_keeps_sizes_with_stock(db):
    """hide_zero_sizes убирает «везде 0», но держит размер с остатком на складе."""
    db.add(models.Product(article="STK", name="С остатком", net_cost=100.0))
    _card(db, "wb", vendor="STK", size="42")
    _card(db, "wb", vendor="STK", size="44")
    _stock_size(db, "wb", "STK", TODAY, qty=5, qf=5, size="44", barcode="B-44")
    db.commit()

    r = replenish_rows(db, D0, TODAY, target_days=30, span_days=30,
                       view="sizes", hide_zero_sizes=True)
    assert [x["size"] for x in r["rows"]] == ["44"]
    assert r["meta"]["count"] == 1


def test_size_view_hide_zero_keeps_size_with_ozon_stock(db):
    """Остаток Ozon тоже держит размер «живым» (проверка по всем кодам)."""
    db.add(models.Product(article="OZS", name="Озон-остаток", net_cost=100.0))
    _card(db, "wb", vendor="OZS", size="42")
    _stock_size(db, "ozon", "OZS", TODAY, qty=5, qf=5, size="42", barcode="B-42")
    db.commit()

    r = replenish_rows(db, D0, TODAY, target_days=30, span_days=30,
                       view="sizes", hide_zero_sizes=True)
    assert [x["size"] for x in r["rows"]] == ["42"]


def test_api_replenish_size_view(db, api_client):
    db.add(models.Product(article="SZ", name="Свитшот", net_cost=100.0))
    _card(db, "wb", vendor="SZ", size="S")
    _card(db, "wb", vendor="SZ", size="L")
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


def test_api_replenish_size_view_hide_zero_sizes(db, api_client):
    """hide_zero_sizes в API: таблица и Excel-выгрузка следуют фильтру."""
    db.add(models.Product(article="SZ", name="Свитшот", net_cost=100.0))
    _card(db, "wb", vendor="SZ", size="S", barcode="B-S")
    _card(db, "wb", vendor="SZ", size="L", barcode="B-L")
    db.add(_wb_row_size("s1", "SZ", TODAY, qty=60, tech_size="S",
                        income=54000.0, amount=60000.0))
    db.commit()

    all_ = api_client.get("/api/replenish", params={
        "date_from": D0.isoformat(), "date_to": TODAY.isoformat(),
        "target_days": 30, "window_days": 30, "view": "sizes",
    })
    assert all_.status_code == 200
    assert {x["size"] for x in all_.json()["rows"]} == {"S", "L"}

    filt = api_client.get("/api/replenish", params={
        "date_from": D0.isoformat(), "date_to": TODAY.isoformat(),
        "target_days": 30, "window_days": 30, "view": "sizes",
        "hide_zero_sizes": 1,
    })
    assert filt.status_code == 200
    assert [x["size"] for x in filt.json()["rows"]] == ["S"]

    xp = api_client.get("/api/export/replenish", params={
        "date_from": D0.isoformat(), "date_to": TODAY.isoformat(),
        "view": "sizes", "hide_zero_sizes": 1,
    })
    assert xp.status_code == 200
    ws = load_workbook(io.BytesIO(xp.content)).active
    sizes_in_excel = [ws.cell(row, 2).value for row in range(2, ws.max_row + 1)]
    assert sizes_in_excel == ["S"]       # мёртвый L не добрался до выгрузки


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


def test_api_replenish_to_sort_column(db, api_client):
    """«Дослать» приходит в таблицу и колонкой уходит в Excel-выгрузку."""
    db.add(models.Product(article="EXP-1", name="Выгрузка", net_cost=100.0))
    _card(db, "wb", vendor="EXP-1", size="42")
    db.add(models.CustomStock(article="EXP-1", quantity=2, net_cost=100))
    db.commit()

    resp = api_client.get("/api/replenish", params={
        "date_from": D0.isoformat(), "date_to": TODAY.isoformat(),
        "target_days": 30, "window_days": 30,
    })
    assert resp.status_code == 200
    row = resp.json()["rows"][0]
    assert row["to_sort"] == 1
    assert row["wb_def"] == 1

    xp = api_client.get("/api/export/replenish", params={
        "date_from": D0.isoformat(), "date_to": TODAY.isoformat(),
    })
    assert xp.status_code == 200
    ws = load_workbook(io.BytesIO(xp.content)).active
    headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
    assert "Дослать" in headers


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
    """Главная боль: размеры без продаж в окне тоже должны попасть в план.

    Здесь ``min_size_stock=0``, чтобы проверить именно попадание размера в
    список — правило «1 шт в пустой размер» разбирается отдельно.
    """
    _card_size(db, "wb", "PLAN-1", "42", "111")
    _card_size(db, "wb", "PLAN-1", "44", "222")
    _card_size(db, "wb", "PLAN-1", "46", "333")
    db.add(_wb_size_row("p1", "PLAN-1", "42", TODAY, qty=180))
    db.commit()

    plan = wb_sorting_plan(db, TODAY, target_days=30, min_size_stock=0)
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

    plan = wb_sorting_plan(db, TODAY, target_days=30, min_size_stock=0)
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

    plan = wb_sorting_plan(db, TODAY, target_days=30, min_size_stock=0)
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


# ------------------------------------------- правило «пустой размер не пустой»
def test_plan_keeps_one_piece_in_empty_size(db):
    """Размер в карточке, пустой на WB и без продаж, получает 1 штуку."""
    _card_size(db, "wb", "EMPTY-1", "42")
    db.commit()

    plan = wb_sorting_plan(db, TODAY, target_days=30)
    assert _plan_size(plan, "EMPTY-1", "42")["to_sort"] == 1


def test_plan_does_not_touch_size_that_already_has_stock(db):
    """Если размер уже лежит на WB — добавлять ничего не нужно."""
    _card_size(db, "wb", "EMPTY-2", "42")
    db.add(models.Stock(
        marketplace_id=_mp_id(db, "wb"), date=TODAY, article="EMPTY-2", size="42",
        warehouse="Стек", quantity=0, quantity_full=3, in_way=0,
    ))
    db.commit()

    plan = wb_sorting_plan(db, TODAY, target_days=30)
    assert _plan_size(plan, "EMPTY-2", "42")["to_sort"] == 0


def test_plan_floor_can_be_switched_off(db):
    _card_size(db, "wb", "EMPTY-3", "42")
    db.commit()

    plan = wb_sorting_plan(db, TODAY, target_days=30, min_size_stock=0)
    assert _plan_size(plan, "EMPTY-3", "42")["to_sort"] == 0


def test_plan_floor_does_not_override_demand(db):
    """Правило не должно урезать реальный дефицит."""
    _card_size(db, "wb", "EMPTY-4", "42")
    db.add(_wb_size_row("f4", "EMPTY-4", "42", TODAY, qty=900))  # 5 шт/дн
    db.commit()

    plan = wb_sorting_plan(db, TODAY, target_days=30)
    assert _plan_size(plan, "EMPTY-4", "42")["to_sort"] == 150


def test_plan_total_counts_floor_across_sizes(db):
    for sz in ("42", "44", "46"):
        _card_size(db, "wb", "EMPTY-5", sz)
    db.commit()

    plan = wb_sorting_plan(db, TODAY, target_days=30)
    assert plan["EMPTY-5"]["total"] == 3


def test_plan_all_time_window_uses_whole_history(db):
    """Окно «всё время»: продажа 3-летней давности должна попасть в скорость."""
    _card_size(db, "wb", "ALL-1", "42")
    db.add(_wb_size_row("a1", "ALL-1", "42", TODAY - timedelta(days=900), qty=900))
    db.commit()

    plan = wb_sorting_plan(db, TODAY, target_days=30, velocity_days=0)
    s = _plan_size(plan, "ALL-1", "42")
    assert s["net"] == 900
    assert s["vel"] > 0.0, "скорость не должна обнуляться на всём окне"
    assert s["to_sort"] > 1


def test_plan_365_window_ignores_older_sales(db):
    _card_size(db, "wb", "ALL-2", "42")
    db.add(_wb_size_row("a2", "ALL-2", "42", TODAY - timedelta(days=400), qty=900))
    db.add(_wb_size_row("a3", "ALL-2", "44", TODAY - timedelta(days=10), qty=360))
    db.commit()

    plan = wb_sorting_plan(db, TODAY, target_days=30, velocity_days=365)
    assert _plan_size(plan, "ALL-2", "42")["to_sort"] == 1   # забытая продажа → пол
    assert _plan_size(plan, "ALL-2", "44")["to_sort"] > 1   # свежая продажа учтена


# ------------------------------------------- покрытие и коэффициент прибыльности
def test_profit_factor_is_smooth_and_capped():
    """Плавная шкала: ≤0 → не подсортировываем, 15% → ×1.0, 30%+ → ×2.0."""
    assert profit_factor(None) == 0.0
    assert profit_factor(-12.0) == 0.0
    assert profit_factor(0.0) == 0.0
    assert profit_factor(7.5) == pytest.approx(0.5)
    assert profit_factor(15.0) == pytest.approx(1.0)
    assert profit_factor(30.0) == pytest.approx(2.0)
    assert profit_factor(90.0) == pytest.approx(2.0)


def test_plan_coverage_days_replaces_target_days(db):
    """Покрытие period/4 вместо «Запаса»: покрытие — шаг умножения скорости."""
    _card_size(db, "wb", "COV-1", "42")
    db.add(_wb_size_row("c1", "COV-1", "42", TODAY, qty=180))  # 1 шт/дн
    db.commit()

    plan = wb_sorting_plan(db, TODAY, target_days=30, coverage_days=7.5,
                           min_size_stock=0)
    assert _plan_size(plan, "COV-1", "42")["to_sort"] == 8      # ceil(7.5 × 1.0)


def test_plan_factor_multiplies_target(db):
    """Коэффициент ×1.0 — нейтральный, ×2.0 удваивает подсорт."""
    _card_size(db, "wb", "FCT-1", "42")
    db.add(_wb_size_row("f1", "FCT-1", "42", TODAY, qty=180))
    db.commit()

    one = wb_sorting_plan(db, TODAY, target_days=30, coverage_days=10,
                          profit_factor={"FCT-1": 1.0}, min_size_stock=0)
    two = wb_sorting_plan(db, TODAY, target_days=30, coverage_days=10,
                          profit_factor={"FCT-1": 2.0}, min_size_stock=0)
    assert _plan_size(one, "FCT-1", "42")["to_sort"] == 10
    assert _plan_size(two, "FCT-1", "42")["to_sort"] == 20


def test_plan_loss_making_article_gets_nothing(db):
    """Убыточный товар не подсортировывается совсем — даже пустой размер."""
    _card_size(db, "wb", "LOSS-1", "42")
    db.add(_wb_size_row("l1", "LOSS-1", "42", TODAY, qty=180))
    db.commit()

    plan = wb_sorting_plan(db, TODAY, target_days=30, coverage_days=7.5,
                           profit_factor={"LOSS-1": 0.0})
    assert _plan_size(plan, "LOSS-1", "42")["to_sort"] == 0
    assert plan["LOSS-1"]["total"] == 0
    assert plan["LOSS-1"]["factor"] == 0.0


def test_plan_loss_making_article_keeps_nothing_even_with_floor(db):
    """Правило пола не должно resurrect-ить убыточный товар."""
    _card_size(db, "wb", "LOSS-2", "42")
    db.commit()

    plan = wb_sorting_plan(db, TODAY, target_days=30,
                           profit_factor={"LOSS-2": 0.0})
    assert _plan_size(plan, "LOSS-2", "42")["to_sort"] == 0


def test_plan_factor_only_present_in_profit_mode(db):
    """Ключ factor — признак включённого режима прибыльности (его читает PDF)."""
    _card_size(db, "wb", "MODE-1", "42")
    db.commit()

    off = wb_sorting_plan(db, TODAY, target_days=30)
    on = wb_sorting_plan(db, TODAY, target_days=30, profit_factor={"MODE-1": 1.2})
    assert "factor" not in off["MODE-1"]
    assert "factor" not in _plan_size(off, "MODE-1", "42")
    assert on["MODE-1"]["factor"] == pytest.approx(1.2)
    assert _plan_size(on, "MODE-1", "42")["factor"] == pytest.approx(1.2)


def test_plan_unknown_article_keeps_default_factor(db):
    """Артикул без маржи (нет продаж) — нейтральный коэффициент и правило пола."""
    _card_size(db, "wb", "MODE-2", "42")
    db.commit()

    plan = wb_sorting_plan(db, TODAY, target_days=30,
                           profit_factor={"OTHER": 0.0})
    assert _plan_size(plan, "MODE-2", "42")["to_sort"] == 1


def test_plan_empty_article_list_returns_empty_plan(db):
    """articles=[] — это «ничего не брать», а не «взять всё»."""
    _card_size(db, "wb", "EMPTY-LIST", "42")
    db.add(_wb_size_row("e1", "EMPTY-LIST", "42", TODAY, qty=180))
    db.commit()

    assert wb_sorting_plan(db, TODAY, target_days=30, articles=[]) == {}


def test_wb_profit_factors_uses_margin_pct(db):
    """Коэффициенты берутся из маржи по детализации, окно ограничено годом."""
    _card_size(db, "wb", "PF-1", "42")
    _card_size(db, "wb", "PF-2", "42")
    _card_size(db, "wb", "PF-3", "42")
    # без Product net_cost подставляется settings.default_net_cost и PF-1
    # выходит убыточным — поэтому себестоимость задаём явно
    db.add(models.Product(article="PF-1", name="Прибыльный", net_cost=50.0,
                          replenishable=True))
    db.add(models.Product(article="PF-3", name="Убыточный", net_cost=900.0,
                          replenishable=True))
    # PF-1: маржа сильно выше себестоимости -> высокий процент
    db.add(_wb_size_row("pf1", "PF-1", "42", TODAY, qty=10))
    db.add(_wb_size_row("pf1b", "PF-1", "42", TODAY, qty=10))
    # PF-2: продажа 400 дней назад — за год её не видно, коэффициента не будет
    db.add(_wb_size_row("pf2", "PF-2", "42", TODAY - timedelta(days=400), qty=10))
    # PF-3: цена реализации ниже себестоимости -> убыток
    db.add(models.WbDetailRow(
        op_key="pf3", source="excel", article="PF-3", doc_type_name="Продажа",
        sale_dt=TODAY, quantity=10, retail_amount=1000.0, for_pay=850.0,
        tech_size="42", sku="",
    ))
    db.commit()

    factors = wb_profit_factors(db, TODAY, velocity_days=0)
    assert "PF-3" in factors and factors["PF-3"] == 0.0   # убыток -> не подсортируем
    assert factors["PF-1"] > 0.0                          # прибыльный -> подсортируем
    assert factors.get("PF-2") is None                    # нет продаж за год


def test_wb_profit_factors_respects_article_filter(db):
    _card_size(db, "wb", "PFF-1", "42")
    _card_size(db, "wb", "PFF-2", "42")
    for a in ("PFF-1", "PFF-2"):
        db.add(_wb_size_row(f"pff{a}", a, "42", TODAY, qty=10))
    db.commit()

    factors = wb_profit_factors(db, TODAY, articles=["PFF-1"])
    assert set(factors) == {"PFF-1"}


# — PDF из Excel: бюджет «Итого дослать» из колонки «WB дефицит» ———————
# Разрез «по размерам» файла в PDF не участвует: бюджет по артикулу
# распределяется по размерам, чтобы карточка не противоречила файлу.

def _budget_plan():
    """План-заглушка: два продающихся размера и один мёртвый (vel=0)."""
    sizes = [
        {"size": "M", "to_sort": 10, "vel": 5.0},
        {"size": "L", "to_sort": 4, "vel": 0.0},
        {"size": "S", "to_sort": 6, "vel": 2.5},
    ]
    return {"BUD-1": {"article": "BUD-1", "sizes": sizes,
                      "total": sum(s["to_sort"] for s in sizes)}}


def _to_sorts(plan, art="BUD-1"):
    return [s["to_sort"] for s in plan[art]["sizes"]]


def test_sort_budget_unknown_article_keeps_plan():
    plan = _budget_plan()
    before = _to_sorts(plan)
    apply_sort_budget(plan, {"OTHER": 3})
    assert _to_sorts(plan) == before
    assert plan["BUD-1"]["total"] == 20


def test_sort_budget_equal_total_is_noop():
    plan = _budget_plan()
    apply_sort_budget(plan, {"BUD-1": 20})
    assert _to_sorts(plan) == [10, 4, 6]
    assert plan["BUD-1"]["total"] == 20


def test_sort_budget_cut_fills_best_sellers_first():
    """Меньший бюджет: сначала продающиеся размеры, мёртвый — последним."""
    plan = _budget_plan()
    apply_sort_budget(plan, {"BUD-1": 8})
    # приоритет M (vel 5) -> S (vel 2.5) -> L (vel 0): M берёт min(10, 8)
    assert _to_sorts(plan) == [8, 0, 0]
    assert plan["BUD-1"]["total"] == 8


def test_sort_budget_cut_stops_when_exhausted():
    plan = _budget_plan()
    apply_sort_budget(plan, {"BUD-1": 13})
    # M: 10 (vel 5), затем S: 3 из 6 (vel 2.5), L: 0
    assert _to_sorts(plan) == [10, 0, 3]
    assert plan["BUD-1"]["total"] == 13


def test_sort_budget_increase_goes_to_selling_sizes():
    """Больший бюджет: докидка только продающимся размерам."""
    plan = _budget_plan()
    apply_sort_budget(plan, {"BUD-1": 25})
    # база 20 + 5 сверху: live = [M, S] по кругу -> M+3, S+2, L без изменений
    assert _to_sorts(plan) == [13, 4, 8]
    assert plan["BUD-1"]["total"] == 25


def test_sort_budget_zero_zeroes_everything():
    plan = _budget_plan()
    apply_sort_budget(plan, {"BUD-1": 0})
    assert _to_sorts(plan) == [0, 0, 0]
    assert plan["BUD-1"]["total"] == 0


def test_sort_budget_negative_is_clamped_to_zero():
    plan = _budget_plan()
    apply_sort_budget(plan, {"BUD-1": -7})
    assert plan["BUD-1"]["total"] == 0
    assert _to_sorts(plan) == [0, 0, 0]


def test_sort_budget_total_always_equals_sum_of_sizes():
    for budget in (1, 7, 19, 20, 21, 54):
        plan = _budget_plan()
        apply_sort_budget(plan, {"BUD-1": budget})
        sizes = plan["BUD-1"]["sizes"]
        assert sum(s["to_sort"] for s in sizes) == budget, budget
        assert plan["BUD-1"]["total"] == budget, budget
        assert all(s["to_sort"] >= 0 for s in sizes), budget


def test_sort_budget_applied_to_real_plan(db):
    """Бюджет поверх настоящего плана: итог равен «WB дефицит» из файла."""
    _card_size(db, "wb", "BUDR-1", "42")
    _card_size(db, "wb", "BUDR-1", "43")
    db.add(_wb_size_row("budr142", "BUDR-1", "42", TODAY, qty=30))
    db.add(_wb_size_row("budr143", "BUDR-1", "43", TODAY, qty=10))
    db.commit()

    plan = wb_sorting_plan(db, TODAY, articles=["BUDR-1"])
    natural = plan["BUDR-1"]["total"]
    assert natural > 1

    apply_sort_budget(plan, {"BUDR-1": 1})
    assert plan["BUDR-1"]["total"] == 1
    assert sum(s["to_sort"] for s in plan["BUDR-1"]["sizes"]) == 1
    # выдали продающемуся размеру (42 продавался активнее)
    assert max(plan["BUDR-1"]["sizes"], key=lambda s: s["to_sort"])["size"] == "42"


