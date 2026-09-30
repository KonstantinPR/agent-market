"""Единая группировка артикулов Ozon: база (товар) + размер.

Зачем
-----
У Ozon артикул продавца — это артикул товара плюс размер через последний «-»
(например `JBG-08283-A12753-29` → база `JBG-08283-A12753`, размер `29`).
Поэтому отчёты Ozon по умолчанию дробятся по размерам, тогда как WB отдаёт
отчёт по артикулам целиком. Здесь живёт одно правило, которое переиспользуют
детализация маржи, сводка, размещение, начисления, остатки, цены и дашборд
(в режиме «по артикулам»), чтобы Ozon и WB считались на одном уровне.

Правило выделения базы (приоритеты)
-----------------------------------
1. ``alias``   — ручное соответствие ``product_aliases`` (alias_article → article).
2. ``native``  — нативная группировка Ozon («Объединён»), см. :func:`native_group_map`.
3. ``wb``      — поиск артикула WB: отбрасываем хвостовые сегменты, пока не найдём
   артикул WB. Совпадение на **полном** артикуле = товар **безразмерный**
   (пример: `HAT-05-BLUE-PINK` — это и артикул WB, и артикул Ozon).
4. ``tail``    — хвост из цифр или известный размер (S/M/L/XL…) = размер.
5. ``whole``   — иначе товар безразмерный, дефис в конце не трогаем.

Почему не «просто последний дефис»
----------------------------------
На 41 533 карточках Ozon (сентябрь 2026) наивное правило ломает 376
безразмерных товаров (`HAT-05-BLUE-PINK` → база `HAT-05-BLUE`, размер `PINK`)
и ошибается ещё в 505 случаях, где артикул WB заканчивается раньше
(`JBG-J763-A7871A1-9-23` → база `JBG-J763-A7871A1`, размер `9-23`).
С приоритетами выше расхождение с группировкой WB — 0.36 % (12 групп из 3 357).
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app import models

# Хвост артикула, который почти наверняка является размером.
SIZE_TOKENS = frozenset({
    "xs", "s", "m", "l", "xl", "xxl", "xxxl", "xxxxl",
    "2xl", "3xl", "4xl", "5xl", "2xs", "1xl", "one size", "onesize", "1 size",
    "uk8", "uk10", "uk12", "uk14", "uk16",
})

# Колонки нативной группировки Ozon, если она появится в данных.
NATIVE_BASE_COLS: Tuple[str, ...] = (
    "base_offer_id", "complex_id", "group_offer_id", "merged_offer_id",
)

Resolved = Tuple[str, str, str]  # (base_article, size, method)


def _clean(text: Optional[str]) -> str:
    return str(text or "").strip()


def split_tail(offer_id: str) -> Tuple[str, str]:
    """Отрезает хвост после последнего «-»: `A-B-29` → (`A-B`, `29`).

    Это чисто механическая разбивка, она **не решает**, является ли хвост
    размером: `TIE-BIGBEN-01` тоже даст (`TIE-BIGBEN`, `01`). Решение о
    размере принимает :func:`resolve_offer`. Пустой хвост (артикул кончается
    дефисом) и артикул без дефиса возвращаются целиком.
    """
    art = _clean(offer_id)
    if "-" not in art:
        return art, ""
    base, _, tail = art.rpartition("-")
    if not base or not tail:
        return art, ""
    return base, tail


def looks_like_size(tail: str) -> bool:
    """Хвост похож на размер: число (24, 29, 48) или размер-токен (S, M, XL)."""
    t = _clean(tail)
    if not t:
        return False
    if t.isdigit():
        return True
    return t.lower() in SIZE_TOKENS


def native_group_map(offers_df: Optional[pd.DataFrame]) -> Dict[str, str]:
    """Нативная группировка Ozon («Объединён») из карточек товара, если есть.

    Статус: НЕ ПОДТВЕРЖДЕНО. На 2026-09-27 проверить не удалось — Seller API
    отдаёт 429 на `/v3/product/info/list` и финансовые методы, документация
    недоступна. В карточках, которые мы видели, есть только Ozon Product ID,
    Offer ID, SKU, Barcode и Name, но это не доказательство отсутствия поля.

    Функция — точка врезки: если поле найдётся, достаточно добавить его имя в
    :data:`NATIVE_BASE_COLS` — оно станет приоритетом №1 и перестроит
    группировку без правок в отчётности.
    """
    out: Dict[str, str] = {}
    if offers_df is None or offers_df.empty:
        return out
    if "offer_id" not in offers_df.columns:
        return out
    for col in NATIVE_BASE_COLS:
        if col not in offers_df.columns:
            continue
        for rec in offers_df[["offer_id", col]].itertuples(index=False):
            offer = _clean(rec[0])
            base = _clean(rec[1])
            if offer and base and offer != base:
                out[offer] = base
        if out:
            return out
    return out


def _wb_walk(offer: str, wb_articles: Dict[str, str]) -> Optional[Tuple[str, str]]:
    """Ищет артикул WB, отбрасывая хвостовые сегменты.

    Возвращает (база, размер) либо None, если артикул WB не найден. Совпадение
    на полном артикуле даёт пустой размер — товар безразмерный.
    """
    art = offer
    while art:
        key = art.lower()
        if key in wb_articles:
            size = offer[len(art):].lstrip("-")
            return offer[:len(art)], size
        if "-" not in art:
            break
        art = art.rsplit("-", 1)[0]
    return None


def resolve_offer(
    offer_id: str,
    wb_articles: Optional[Dict[str, str]] = None,
    aliases: Optional[Dict[str, str]] = None,
    native: Optional[Dict[str, str]] = None,
) -> Resolved:
    """Определяет (база, размер, способ) для одного артикула Ozon."""
    offer = _clean(offer_id)
    if not offer:
        return "", "", "empty"
    wb_articles = wb_articles or {}
    aliases = aliases or {}
    native = native or {}

    base, size = split_tail(offer)

    # 1. Ручное соответствие: полный артикул, затем база.
    for key in (offer, base):
        hit = _clean(aliases.get(key) or aliases.get(key.lower()) or "")
        if hit:
            return hit, (size if key == base and hit != offer else ""), "alias"

    # 2. Нативная группировка Ozon, если появится.
    hit = _clean(native.get(offer) or native.get(offer.lower()) or "")
    if hit:
        tail = offer[len(hit):].lstrip("-") if offer.startswith(hit) else ""
        return hit, tail, "native"

    # 3. Поиск артикула WB: отбрасываем хвост до совпадения.
    walked = _wb_walk(offer, wb_articles)
    if walked is not None:
        return walked[0], walked[1], "wb"

    # 4. Хвост-размер без подтверждения карточкой WB.
    if looks_like_size(size):
        return base, size, "tail"

    # 5. Безразмерный товар: дефис в конце не является размером.
    return offer, "", "whole"


def load_wb_articles(db: Session) -> Dict[str, str]:
    """Множество артикулов WB (vendor_code) в нижнем регистре."""
    mp = db.execute(
        select(models.Marketplace.id).where(models.Marketplace.code == "wb")
    ).scalar_one_or_none()
    if mp is None:
        return {}
    out: Dict[str, str] = {}
    for (vc,) in db.execute(
        select(models.MarketplaceCard.vendor_code)
        .where(models.MarketplaceCard.marketplace_id == mp)
    ).all():
        code = _clean(vc)
        if code:
            out.setdefault(code.lower(), code)
    return out


def load_aliases(db: Session) -> Dict[str, str]:
    """Ручные соответствия «чужой артикул → канонический товар»."""
    out: Dict[str, str] = {}
    for a in db.execute(select(models.ProductAlias)).scalars().all():
        alias = _clean(a.alias_article)
        art = _clean(a.article)
        if alias and art:
            out[alias] = art
    return out


def load_ozon_offer_columns(db: Session) -> Dict[str, Tuple[str, str]]:
    """Заполненные base_article/size карточек Ozon (marketplace_cards)."""
    mp = db.execute(
        select(models.Marketplace.id).where(models.Marketplace.code == "ozon")
    ).scalar_one_or_none()
    if mp is None:
        return {}
    out: Dict[str, Tuple[str, str]] = {}
    for offer, base, size in db.execute(
        select(
            models.MarketplaceCard.vendor_code,
            models.MarketplaceCard.base_article,
            models.MarketplaceCard.size,
        ).where(models.MarketplaceCard.marketplace_id == mp)
    ).all():
        code = _clean(offer)
        if code:
            out.setdefault(code, (_clean(base), _clean(size)))
    return out


def build_offer_map(
    db: Session,
    offers: Optional[Iterable[str]] = None,
    use_cards: bool = True,
) -> Dict[str, Resolved]:
    """Карта offer_id → (база, размер, способ) для нужных артикулов Ozon.

    offers=None — разрешает все артикулы из карточек Ozon (marketplace_cards).
    Готовые base_article/size из карточек используются как есть, остальные
    считаются по правилам модуля.
    """
    wb_articles = load_wb_articles(db)
    aliases = load_aliases(db)
    cached: Dict[str, Tuple[str, str]] = load_ozon_offer_columns(db) if use_cards else {}

    if offers is None:
        wanted = list(cached.keys())
    else:
        wanted = []
        seen = set()
        for o in offers:
            key = _clean(o)
            if key and key not in seen:
                seen.add(key)
                wanted.append(key)
    if not wanted:
        return {}

    out: Dict[str, Resolved] = {}
    for offer in wanted:
        hit = cached.get(offer)
        if hit and (hit[0] or hit[1]):
            # Размер в карточке заполнен — база уже посчитана при загрузке.
            out[offer] = (hit[0] or offer, hit[1] or "", "cards")
            continue
        out[offer] = resolve_offer(offer, wb_articles, aliases)
    return out


def group_key(
    offer_id: str,
    offer_map: Dict[str, Resolved],
    by_size: bool = False,
) -> str:
    """Ключ группировки строки: база (по умолчанию) или полный артикул."""
    offer = _clean(offer_id)
    if not offer:
        return ""
    if by_size:
        return offer
    hit = offer_map.get(offer)
    return hit[0] if hit and hit[0] else offer


def size_of(offer_id: str, offer_map: Dict[str, Resolved]) -> str:
    offer = _clean(offer_id)
    hit = offer_map.get(offer)
    return hit[1] if hit else ""


def sizes_for(offers: Sequence[str], offer_map: Dict[str, Resolved]) -> List[str]:
    """Отсортированный список размеров по набору артикулов одного товара."""
    out: set = set()
    for offer in offers:
        s = size_of(offer, offer_map)
        if s:
            out.add(s)
    return sorted(out, key=lambda x: (not x.isdigit(), int(x) if x.isdigit() else 0, x))


# Таблицы, где артикул Ozon нужно разложить на базу и размер.
# stocks/price_snapshots общие с WB — фильтруются по маркетплейсу.
BACKFILL_TARGETS: Tuple[Tuple[str, str, str], ...] = (
    # (таблица, колонка с артикулом, SQL-фильтр «только Ozon»)
    ("ozon_detail_rows", "offer_id", ""),
    ("ozon_accruals", "offer_id", ""),
    ("ozon_placements", "offer_id", ""),
    ("ozon_buyouts", "offer_id", ""),
    ("marketplace_cards", "vendor_code",
     "marketplace_id = (SELECT id FROM marketplaces WHERE code = 'ozon')"),
    ("stocks", "article",
     "marketplace_id = (SELECT id FROM marketplaces WHERE code = 'ozon')"),
    ("price_snapshots", "article", "marketplace = 'ozon'"),
)


def collect_stored_offers(db: Session) -> List[str]:
    """Артикулы Ozon, реально встречающиеся в накопленных данных.

    Нужны в дополнение к карточкам: товар может продаваться/лежать на складе,
    ещё не попав в ``marketplace_cards`` (свежая поставка, архивная выгрузка).
    Без них бэкфилл оставляет ``base_article`` пустым и такие строки не
    сворачиваются в отчётах по товару.
    """
    found: set = set()
    for table, col, where in BACKFILL_TARGETS:
        sql = f"SELECT DISTINCT {col} FROM {table}"
        if where:
            sql += f" WHERE {where}"
        for (val,) in db.execute(text(sql)).all():
            key = _clean(val)
            if key:
                found.add(key)
    return sorted(found)


def backfill(db: Session, batch: int = 20000, verbose: bool = False) -> Dict[str, int]:
    """Заполняет base_article/size во всех накопленных данных Ozon.

    Идемпотентен: повторный вызов просто перезапишет те же значения.
    Возвращает словарь {таблица: число обновлённых строк}.
    """
    # Артикулы из карточек + те, что есть в данных, но ещё не в карточках.
    offer_map = build_offer_map(db)
    known = set(offer_map)
    extra = [o for o in collect_stored_offers(db) if o not in known]
    if extra:
        offer_map.update(build_offer_map(db, offers=extra))
    offers = list(offer_map.keys())
    bases = [offer_map[o][0] or o for o in offers]
    sizes = [offer_map[o][1] or "" for o in offers]

    stats: Dict[str, int] = {}
    for table, col, where in BACKFILL_TARGETS:
        total = 0
        for i in range(0, len(offers), batch):
            sl = slice(i, i + batch)
            sql = (
                f"UPDATE {table} AS t SET base_article = v.base, size = v.size "
                f"FROM unnest(CAST(:offers AS text[]), CAST(:bases AS text[]), "
                f"CAST(:sizes AS text[])) AS v(art, base, size) "
                f"WHERE t.{col} = v.art"
            )
            if where:
                sql += f" AND {where}"
            res = db.execute(text(sql), {
                "offers": offers[sl], "bases": bases[sl], "sizes": sizes[sl],
            })
            total += res.rowcount or 0
        db.commit()
        stats[table] = total
        if verbose:
            print(f"  {table}: {total}")
    return stats
