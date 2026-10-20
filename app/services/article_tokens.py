# -*- coding: utf-8 -*-
"""Резолв списка артикулов (вставка в поле/Excel) в ключи строк детализации.

Пользователь вставляет артикулы по одному в строке, но это может быть не
только наш артикул: баркод товара/размера/карточки, nmID Wildberries или
артикул Ozon (offer_id / vendor_code). Резолв даёт набор UPPER-артикулов —
ровно те ключи, по которым строятся строки отчётов (``WbDetailRow.article``).

Резолв идёт только по небольшим справочным таблицам (products, product_sizes,
product_aliases, nm_articles, marketplace_cards) — без сканов детализации.
"""
from io import BytesIO
from typing import Iterable, Optional

import pandas as pd
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import models

# Больше не нужно: такое количество токенов уже не влезает в URL-параметр
# (страница/экспорт передают список строкой через запятую).
MAX_TOKENS = 3000

# Точные подписи шапки (после нормализации: нижний регистр, «_» → пробел).
_HEADER_EXACT = {
    "артикул", "артикулы", "арт", "арт.", "article", "articles",
    "артикул wb", "артикул ozon", "артикул озон",
    "barcode", "баркод", "штрихкод", "штрих код", "штрих-код",
    "nmid", "nm id", "nm_id", "sku", "offer id", "offer_id",
    "ozon", "озон", "vendor", "vendor code", "vendor_code", "вендор",
}


def normalize_token(value) -> str:
    """Значение ячейки → токен: strip, без хвоста «.0» у чисел из Excel."""
    s = str(value if value is not None else "").strip()
    if s.endswith(".0") and s[:-2].isdigit():
        s = s[:-2]
    return s


def split_tokens(text) -> list[str]:
    """Текст (строки/запятые/точки с запятой) → токены в порядке ввода."""
    out: list[str] = []
    seen: set[str] = set()
    for chunk in str(text or "").replace("\r", "\n").replace(";", "\n").split("\n"):
        for part in chunk.split(","):
            tok = normalize_token(part).strip().strip('"').strip("'")
            if not tok:
                continue
            key = tok.upper()
            if key in seen:
                continue
            seen.add(key)
            out.append(tok)
            if len(out) >= MAX_TOKENS:
                return out
    return out


def _is_header(value: str) -> bool:
    """Похоже ли значение на подпись колонки (а не на артикул/баркод)."""
    low = " ".join(normalize_token(value).lower().replace("_", " ").split())
    low = low.rstrip(":").strip()
    if not low:
        return False
    if low in _HEADER_EXACT:
        return True
    return low.startswith(("артикул", "баркод", "штрихкод", "штрих-код",
                           "barcode", "nmid ", "nm id ", "nm_id "))


def tokens_from_xlsx(data: bytes) -> tuple[list[str], dict]:
    """Excel → (токены, meta).

    Берём первый столбец всегда; если в первой непустой строке есть подписи
    «Артикул/Баркод/Штрихкод/nmID/SKU/Ozon/vendor», эта строка считается
    шапкой, и дополнительно берём значения из таких колонок.
    """
    grid = pd.read_excel(BytesIO(data), sheet_name=0, header=None, dtype=str)
    grid = grid.where(grid.notna(), "")
    rows = [[normalize_token(v) for v in row] for row in grid.values.tolist()]
    rows = [r for r in rows if any(r)]
    meta = {"count": 0, "rows": 0, "columns": [], "header": False}
    if not rows:
        return [], meta
    first = rows[0]
    hdr_idx = [i for i, v in enumerate(first) if _is_header(v)]
    has_header = bool(hdr_idx)
    data_rows = rows[1:] if has_header else rows
    cols = [0] + [i for i in hdr_idx if i != 0]
    meta.update({
        "rows": len(data_rows),
        "columns": [(first[i] or f"колонка {i + 1}") if has_header and i < len(first)
                    else f"колонка {i + 1}" for i in cols],
        "header": has_header,
    })
    tokens: list[str] = []
    seen: set[str] = set()
    for row in data_rows:
        for i in cols:
            if i >= len(row):
                continue
            val = row[i]
            if not val or _is_header(val):
                continue
            key = val.upper()
            if key in seen:
                continue
            seen.add(key)
            tokens.append(val)
            if len(tokens) >= MAX_TOKENS:
                meta["count"] = len(tokens)
                return tokens, meta
    meta["count"] = len(tokens)
    return tokens, meta


def resolve_articles(db: Session,
                     tokens: Optional[Iterable[str]]) -> tuple[set[str], set[str]]:
    """Токены → (UPPER-артикулы строк детализации, распознанные токены UPPER).

    Распознанные — те, что нашли хотя бы в одном справочнике. Остальные UI
    показывает как «не найдены» (фильтр всё равно применяется к найденным).
    """
    arts: set[str] = set()
    matched: set[str] = set()
    up_set: set[str] = set()
    for t in tokens or []:
        tok = normalize_token(t)
        if tok:
            up_set.add(tok.upper())
    if not up_set:
        return arts, matched
    ups = sorted(up_set)

    def add(article, *sources) -> None:
        art = normalize_token(article).upper()
        if art:
            arts.add(art)
        for s in sources:
            src = normalize_token(s).upper()
            if src and src in up_set:
                matched.add(src)

    # 1. Наш артикул и баркод товара.
    for art, bc in db.execute(select(
            models.Product.article, models.Product.barcode).where(or_(
                func.upper(models.Product.article).in_(ups),
                func.upper(func.coalesce(models.Product.barcode, "")).in_(ups)))):
        add(art, art, bc)

    # 2. Баркоды размеров.
    for art, bc in db.execute(select(
            models.ProductSize.article, models.ProductSize.barcode).where(
                func.upper(func.coalesce(models.ProductSize.barcode, "")).in_(ups))):
        add(art, bc)

    # 3. Алиасы (старый/Ozon-артикул) → наш артикул.
    for alias, art in db.execute(select(
            models.ProductAlias.alias_article, models.ProductAlias.article).where(
                func.upper(models.ProductAlias.alias_article).in_(ups))):
        add(art, alias)

    # 4. nmID WB → артикул продавца.
    for nm, art in db.execute(select(
            models.NmArticle.nm_id, models.NmArticle.article).where(
                func.upper(models.NmArticle.nm_id).in_(ups))):
        add(art, nm)

    # 5. Карточки МП: nmID, вендор-код (Ozon offer_id), баркод.
    for nm, vc, base, bc in db.execute(select(
            models.MarketplaceCard.nm_id, models.MarketplaceCard.vendor_code,
            models.MarketplaceCard.base_article,
            models.MarketplaceCard.barcode).where(or_(
                func.upper(func.coalesce(models.MarketplaceCard.nm_id, "")).in_(ups),
                func.upper(func.coalesce(models.MarketplaceCard.vendor_code, "")).in_(ups),
                func.upper(func.coalesce(models.MarketplaceCard.barcode, "")).in_(ups)))):
        add(base or vc, nm, vc, bc)

    return arts, matched
