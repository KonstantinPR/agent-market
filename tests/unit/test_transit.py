# -*- coding: utf-8 -*-
"""Тесты сервиса «в пути» (app/services/transit.py)."""
from app.services import transit


def test_default_cards_are_empty():
    """Пока источников нет — карты пустые, расчёт потребности не меняется."""
    assert transit.supplier_in_transit(None) == {}
    assert transit.marketplace_in_transit(None) == {"wb": {}, "ozon": {}}


def test_normalize_key():
    assert transit.normalize_key(" art ", " s ") == ("ART", "S")
    assert transit.normalize_key("art") == ("ART", "")
    assert transit.normalize_key("") is None
    assert transit.normalize_key(None, "S") is None


def test_normalize_qty():
    assert transit.normalize_qty(5) == 5
    assert transit.normalize_qty("3") == 3
    assert transit.normalize_qty("2.9") == 2
    assert transit.normalize_qty(None) == 0
    assert transit.normalize_qty("мусор") == 0
    assert transit.normalize_qty(-4) == 0   # отрицательного транзита не бывает


def test_by_article_sums_sizes_and_skips_empty():
    card = {
        ("ART", "S"): 2,
        ("art", ""): 3,
        ("ART", "L"): 1,
        ("", "S"): 99,
        ("OTHER", "S"): 0,
    }
    assert transit.by_article(card) == {"ART": 6}
    assert transit.by_article({}) == {}
