"""Резолвер базы/размера артикулов Ozon."""
import datetime as dt

import pytest
from sqlalchemy import select

from app import models
from app.services.ozon_article import (
    SIZE_TOKENS,
    backfill,
    build_offer_map,
    collect_stored_offers,
    group_key,
    looks_like_size,
    native_group_map,
    resolve_offer,
    sizes_for,
    split_tail,
)

# Артикулы WB из боевой базы (характерные примеры).
WB = {
    "jbg-08283-a12753": "JBG-08283-A12753",
    "jbg-j763-a7871a1": "JBG-J763-A7871A1",
    "ian-28-black-blue": "IAN-28-BLACK-BLUE",
    "hat-05-blue-pink": "HAT-05-BLUE-PINK",
    "tie-bigben-01": "TIE-BIGBEN-01",
    "jbr-lya-297-a-blue": "JBR-LYA-297-A-BLUE",
}


def test_split_tail_basic():
    assert split_tail("JBG-08283-A12753-29") == ("JBG-08283-A12753", "29")
    assert split_tail("PLAIN") == ("PLAIN", "")
    assert split_tail("") == ("", "")


def test_split_tail_is_mechanical_not_size_decision():
    """Чистая разбивка не решает, размер ли хвост (это делает resolve_offer)."""
    assert split_tail("TIE-BIGBEN-01") == ("TIE-BIGBEN", "01")


def test_split_tail_trailing_dash_keeps_whole():
    # артикул, заканчивающийся на дефис: дефис не считается разделителем размера
    assert split_tail("ABC-") == ("ABC-", "")


def test_looks_like_size():
    assert looks_like_size("29")
    assert looks_like_size("0")
    assert looks_like_size("XL")
    assert looks_like_size("xl")
    assert not looks_like_size("PINK")
    assert not looks_like_size("WH")
    assert not looks_like_size("")
    assert "xxl" in SIZE_TOKENS


def test_resolve_wb_match_on_tail():
    base, size, method = resolve_offer("JBG-08283-A12753-29", WB)
    assert (base, size, method) == ("JBG-08283-A12753", "29", "wb")


def test_resolve_exact_wb_match_is_sizeless():
    """Совпадение на полном артикуле WB = товар безразмерный."""
    base, size, method = resolve_offer("HAT-05-BLUE-PINK", WB)
    assert (base, size, method) == ("HAT-05-BLUE-PINK", "", "wb")


def test_resolve_multi_segment_size():
    """Размер может занимать несколько сегментов: ...-9-23."""
    base, size, method = resolve_offer("JBG-J763-A7871A1-9-23", WB)
    assert (base, size, method) == ("JBG-J763-A7871A1", "9-23", "wb")


def test_resolve_alpha_size_via_wb():
    base, size, method = resolve_offer("IAN-28-BLACK-BLUE-L", WB)
    assert (base, size, method) == ("IAN-28-BLACK-BLUE", "L", "wb")


def test_resolve_no_wb_numeric_tail():
    """Без карточки WB числовой хвост всё равно размер."""
    base, size, method = resolve_offer("ZZZ-UNKNOWN-42", {})
    assert (base, size, method) == ("ZZZ-UNKNOWN", "42", "tail")


def test_resolve_no_wb_size_token_tail():
    base, size, method = resolve_offer("ZZZ-UNKNOWN-XL", {})
    assert (base, size, method) == ("ZZZ-UNKNOWN", "XL", "tail")


def test_resolve_no_wb_color_tail_stays_whole():
    """`-WH`, `-IV`, цвета и артикулы без дефиса — товар безразмерный."""
    for offer in ("BAND-01-BLUE", "HAT-10-BEIGE", "MIT-2-4-01K-0-07-IV", "PLAINART"):
        base, size, method = resolve_offer(offer, {})
        assert (base, size) == (offer, ""), offer
        assert method == "whole", offer


def test_resolve_alias_priority():
    base, size, method = resolve_offer(
        "JBG-08283-A12753-29", WB, aliases={"JBG-08283-A12753": "JBG-08283-A12753-X"})
    assert base == "JBG-08283-A12753-X"
    assert method == "alias"


def test_resolve_alias_on_full_offer_is_sizeless():
    base, size, method = resolve_offer("MY-OLD-NAME", WB, aliases={"MY-OLD-NAME": "CAT-1"})
    assert (base, size, method) == ("CAT-1", "", "alias")


def test_native_group_used_when_present():
    import pandas as pd
    df = pd.DataFrame([{"offer_id": "A-1", "complex_id": "A"}])
    assert native_group_map(df) == {"A-1": "A"}
    base, size, method = resolve_offer("A-1", {}, {}, native_group_map(df))
    assert (base, size, method) == ("A", "1", "native")


def test_native_group_map_empty_without_column():
    import pandas as pd
    df = pd.DataFrame([{"offer_id": "A-1", "sku": "1"}])
    assert native_group_map(df) == {}
    assert native_group_map(None) == {}
    assert native_group_map(pd.DataFrame()) == {}


def test_resolve_empty_offer():
    assert resolve_offer("", WB) == ("", "", "empty")
    assert resolve_offer(None, WB) == ("", "", "empty")


def test_group_key_modes():
    omap = {"A-29": ("A", "29", "wb")}
    assert group_key("A-29", omap, by_size=False) == "A"
    assert group_key("A-29", omap, by_size=True) == "A-29"
    assert group_key("", omap) == ""
    # артикул вне карты остаётся сам собой
    assert group_key("B-30", {}, by_size=False) == "B-30"


def test_sizes_for_sorted_numerically():
    omap = {"A-29": ("A", "29", "wb"), "A-L": ("A", "L", "wb"),
            "A-8": ("A", "8", "wb"), "A-": ("A-", "", "whole")}
    assert sizes_for(["A-29", "A-L", "A-8", "A-"], omap) == ["8", "29", "L"]


def test_build_offer_map_uses_explicit_offers(db):
    got = build_offer_map(db, offers=["JBG-08283-A12753-29", "", "PLAIN", "X-30"])
    assert set(got) == {"JBG-08283-A12753-29", "PLAIN", "X-30"}
    # без карточек WB: числовой хвост — размер, без дефиса — безразмерный товар
    assert got["X-30"][:2] == ("X", "30")
    assert got["PLAIN"][:2] == ("PLAIN", "")
    assert got["JBG-08283-A12753-29"][1] == "29"


def _mp_id(db, code):
    return db.execute(
        select(models.Marketplace.id).where(models.Marketplace.code == code)
    ).scalar_one()


def _seed_wb_card(db, vendor_code):
    db.add(models.MarketplaceCard(
        marketplace_id=_mp_id(db, "wb"), chrt_id="c-" + vendor_code,
        vendor_code=vendor_code, name="товар",
    ))
    db.commit()


def test_collect_stored_offers_sees_rows_without_cards(db):
    """Артикул, которого нет в карточках, всё равно попадает в карту."""
    oz = _mp_id(db, "ozon")
    _seed_wb_card(db, "TIE-BIGBEN-01")
    db.add(models.Stock(
        marketplace_id=oz, date=dt.date(2026, 9, 22), article="TIE-BIGBAN-17-PINKMILK",
        warehouse="FBO Ozon", quantity=3,
    ))
    db.commit()

    assert "TIE-BIGBAN-17-PINKMILK" in collect_stored_offers(db)
    got = build_offer_map(db, offers=collect_stored_offers(db))
    # хвост PINKMILK — не размер, поэтому товар безразмерный
    assert got["TIE-BIGBAN-17-PINKMILK"][:2] == ("TIE-BIGBAN-17-PINKMILK", "")


def test_backfill_covers_offers_missing_from_cards(db):
    """Остатки по товару, ещё не заведённому в карточках, тоже разворачиваются."""
    oz = _mp_id(db, "ozon")
    _seed_wb_card(db, "TIE-BIGBEN-01")
    db.add(models.Stock(
        marketplace_id=oz, date=dt.date(2026, 9, 22), article="TIE-BIGBEN-01-42",
        warehouse="FBO Ozon", quantity=1,
    ))
    db.add(models.Stock(
        marketplace_id=oz, date=dt.date(2026, 9, 22), article="1000",
        warehouse="FBO Ozon", quantity=1,
    ))
    db.commit()

    stats = backfill(db)
    assert stats["stocks"] == 2

    rows = {
        s.article: (s.base_article, s.size)
        for s in db.execute(select(models.Stock)).scalars()
    }
    # артикул WB найден отбрасыванием хвоста -> база + размер
    assert rows["TIE-BIGBEN-01-42"] == ("TIE-BIGBEN-01", "42")
    # мусорный артикул без дефиса остаётся самим собой (безразмерный товар)
    assert rows["1000"] == ("1000", "")
