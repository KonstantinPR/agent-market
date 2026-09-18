"""T-18: синк карточек WB+Ozon в общий каталог (идентификация, алиасы, перезапись)."""
import pandas as pd
import pytest

from app import models
from app.services import refresh as refresh_service
from app.services import sync as sync_service
from app.services.sync import normalize_catalog_card, sync_catalog_from_cards

CAT = sync_service.CATALOG_FIELDS


def _cards(*rows):
    return pd.DataFrame([{k: r.get(k, "") for k in CAT} for r in rows])


# ------------------------------------------------------------------ нормализация


def test_normalize_wb_card():
    df = pd.DataFrame({
        "vendorCode": ["A1", "A2"],
        "title": ["Товар A1", "Товар A2"],
        "brand": ["Бренд X", "Бренд X"],
        "subject": [{"name": "Обувь", "parentName": "Дом"}, "Одежда"],
        "techSize": ["46", "47"],
        "skus": [[2000000001, 2000000002], ""],
        "dimensions": [{"length": 300, "width": 200, "height": 100}, None],
        "composition": ["Кожа", ""],
    })
    n = normalize_catalog_card(df, "wb")
    assert n is not None
    assert list(n.columns) == CAT
    r0 = n.iloc[0]
    assert r0["article"] == "A1"
    assert r0["name"] == "Товар A1"
    assert r0["subject"] == "Обувь"
    assert r0["size"] == "46"
    assert r0["barcode"] == "2000000001"          # первый штрихкод размера
    assert r0["volume_l"] == pytest.approx(6.0)   # 300*200*100 мм³ / 1e6
    assert r0["composition"] == "Кожа"
    assert r0["source"] == "wb"
    assert all(n["article"] != "")


def test_normalize_oz_card():
    df = pd.DataFrame({
        "Ozon Product ID": ["90001", "90002"],
        "Offer ID": ["OZ-1", "OZ-2"],
        "Name": ["Ozon 1", "Ozon 2"],
        "SKU": ["3000000001", "3000000002"],
        "Barcode": ["3 000000001", "3 000000002"],
        "Category": ["Обувь", "Обувь"],
    })
    n = normalize_catalog_card(df, "ozon")
    assert n is not None
    r0 = n.iloc[0]
    assert r0["article"] == "OZ-1"
    assert r0["name"] == "Ozon 1"
    assert r0["barcode"] == "3000000001"
    assert r0["subject"] == "Обувь"
    assert r0["size"] == ""
    assert r0["source"] == "ozon"


def test_normalize_none_without_article_col():
    assert normalize_catalog_card(pd.DataFrame({"title": ["X"]}), "wb") is None
    assert normalize_catalog_card(pd.DataFrame({"Name": ["X"]}), "ozon") is None
    assert normalize_catalog_card(pd.DataFrame(), "wb") is None
    assert normalize_catalog_card(None, "wb") is None


def test_normalize_drops_empty_articles_and_dups():
    df = pd.DataFrame({
        "vendorCode": ["A1", "", "A1", "A2"],
        "title": ["Т1", "Т2", "Т3", "Т4"],
        "skus": ["2001", "2002", "2001", "2003"],
        "techSize": ["46", "", "46", ""],
    })
    n = normalize_catalog_card(df, "wb")
    assert sorted(n["article"]) == ["A1", "A2"]
    assert len(n) == 2  # дубликат (article, size, barcode) ушёл


# ------------------------------------------------------------------ идентификация


def test_creates_products_and_sizes(db):
    cards = _cards(
        {"article": "A1", "name": "Товар 1", "brand": "Br", "subject": "Обувь",
         "size": "46", "barcode": "2001", "volume_l": 0.3, "source": "wb"},
        {"article": "OZ-1", "name": "Ozon 1", "brand": "Br2", "subject": "Обувь",
         "size": "", "barcode": "3001", "volume_l": 0, "source": "ozon"},
    )
    r = sync_catalog_from_cards(db, cards)
    assert r["created_products"] == 2
    assert r["sizes_added"] == 2
    assert r["updated_products"] == 0
    assert r["aliases"] == 0
    assert r["errors"] == []
    p1 = db.get(models.Product, "A1")
    assert p1.name == "Товар 1"
    assert p1.brand == "Br"
    assert float(p1.volume_l) == pytest.approx(0.3)
    sizes = db.query(models.ProductSize).filter_by(article="A1").all()
    assert len(sizes) == 1
    assert sizes[0].size == "46"
    assert sizes[0].barcode == "2001"


def test_barcode_match_merges_via_alias(db):
    sync_catalog_from_cards(db, _cards(
        {"article": "A1", "size": "46", "barcode": "2001", "source": "wb"},
        {"article": "OZ-1", "size": "", "barcode": "3001", "source": "ozon"},
    ))
    # OZ-1 теперь продаётся с баркодом A1 -> объединяем в A1, старый артикул — алиас
    r = sync_catalog_from_cards(db, _cards(
        {"article": "OZ-1", "size": "", "barcode": "2001", "source": "ozon"},
    ))
    assert r["created_products"] == 0
    assert r["aliases"] == 1
    al = db.query(models.ProductAlias).filter_by(alias_article="OZ-1").all()
    assert len(al) == 1 and al[0].article == "A1"
    # последующие строки со старым артикулом разрешаются через алиас
    r2 = sync_catalog_from_cards(db, _cards(
        {"article": "OZ-1", "size": "", "barcode": "3002", "source": "ozon"},
    ))
    assert r2["created_products"] == 0
    assert r2["aliases"] == 0
    assert r2["sizes_updated"] == 1
    # размеры A1: первый баркод 2001 заменён на 3002; 3001 живёт в ряду OZ-1
    sizes = db.query(models.ProductSize).filter_by(article="A1").order_by(models.ProductSize.id).all()
    assert {s.barcode for s in sizes} == {"2001", "3002"}


def test_article_size_match_no_new_product(db):
    prod = models.Product(article="X", name="Старый")
    db.add(prod)
    db.add(models.ProductSize(article="X", size="М", barcode="9"))
    db.commit()
    r = sync_catalog_from_cards(db, _cards(
        {"article": "X", "size": "М", "barcode": "", "source": "wb"},
    ))
    assert r["created_products"] == 0
    assert r["sizes_added"] == 0
    assert db.get(models.Product, "X").name == "Старый"


def test_alias_article_resolves_to_first_product(db):
    sync_catalog_from_cards(db, _cards(
        {"article": "A1", "size": "", "barcode": "2001", "source": "wb"},
    ))
    db.add(models.ProductAlias(alias_article="STAR", article="A1"))
    db.commit()
    r = sync_catalog_from_cards(db, _cards(
        {"article": "STAR", "size": "", "barcode": "", "source": "wb"},
    ))
    assert r["created_products"] == 0
    # строка ушла в существующую product_sizes (A1,"") — алиас отработал
    sizes = db.query(models.ProductSize).filter_by(article="A1").all()
    assert len(sizes) == 1


def test_empty_article_skips_with_error(db):
    r = sync_catalog_from_cards(db, _cards({"article": "", "size": "", "source": "wb"}))
    assert r["created_products"] == 0
    assert r["rows"] == 0
    # пустые артикулы отсекаются на этапе нормализации/фильтра, до идентификации


def test_second_barcode_updates_size_row(db):
    sync_catalog_from_cards(db, _cards(
        {"article": "A1", "size": "46", "barcode": "2001", "source": "wb"},
    ))
    r = sync_catalog_from_cards(db, _cards(
        {"article": "A1", "size": "46", "barcode": "2002", "source": "wb"},
    ))
    assert r["sizes_updated"] == 1
    srow = db.query(models.ProductSize).filter_by(article="A1", size="46").one()
    assert srow.barcode == "2002"


# ------------------------------------------------------------------ перезапись


def _seed_named_product(db):
    sync_catalog_from_cards(db, _cards(
        {"article": "A1", "name": "Имя 1", "brand": "Br1", "subject": "Обувь",
         "size": "46", "barcode": "2001", "source": "wb"},
    ))
    p = db.get(models.Product, "A1")
    p.net_cost = 150
    p.replenishable = True
    db.commit()


def test_overwrite_false_fills_only_empty(db):
    _seed_named_product(db)
    r = sync_catalog_from_cards(db, _cards(
        {"article": "A1", "name": "Имя 2", "brand": "Br2", "subject": "",
         "composition": "Кожа", "size": "46", "barcode": "2001", "source": "wb"},
    ))
    p = db.get(models.Product, "A1")
    assert p.name == "Имя 1"          # не перезаписываем
    assert p.brand == "Br1"
    assert p.composition == "Кожа"    # было пусто — заполнили
    assert r["updated_products"] == 1


def test_overwrite_true_replaces_fields(db):
    _seed_named_product(db)
    r = sync_catalog_from_cards(db, _cards(
        {"article": "A1", "name": "Имя 2", "brand": "Br2", "subject": "Сумки",
         "size": "46", "barcode": "2001", "source": "wb"},
    ), overwrite=True)
    p = db.get(models.Product, "A1")
    assert p.name == "Имя 2"
    assert p.brand == "Br2"
    assert p.subject == "Сумки"
    assert r["updated_products"] == 1


def test_net_cost_and_replenishable_never_touched(db):
    _seed_named_product(db)
    sync_catalog_from_cards(db, _cards(
        {"article": "A1", "name": "Имя 2", "size": "46", "barcode": "2001",
         "net_cost": 999.0, "source": "wb"},
    ), overwrite=True)
    p = db.get(models.Product, "A1")
    assert float(p.net_cost) == 150.0
    assert p.replenishable is True


def test_unmapped_fields_reported(db):
    cards = _cards({"article": "A1", "size": "46", "barcode": "2001", "source": "wb"})
    cards["foo"] = ["x"]
    r = sync_catalog_from_cards(db, cards)
    assert "foo" in r["unmapped_fields"]


# ------------------------------------------------------------------ pull_catalog


def test_pull_catalog_writes_marketplace_cards_and_catalog(db, patch_factory):
    res = refresh_service.pull_catalog(db)
    rep = res["report"]
    assert res["wb_rows"] == 2
    assert res["oz_rows"] == 2
    assert rep["created_products"] == 4
    assert rep["sizes_added"] == 4
    # marketplace_cards/nm_articles пишутся для WB и Ozon
    mp_wb = db.query(models.Marketplace).filter_by(code="wb").one()
    mp_oz = db.query(models.Marketplace).filter_by(code="ozon").one()
    assert db.query(models.MarketplaceCard).filter_by(marketplace_id=mp_wb.id).count() == 2
    assert db.query(models.MarketplaceCard).filter_by(marketplace_id=mp_oz.id).count() == 2
    assert db.query(models.NmArticle).count() == 2
    assert db.get(models.Product, "TST-1").name == "Товар 1"
    assert db.get(models.Product, "OZ-1").name == "Ozon 1"