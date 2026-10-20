# -*- coding: utf-8 -*-
"""Фильтр «Артикулы»: разбор текста/Excel и резолв токенов в артикулы."""
import io

import pandas as pd

from app import models
from app.services import article_tokens


def _xlsx(rows) -> bytes:
    buf = io.BytesIO()
    pd.DataFrame(rows).to_excel(buf, index=False, header=False, engine="openpyxl")
    return buf.getvalue()


def test_split_tokens_lines_commas_and_dedup():
    toks = article_tokens.split_tokens(" A-1 \n a-1\n, 4600000000001;;;B-2 ,")
    assert toks == ["A-1", "4600000000001", "B-2"]


def test_split_tokens_strips_excel_float_suffix():
    assert article_tokens.split_tokens("2047932869792.0") == ["2047932869792"]
    assert article_tokens.split_tokens("") == []


def test_tokens_from_xlsx_first_column_plus_labelled_columns():
    data = _xlsx([
        ["Артикул", "Баркод", "Комментарий"],
        ["A-1", "2001", "x"],
        ["A-2", "2002", ""],
        ["A-1", "2001", ""],  # дубль по обоим столбцам
    ])
    tokens, meta = article_tokens.tokens_from_xlsx(data)
    assert tokens == ["A-1", "2001", "A-2", "2002"]
    assert meta["count"] == 4
    assert meta["columns"] == ["Артикул", "Баркод"]
    assert meta["rows"] == 3
    assert meta["header"] is True


def test_tokens_from_xlsx_without_header_uses_first_column_only():
    data = _xlsx([
        ["A-1", "что-то"],
        ["A-2", "ещё"],
    ])
    tokens, meta = article_tokens.tokens_from_xlsx(data)
    assert tokens == ["A-1", "A-2"]
    assert meta["columns"] == ["колонка 1"]
    assert meta["header"] is False


def test_tokens_from_xlsx_empty_sheet():
    tokens, meta = article_tokens.tokens_from_xlsx(_xlsx([[None], [None]]))
    assert tokens == []
    assert meta["count"] == 0


def test_resolve_articles_by_all_identifiers(db):
    wb = db.query(models.Marketplace).filter_by(code="wb").one()
    db.add_all([
        models.Product(article="A-1", name="Товар 1", barcode="2001"),
        models.ProductSize(article="A-1", size="46", barcode="3001"),
        models.ProductAlias(alias_article="A-1-OZ", article="A-1"),
        models.NmArticle(nm_id="1001", article="A-1"),
        models.MarketplaceCard(marketplace_id=wb.id, chrt_id="1", nm_id="1002",
                               vendor_code="A-2", base_article="A-1", barcode="4002"),
    ])
    db.commit()

    arts, matched = article_tokens.resolve_articles(
        db, ["a-1", "2001", "3001", "a-1-oz", "1001", "1002", "4002", "NOPE"])

    assert arts == {"A-1"}
    assert matched == {"A-1", "2001", "3001", "A-1-OZ", "1001", "1002", "4002"}
    assert "NOPE" not in matched


def test_resolve_articles_empty_tokens(db):
    arts, matched = article_tokens.resolve_articles(db, [])
    assert arts == set() and matched == set()
    arts, matched = article_tokens.resolve_articles(db, None)
    assert arts == set() and matched == set()
