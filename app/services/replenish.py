"""Потребность в товаре (replenishment).

Сводит аналитику Прибыльности (детализации продаж WB/Ozon) с остатками
нашего склада и складов маркетплейсов и выдаёт рекомендации:

1. докупить у поставщика на наш склад (`need_buy`) — по суммарному спросу
   и целевому запасу в днях;
2. отгрузить на склады маркетплейсов (`ship_wb` / `ship_oz`) — из нашего
   остатка, по дефициту конкретного МП.

Спрос = продажи − возвраты за окно (нетто), в штуках в день. Товар без
карточки на WB/Ozon (сверка по артикулу и штрихкоду) помечается
неактуальным и из основной выдачи исключается.
"""

import math
import re
from datetime import timedelta
from typing import Optional

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.config import settings
from app.services import margin as margin_service
from app.services.common import like_match
from app.services.sync import _is_goods_row
from app.services.warehouse import stock_view

STATUS_LABELS = {
    "nostock": "Нет нигде",
    "urgent": "Срочно",
    "normal": "Норма",
    "inactive": "Неактуальный",
}


def _ceil(v: float) -> int:
    return 0 if v <= 0 else int(math.ceil(v))


def _num(v, default: float = 0.0) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return default


def _span_days(date_from, date_to) -> int:
    try:
        return max(1, (date_to - date_from).days + 1)
    except (TypeError, ValueError):
        return 1


def _mp_id(db, code: str):
    return db.execute(
        select(models.Marketplace.id).where(models.Marketplace.code == code)
    ).scalar_one_or_none()


def _detail_aggregates(db: Session, date_from, date_to):
    """Агрегаты по детализациям за окно (как в отчётах Прибыльности).

    Ozon — по размерам (by_size=True): в этом отчёте размер решает, что и в
    каком количестве закупать, а остатки/штрихкоды тоже хранятся по артикулу
    размера. Свёртка по товару живёт в отчётах Прибыльности и на дашборде.
    """
    wb_df = margin_service.margin_detail_dataframe(
        db, date_from=date_from, date_to=date_to, article_like=None,
        default_net_cost=settings.default_net_cost,
    )
    oz_df = margin_service.ozon_margin_detail_dataframe(
        db, date_from=date_from, date_to=date_to, article_like=None,
        default_net_cost=settings.default_net_cost, by_size=True,
    )
    return wb_df, oz_df


def _detail_maps(df: pd.DataFrame):
    """{UPPER(article): dict} из агрегата детализации."""
    out: dict = {}
    originals: dict = {}
    if df is None or df.empty:
        return out, originals
    for rec in df.to_dict("records"):
        art = str(rec.get("article", "") or "").strip()
        if not art:
            continue
        key = art.upper()
        out.setdefault(key, {
            "sells": 0, "returns_qty": 0, "income": 0.0, "revenue": 0.0,
            "margin": 0.0, "margin_per_one": 0.0, "margin_pct": 0.0,
            "net_cost_est": 0, "name": "",
        })
        d = out[key]
        d["sells"] += int(_num(rec.get("sells")))
        d["returns_qty"] += int(_num(rec.get("returns_qty")))
        d["income"] += _num(rec.get("income"))
        d["revenue"] += _num(rec.get("revenue"))
        d["margin"] += _num(rec.get("margin"))
        d["net_cost_est"] += int(_num(rec.get("net_cost_est")))
        name = str(rec.get("name") or "").strip()
        if name and not d["name"]:
            d["name"] = name
        originals.setdefault(key, art)
    for d in out.values():
        # удельные и проценты НЕ аддитивны: считаем заново по суммам строки
        d["margin_per_one"] = (round(d["margin"] / d["sells"], 2)
                               if d["sells"] else 0.0)
        d["margin_pct"] = (round(d["margin"] / d["revenue"] * 100, 1)
                           if d["revenue"] else 0.0)
    return out, originals


def _mp_stocks(db: Session, code: str, date_to):
    """Остатки МП на последнем срезе <= date_to.

    Возвращает (по_артикулам, по_размерам):
    {UPPER(article): [quantity_full, quantity, in_way]} и
    {(UPPER(article), UPPER(size)): [quantity_full, quantity, in_way, barcode]}.
    """
    mp = _mp_id(db, code)
    if mp is None:
        return {}, {}
    snap = db.execute(
        select(func.max(models.Stock.date)).where(
            models.Stock.marketplace_id == mp, models.Stock.date <= date_to,
        )
    ).scalar_one_or_none()
    if snap is None:
        return {}, {}
    by_art: dict = {}
    by_size: dict = {}
    rows = db.execute(
        select(models.Stock.article, models.Stock.size, models.Stock.barcode,
               models.Stock.quantity_full, models.Stock.quantity, models.Stock.in_way)
        .where(models.Stock.marketplace_id == mp, models.Stock.date == snap)
    ).all()
    for art, size, bc, qf, qty, way in rows:
        a = (art or "").strip().upper()
        if not a:
            continue
        cur = by_art.get(a, [0, 0, 0])
        cur[0] += int(qf or 0)
        cur[1] += int(qty or 0)
        cur[2] += int(way or 0)
        by_art[a] = cur
        s = (size or "").strip().upper()
        key = (a, s)
        cur2 = by_size.get(key, [0, 0, 0, ""])
        cur2[0] += int(qf or 0)
        cur2[1] += int(qty or 0)
        cur2[2] += int(way or 0)
        if not cur2[3] and bc:
            cur2[3] = str(bc).strip()
        by_size[key] = cur2
    return by_art, by_size


def _product_sizes(db: Session) -> dict:
    """{(UPPER(article), UPPER(size)): barcode} из каталога размеров."""
    out: dict = {}
    for art, size, bc in db.execute(select(
            models.ProductSize.article, models.ProductSize.size,
            models.ProductSize.barcode)).all():
        a = (art or "").strip().upper()
        s = (size or "").strip().upper()
        if not a:
            continue
        cur = out.setdefault((a, s), "")
        if not cur and bc:
            out[(a, s)] = str(bc).strip()
    return out


#: Окно скорости для плана подсортировки на WB. Короткое окно спроса не годится:
#: размер, который не продавался последние 30 дней, получает нулевую скорость,
#: цель 0 и выпадает из плана — хотя в карточке он есть и остатков на нём нет.
WB_SORT_VELOCITY_DAYS = 180

#: Окна скорости, доступные в выгрузке. 0 = вся история продаж.
WB_SORT_VELOCITY_WINDOWS = (180, 365, 0)

#: Сколько держать в размере, который есть в карточке, но пуст на WB и давно не
#: продавался. Без этого размера не видно в продаже на карточке товара, хотя он
#: там выбирается, и покупатель уходит. Правило осознанно грубое: одна штука —
#: это «размер не выглядит пустым», а не запас на продажи.
WB_SORT_MIN_SIZE_STOCK = 1


def _wb_card_sizes(db: Session) -> dict:
    """{UPPER(article): {UPPER(size): barcode}} — полный набор размеров карточки WB.

    ``ProductSize`` в базе пуста, а детализация продаж и остатки знают только те
    размеры, которые уже продавались или лежат на складе. Карточка WB —
    единственный источник полного списка размеров товара, поэтому именно она
    отвечает на вопрос «какие размеры есть у товара».
    """
    mp = _mp_id(db, "wb")
    out: dict = {}
    if mp is None:
        return out
    for vendor, size, bc in db.execute(
        select(models.MarketplaceCard.vendor_code, models.MarketplaceCard.size,
               models.MarketplaceCard.barcode)
        .where(models.MarketplaceCard.marketplace_id == mp)
    ).all():
        a = (vendor or "").strip().upper()
        if not a:
            continue
        s = (size or "").strip().upper()
        sizes = out.setdefault(a, {})
        # размер в список попадает всегда, штрихкод — бонусом: пустой barcode
        # не должен выкидывать размер из плана
        if s not in sizes:
            sizes[s] = ""
        if bc and not sizes[s]:
            sizes[s] = str(bc).strip()
    return out


def _size_sort_key(size: str) -> tuple:
    """Естественный порядок размеров: 9 < 10 < 42, XS < S < M < L."""
    chunks = re.findall(r"\d+|\D+", str(size or ""))
    return tuple((0, int(c), "") if c.isdigit() else (1, 0, c) for c in chunks)


def _wb_first_sale_dt(db: Session):
    """Самая ранняя дата продаж WB — начало окна «всё время»."""
    return db.execute(select(func.min(models.WbDetailRow.sale_dt))).scalar_one_or_none()


#: Рентабельность, при которой коэффициент подсорта достигает максимума (%).
PROFIT_FULL_PCT = 30.0
#: Максимальный коэффициент подсорта для прибыльного товара.
PROFIT_FACTOR_MAX = 2.0
#: Окно маржи по умолчанию: прибыльность всегда считаем за год, даже если
#: скорость берём «за всё время» — старые цены дают бесполезный процент.
PROFIT_MARGIN_DAYS = 365


def profit_factor(margin_pct: Optional[float]) -> float:
    """Плавный коэффициент подсорта по рентабельности товара, 0…2.

    Рентабельность уже посчитана с вычетом себестоимости (``margin`` в
    margin.py = income − логистика − хранение − услуги − net_cost × продажи),
    поэтому ``<= 0`` — это и есть убыток: такой товар не подсортировываем.
    Дальше линейный рост: 15 % → ×1.0, 30 % и выше → ×2.0. Без ступеней,
    чтобы не было «обрыва» на границах диапазонов.
    """
    if margin_pct is None:
        return 0.0
    pct = float(margin_pct)
    if pct <= 0:
        return 0.0
    return min(PROFIT_FACTOR_MAX, PROFIT_FACTOR_MAX * pct / PROFIT_FULL_PCT)


def wb_profit_factors(
    db: Session, date_to, velocity_days: int = WB_SORT_VELOCITY_DAYS,
    articles: Optional[list] = None, article_like: Optional[str] = None,
) -> dict:
    """Коэффициенты подсорта по артикулам: {UPPER(article): коэффициент}.

    Маржа берётся из того же расчёта, что и колонки «Маржа/шт» и
    «Рентабельность» в таблице, — иначе PDF и экран показывали бы разные
    числа. Окно маржи ограничено ``PROFIT_MARGIN_DAYS`` независимо от окна
    скорости.

    Артикулы без продаж в окне в словарь не попадают: у них нет ни маржи, ни
    скорости, и им достаётся только правило пола (1 шт в пустой размер).
    """
    days = int(velocity_days or 0)
    days = min(days, PROFIT_MARGIN_DAYS) if days > 0 else PROFIT_MARGIN_DAYS
    since = date_to - timedelta(days=days - 1)
    df = margin_service.margin_detail_dataframe(
        db, since, date_to, article_like=article_like,
        default_net_cost=settings.default_net_cost,
    )
    wanted = None
    if articles:
        wanted = {str(a or "").strip().upper() for a in articles}
    out: dict = {}
    for rec in df.to_dict("records"):
        art = str(rec.get("article") or "").strip().upper()
        if not art or (wanted is not None and art not in wanted):
            continue
        out[art] = profit_factor(rec.get("margin_pct"))
    return out


def wb_sorting_plan(
    db: Session, date_to, target_days: int = 30,
    articles: Optional[list] = None,
    velocity_days: int = WB_SORT_VELOCITY_DAYS,
    min_size_stock: int = WB_SORT_MIN_SIZE_STOCK,
    coverage_days: Optional[float] = None,
    profit_factor: Optional[dict] = None,
) -> dict:
    """План подсортировки на WB: сколько дослать в каждый размер карточки.

    По каждому размеру: остаток на WB (включая наш товар в пути), скорость
    продаж за окно скорости и сколько не хватает до целевого запаса.

    ``velocity_days`` = 0 означает «всю историю продаж» — полезно, когда товар
    продаётся сезонно и за 180 дней просто пусто. Размер, который есть в
    карточке, но пуст на WB и не продаётся, всё равно получает
    ``min_size_stock`` штук: иначе размер нельзя купить на карточке.

    ``coverage_days`` — сколько дней продаж держим на WB. По умолчанию
    ``target_days``. ``profit_factor`` — коэффициенты по артикулу из
    ``wb_profit_factors``: они домножаются к целевому уровню, поэтому
    убыточный товар (коэффициент 0) не подсортировывается вовсе, а прибыльный
    получает более глубокий запас. Для такого товара правило пола не
    применяется — иначе убыточный размер всё равно получил бы 1 штуку.

    ``articles`` = ``None`` — все артикулы, ``[]`` — ни одного.

    Ключи — те же ``UPPER``, что у остатков и продаж, чтобы размеры не
    «разъезжались» из-за регистра. Размеры, которых нет в карточке, но которые
    есть в продажах или остатках, тоже попадают в план: молча терять их нельзя.

    Возвращает ``{UPPER(article): {"article": …, "sizes": [...], "total": шт}}``.
    """
    target_days = max(1, int(target_days or 1))
    coverage = float(coverage_days) if coverage_days else float(target_days)
    coverage = max(0.25, coverage)
    factors = profit_factor or None
    days = int(velocity_days or 0)
    if days > 0:
        since = date_to - timedelta(days=days - 1)
    else:
        since = _wb_first_sale_dt(db) or date_to
    # делим на фактическую длину окна, а не на запрошенную: при «всё время»
    # это вся история продаж, и скорость должна быть шт/день за неё
    span = max(1, _span_days(since, date_to))

    cards = _wb_card_sizes(db)
    sales = _wb_sizes(db, since, date_to)
    _, stocks = _mp_stocks(db, "wb", date_to)

    keys = set(cards)
    keys.update(a for a, _s in sales)
    keys.update(a for a, _s in stocks)
    if articles is not None:
        # фильтр применяем к объединению, а не только к карточкам: иначе
        # артикул, у которого есть продажи, но нет карточки, проскочит мимо.
        # is not None, а не truthy: пустой список = «ничего не брать»
        keys &= {str(a or "").strip().upper() for a in articles}

    plan: dict = {}
    for art in sorted(keys):
        sizes = set(cards.get(art, {}))
        sizes.update(s for (a, s) in sales if a == art)
        sizes.update(s for (a, s) in stocks if a == art)
        card_bc = cards.get(art, {})
        k = 1.0 if factors is None else float(factors.get(art, 1.0))
        items = []
        total = 0
        for s in sorted(sizes, key=_size_sort_key):
            net = max(0, int((sales.get((art, s)) or {}).get("net") or 0))
            vel = net / span
            target = coverage * vel * k
            st = stocks.get((art, s), [0, 0, 0, ""])
            avail = int(st[0] or 0) + int(st[2] or 0)
            to_sort = max(0, _ceil(target) - avail)
            if to_sort == 0 and avail == 0 and min_size_stock > 0 and k > 0:
                to_sort = int(min_size_stock)
            total += to_sort

            item = {
                "size": s or "—",
                "barcode": (card_bc.get(s) or (sales.get((art, s)) or {}).get("barcode")
                            or st[3] or ""),
                "stock": int(st[0] or 0),
                "in_way": int(st[2] or 0),
                "avail": avail,
                "net": net,
                "vel": round(vel, 3),
                "target": round(target, 2),
                "to_sort": to_sort,
            }
            if factors is not None:
                # ключ есть только когда коэффициенты действительно применились:
                # по нему PDF понимает, что режим прибыльности включён
                item["factor"] = round(k, 2)
            items.append(item)
        entry = {"article": art, "sizes": items, "total": total}
        if factors is not None:
            entry["factor"] = round(k, 2)
        plan[art] = entry
    return plan


def apply_sort_budget(plan: dict, budgets: dict) -> dict:
    """Пересчитать «дослать» по размерам под бюджет из Excel («WB дефицит»).

    В режиме PDF из файла колонка «WB дефицит, шт» — это и есть «Итого
    дослать на WB» карточки, поэтому её значение задаёт бюджет по артикулу,
    а план остаётся источником только по тому, какие размеры и в каком
    порядке закрывать.

    Правила:

    * артикула нет в ``budgets`` (колонки в файле не было или ячейка пустая)
      либо бюджет равен плановому итогу — артикул не меняется;
    * бюджет меньше плана — «дослать» выдаётся в порядке скорости продаж
      (сначала продающиеся размеры), пока бюджет не исчерпан;
    * бюджет больше плана — остаток докидывается тем же порядком;
    * итог по артикулу всегда равен бюджету.

    ``budgets`` — ``{UPPER(article): int}``. Изменяет ``plan`` на месте.
    """
    for art, entry in plan.items():
        if art not in budgets or not entry.get("sizes"):
            continue
        items = entry["sizes"]
        base = [max(0, int(it.get("to_sort") or 0)) for it in items]
        total = sum(base)
        try:
            budget = max(0, int(budgets[art]))
        except (TypeError, ValueError):
            continue
        if budget == total:
            continue
        # Сортировка по убыванию скорости продаж; исходный порядок размеров
        # (alphabetical-numeric через _size_sort_key) сохраняется при равенстве.
        order = sorted(range(len(items)), key=lambda i: -(items[i].get("vel") or 0))
        if budget < total:
            new = [0] * len(items)
            rem = budget
            for i in order:
                if rem <= 0:
                    break
                give = min(base[i], rem)
                new[i] = give
                rem -= give
        else:
            new = list(base)
            extra = budget - total
            live = [i for i in order if (items[i].get("vel") or 0) > 0] or order
            j = 0
            while extra > 0:
                new[live[j % len(live)]] += 1
                extra -= 1
                j += 1
        for it, v in zip(items, new):
            it["to_sort"] = v
        entry["total"] = sum(new)
    return plan


def _wb_sizes(db: Session, date_from, date_to) -> dict:
    """Продажи WB по (артикул, размер): нетто = продажи − возвраты.

    Возвращает {(UPPER(article), UPPER(size)): {net, sells, ret, barcode}}.
    Баркод берём из sku строк WB-отчёта (там это штрихкод).
    """
    out: dict = {}
    rows = db.execute(
        select(models.WbDetailRow.article, models.WbDetailRow.tech_size,
               models.WbDetailRow.sku, models.WbDetailRow.doc_type_name,
               models.WbDetailRow.quantity, models.WbDetailRow.retail_amount,
               models.WbDetailRow.for_pay)
        .where(models.WbDetailRow.sale_dt >= date_from,
               models.WbDetailRow.sale_dt <= date_to)
    ).all()
    for art, size, sku, dtn, qty, ra, fp in rows:
        a = (art or "").strip().upper()
        if not a or not _is_goods_row(dtn, ra, fp):
            continue
        s = (size or "").strip().upper()
        q = int(qty or 0)
        is_ret = ("возврат" in str(dtn or "").lower()
                  or "return" in str(dtn or "").lower())
        key = (a, s)
        d = out.setdefault(key, {"net": 0, "sells": 0, "ret": 0, "barcode": ""})
        d["sells"] += q if not is_ret else 0
        d["ret"] += q if is_ret else 0
        d["net"] += -q if is_ret else q
        if not d["barcode"] and sku:
            d["barcode"] = str(sku).strip()
    return out


def _cards_maps(db: Session):
    """Карточки по маркетплейсам: {code: (set артикулов, set баркодов)}."""
    by_code: dict = {"wb": {"vendor": set(), "barcode": set()},
                     "ozon": {"vendor": set(), "barcode": set()}}
    code_by_id = {}
    for m in db.execute(select(models.Marketplace.code, models.Marketplace.id)).all():
        code_by_id[m[1]] = m[0]
    rows = db.execute(select(
        models.MarketplaceCard.marketplace_id,
        models.MarketplaceCard.vendor_code,
        models.MarketplaceCard.barcode,
    )).all()
    for mp_id, vendor, barcode in rows:
        code = code_by_id.get(mp_id)
        if code not in by_code:
            continue
        v = str(vendor or "").strip().upper()
        b = str(barcode or "").strip().upper()
        if v:
            by_code[code]["vendor"].add(v)
        if b:
            by_code[code]["barcode"].add(b)
    return by_code


def _our_stock_map(db: Session):
    """Остатки нашего склада: {UPPER(article): balance} (округлено до целых)."""
    out: dict = {}
    for r in stock_view(db):
        out[(r["article"] or "").strip().upper()] = int(round(r["balance"]))
    return out


def replenish_rows(
    db: Session,
    date_from,
    date_to,
    target_days: int = 30,
    span_days: Optional[int] = None,
    marketplace: Optional[str] = None,
    sort: str = "urgency",
    article_like: Optional[str] = None,
    show_inactive: bool = False,
    view: str = "article",
) -> dict:
    """Главный расчёт. Возвращает {'rows': [...], 'meta': {...}}.

    view='article' — одна строка на артикул; view='sizes' — плоский разрез
    по размерам (WB-продажи по tech_size + остатки складов МП по размерам;
    Ozon-спрос/покупка остаются на уровне артикула).
    """
    span = span_days or _span_days(date_from, date_to)
    target_days = max(1, int(target_days or 30))
    codes = ["wb", "ozon"]
    if marketplace:
        mp_filter = str(marketplace).strip().lower()
        if mp_filter and mp_filter not in ("", "all"):
            codes = [c for c in codes if c == mp_filter]
    if not codes:
        codes = ["wb"]
    if str(view).strip().lower() == "sizes":
        return _size_view(
            db, date_from, date_to, target_days=target_days, span=span,
            codes=codes, sort=sort, article_like=article_like,
            show_inactive=show_inactive,
        )

    wb_df, oz_df = _detail_aggregates(db, date_from, date_to)
    detail_maps = {"wb": {}, "ozon": {}}
    originals: dict = {}
    for code, df in (("wb", wb_df), ("ozon", oz_df)):
        if code not in codes:
            continue
        detail_maps[code], orig = _detail_maps(df)
        for k, v in orig.items():
            originals.setdefault(k, v)

    our_map = _our_stock_map(db)
    stock_maps = {}
    for c in ("wb", "ozon"):
        by_art, _by_sz = _mp_stocks(db, c, date_to)
        stock_maps[c] = by_art if c in codes else {}
    cards = _cards_maps(db)

    keys = set(originals)
    keys |= set(our_map)
    for m in stock_maps.values():
        keys |= set(m)
    for m in detail_maps.values():
        keys |= set(m)
    if article_like:
        keys = {k for k in keys if like_match(k, article_like)}

    if not keys:
        return {"rows": [], "meta": _meta(0)}

    prods = db.execute(
        select(models.Product).where(models.Product.article.in_(sorted(keys)))
    ).scalars().all()
    products = {p.article.strip().upper(): p for p in prods if (p.article or "").strip()}

    rows = []
    for key in sorted(keys):
        raw_k = originals.get(key, key)
        prod = products.get(key)

        wb_d = detail_maps.get("wb", {}).get(key, {})
        oz_d = detail_maps.get("ozon", {}).get(key, {})
        wb_s = stock_maps.get("wb", {}).get(key, [0, 0, 0])
        oz_s = stock_maps.get("ozon", {}).get(key, [0, 0, 0])

        sell_wb = int(_num(wb_d.get("sells")))
        ret_wb = int(_num(wb_d.get("returns_qty")))
        sell_oz = int(_num(oz_d.get("sells")))
        ret_oz = int(_num(oz_d.get("returns_qty")))

        # WB: sells уже нетто (продажи минус возвраты, знак учтён). Ozon:
        # sells — гросс, возвраты отдельной колонкой return_qty.
        net_wb = max(0, sell_wb)
        net_oz = max(0, sell_oz - ret_oz)
        net = net_wb + net_oz
        vel_wb = net_wb / span
        vel_oz = net_oz / span
        vel = net / span

        our = int(_num(our_map.get(key)))
        wb_qf, wb_q, wb_w = wb_s[0], wb_s[1], wb_s[2]
        oz_qf, oz_q, oz_w = oz_s[0], oz_s[1], oz_s[2]
        wb_avail = wb_qf + wb_w
        oz_avail = oz_qf + oz_w
        wb_all = wb_q + wb_w
        oz_all = oz_q + oz_w

        wb_doc = round(wb_avail / vel_wb, 1) if vel_wb > 0 else None
        oz_doc = round(oz_avail / vel_oz, 1) if vel_oz > 0 else None
        wb_def = _ceil(target_days * vel_wb - wb_avail)
        oz_def = _ceil(target_days * vel_oz - oz_avail)

        # Актуальность: карточка на одном из МП (артикул или баркод).
        actual_mp = []
        for code in ("wb", "ozon"):
            if code not in codes:
                continue
            vendor = cards[code]["vendor"]
            barcode = cards[code]["barcode"]
            if key in vendor:
                actual_mp.append(code)
                continue
            bc = (prod.barcode or "").strip().upper() if prod else ""
            if bc and bc in barcode:
                actual_mp.append(code)
        actual = bool(actual_mp)

        # Отгрузка с нашего склада: покрываем дефициты МП, приоритет —
        # худшему DOC, при равенстве — более маржинальному.
        defs = []
        for code, df_, d_ in (("wb", wb_d, wb_def), ("ozon", oz_d, oz_def)):
            if code not in codes:
                continue
            doc = wb_doc if code == "wb" else oz_doc
            merit = _num(df_.get("margin_per_one"))
            defs.append((code, d_, doc, merit))
        defs = [x for x in defs if x[1] > 0]
        defs.sort(key=lambda x: (x[2] if x[2] is not None else 1e12, -x[3]))
        rem = max(0, our)
        ship = {"wb": 0, "ozon": 0}
        for code, d_, _doc, _m in defs:
            s = min(d_, rem)
            ship[code] = s
            rem -= s

        sys_stock = our + wb_all + oz_all
        need_buy = _ceil(target_days * vel - sys_stock)

        margin_total = _num(wb_d.get("margin")) + _num(oz_d.get("margin"))
        income_total = _num(wb_d.get("income")) + _num(oz_d.get("income"))
        margin_per_one = (
            round(margin_total / net, 2) if net > 0 else 0.0
        )
        margin_pct = round(margin_total / income_total * 100, 1) if income_total > 0 else 0.0
        ret_total = ret_wb + ret_oz
        denom = sell_wb + sell_oz + ret_total
        return_rate = round(ret_total / denom * 100, 1) if denom > 0 else 0.0

        if not actual:
            status = "inactive"
        elif net <= 0:
            status = "normal"
        elif our <= 0 and wb_all + oz_all <= 0:
            status = "nostock"
        elif need_buy > 0 or ship["wb"] > 0 or ship["ozon"] > 0:
            status = "urgent"
        else:
            status = "normal"

        rows.append({
            "article": prod.article if prod and prod.article else raw_k,
            "name": (prod.name if prod and (prod.name or "").strip()
                     else wb_d.get("name") or oz_d.get("name") or ""),
            "barcode": (prod.barcode if prod else "") or "",
            "actual": actual,
            "actual_mp": ",".join(actual_mp),
            "replenishable": (bool(prod.replenishable) if prod is not None else None),
            "window_days": span,
            "demand": round(vel, 2), "demand_wb": round(vel_wb, 2),
            "demand_oz": round(vel_oz, 2), "return_rate": return_rate,
            "sells": sell_wb + sell_oz, "returns_qty": ret_total,
            # Продано WB за окно, нетто: sell_wb в детализации WB уже нетто
            # (возвраты вычтены), net_wb дополнительно отсекает возвратные минусы.
            "wb_sells": int(net_wb),
            "our_stock": our,
            "our_cost": round(_num(prod.net_cost if prod else 0), 2),
            "wb_qty": wb_q, "wb_avail": wb_avail, "wb_in_way": wb_w,
            "wb_doc": wb_doc, "wb_def": wb_def,
            "oz_qty": oz_q, "oz_avail": oz_avail, "oz_in_way": oz_w,
            "oz_doc": oz_doc, "oz_def": oz_def,
            "ship_wb": ship["wb"], "ship_oz": ship["ozon"],
            "need_buy": need_buy,
            "income": round(income_total, 2), "margin": round(margin_total, 2),
            "margin_per_one": margin_per_one, "margin_pct": margin_pct,
            "net_cost_est": int(_num(wb_d.get("net_cost_est")) + _num(oz_d.get("net_cost_est"))),
            "status": status,
            "status_label": STATUS_LABELS.get(status, status),
        })

    # Сортировка
    if sort == "margin":
        rows.sort(key=lambda r: (r["margin_per_one"], r["demand"]), reverse=True)
    elif sort == "name":
        rows.sort(key=lambda r: r["article"].lower())
    else:  # urgency
        rank = {"nostock": 0, "urgent": 1, "normal": 2, "inactive": 3}
        rows.sort(key=lambda r: (
            rank.get(r["status"], 3),
            -(r["need_buy"] + r["ship_wb"] + r["ship_oz"]),
            -r["demand"],
        ))

    inactive_total = 0
    if not show_inactive:
        kept = []
        for r in rows:
            if r["status"] == "inactive":
                inactive_total += 1
            else:
                kept.append(r)
        rows = kept

    meta = _meta(len(rows), rows, target_days, span, codes)
    if inactive_total:
        meta["inactive_total"] = inactive_total
    return {"rows": rows, "meta": meta}


def _size_view(
    db: Session, date_from, date_to, target_days: int = 30, span: int = 1,
    codes=None, sort: str = "urgency", article_like: Optional[str] = None,
    show_inactive: bool = False,
) -> dict:
    """Плоский разрез «по размерам».

    Строка — (артикул, размер). WB-спрос по tech_size детализации (реальный),
    остатки МП — по размеру из stocks, штрихкод — из каталога размеров /
    строк WB / стоков. Ozon-спрос и «купить у поставщика» считаются только
    на уровне артикула (размера в продажах Ozon нет) — в этом виде не
    выводятся (meta.need_total = 0, Ozon в колонках «—»).
    """
    codes = codes or ["wb", "ozon"]
    wb_df, oz_df = _detail_aggregates(db, date_from, date_to)
    detail_maps = {"wb": {}, "ozon": {}}
    originals: dict = {}
    for code, df in (("wb", wb_df), ("ozon", oz_df)):
        if code not in codes:
            continue
        detail_maps[code], orig = _detail_maps(df)
        for k, v in orig.items():
            originals.setdefault(k, v)

    our_map = _our_stock_map(db)
    art_stocks: dict = {}
    sz_stocks: dict = {}
    for c in codes:
        art_stocks[c], sz_stocks[c] = _mp_stocks(db, c, date_to)
    cards = _cards_maps(db)
    prodsizes = _product_sizes(db)
    wbsizes = _wb_sizes(db, date_from, date_to) if "wb" in codes else {}

    keys = set()
    for a, _s in prodsizes:
        keys.add(a)
    for a, _s in wbsizes:
        keys.add(a)
    for c in codes:
        keys.update(art_stocks[c])
    if article_like:
        keys = {k for k in keys if like_match(k, article_like)}
    if not keys:
        return {"rows": [], "meta": _meta(0)}

    prods = db.execute(
        select(models.Product).where(models.Product.article.in_(sorted(keys)))
    ).scalars().all()
    products = {p.article.strip().upper(): p for p in prods if (p.article or "").strip()}

    rows = []
    for key in sorted(keys):
        prod = products.get(key)
        wb_d = detail_maps.get("wb", {}).get(key, {})
        oz_d = detail_maps.get("ozon", {}).get(key, {})
        our = int(_num(our_map.get(key)))

        actual_mp = []
        for code in codes:
            vendor = cards[code]["vendor"]
            barcode = cards[code]["barcode"]
            if key in vendor:
                actual_mp.append(code)
                continue
            bc = (prod.barcode or "").strip().upper() if prod else ""
            if bc and bc in barcode:
                actual_mp.append(code)
        actual = bool(actual_mp)

        a_sells_wb = int(_num(wb_d.get("sells")))       # WB sells уже нетто
        a_sells_oz = int(_num(oz_d.get("sells")))
        a_ret_oz = int(_num(oz_d.get("returns_qty")))
        a_net = max(0, a_sells_wb) + max(0, a_sells_oz - a_ret_oz)
        margin_total = _num(wb_d.get("margin")) + _num(oz_d.get("margin"))
        margin_per_one = round(margin_total / a_net, 2) if a_net > 0 else 0.0
        margin_pct = _num(wb_d.get("margin_pct")) or _num(oz_d.get("margin_pct"))

        sizes = set()
        for (a, s) in prodsizes:
            if a == key:
                sizes.add(s)
        for (a, s) in wbsizes:
            if a == key:
                sizes.add(s)
        for c in codes:
            for (a, s) in sz_stocks[c]:
                if a == key:
                    sizes.add(s)
        if not sizes:
            sizes = {""}

        # Расчёт по каждому размеру WB-части + аллокация нашего склада
        size_defs = []
        for s in sorted(sizes, key=lambda x: (x == "", x)):
            wbs = wbsizes.get((key, s), {"net": 0, "sells": 0, "ret": 0, "barcode": ""})
            wb_s = sz_stocks.get("wb", {}).get((key, s), [0, 0, 0, ""])
            wb_avail = wb_s[0] + wb_s[2]
            vel = wbs["net"] / span if span > 0 else 0.0
            doc = round(wb_avail / vel, 1) if vel > 0 else None
            df_ = _ceil(target_days * vel - wb_avail)
            size_defs.append((s, wbs, wb_s, wb_avail, vel, doc, df_))

        rem = max(0, our)
        ship = {}
        for s, wbs, wb_s, wb_avail, vel, doc, df_ in size_defs:
            ship[s] = 0
        for s, wbs, wb_s, wb_avail, vel, doc, df_ in \
                sorted(size_defs,
                       key=lambda x: (x[5] if x[5] is not None else 1e12, -x[4])):
            give = min(df_, rem)
            ship[s] = give
            rem -= give
        # Остаток склада (после WB-дефицитов) не делим по размерам —
        # он отражается на строке артикула (Ozon/резерв).

        for s, wbs, wb_s, wb_avail, vel, doc, df_ in size_defs:
            if not actual:
                status = "inactive"
            elif wbs["net"] <= 0:
                status = "normal"
            elif wb_avail <= 0 and our <= 0:
                status = "nostock"
            elif df_ > 0 or ship[s] > 0:
                status = "urgent"
            else:
                status = "normal"
            rows.append({
                "article": prod.article if prod and prod.article else key,
                "size": s or "—",
                "barcode": (prodsizes.get((key, s)) or wbs.get("barcode")
                            or wb_s[3] or ""),
                "name": (prod.name if prod and (prod.name or "").strip()
                         else wb_d.get("name") or oz_d.get("name") or ""),
                "actual": actual,
                "actual_mp": ",".join(actual_mp),
                "status": status,
                "status_label": STATUS_LABELS.get(status, status),
                "wb_sells": wbs["sells"], "wb_ret": wbs["ret"],
                "wb_net": wbs["net"], "wb_vel": round(vel, 2),
                "wb_qty": wb_s[1], "wb_avail": wb_avail, "wb_in_way": wb_s[2],
                "wb_doc": doc, "wb_def": df_, "ship_wb": ship[s],
                "our_stock": our,
                "margin_per_one": margin_per_one, "margin_pct": margin_pct,
                "margin": round(margin_total, 2),
            })

    if sort == "margin":
        rows.sort(key=lambda r: (r["margin_per_one"], r["wb_vel"]), reverse=True)
    elif sort == "name":
        rows.sort(key=lambda r: (r["article"].lower(), r["size"].lower()))
    else:  # urgency
        rank = {"nostock": 0, "urgent": 1, "normal": 2, "inactive": 3}
        rows.sort(key=lambda r: (
            rank.get(r["status"], 3),
            -(r["wb_def"] + r["ship_wb"]),
            r["article"].lower(),
            r["size"].lower(),
        ))

    inactive_total = 0
    if not show_inactive:
        kept = []
        for r in rows:
            if r["status"] == "inactive":
                inactive_total += 1
            else:
                kept.append(r)
        rows = kept

    meta = _meta(len(rows), rows, target_days, span, codes, unit="sizes")
    if inactive_total:
        meta["inactive_total"] = inactive_total
    return {"rows": rows, "meta": meta}


def _meta(count: int, rows=None, target_days: int = 30, span: int = 1, codes=None, unit: str = "article"):
    if not rows:
        rows = []
    counts = {"nostock": 0, "urgent": 0, "normal": 0, "inactive": 0}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {
        "count": count,
        "target_days": target_days,
        "span_days": span,
        "codes": codes or ["wb", "ozon"],
        "unit": unit,
        **counts,
        "need_total": sum(r.get("need_buy", 0) for r in rows),
        "ship_total": sum(r.get("ship_wb", 0) + r.get("ship_oz", 0) for r in rows),
        "demand_total": round(sum(r.get("demand", 0) for r in rows), 2),
        "our_stock_total": sum(r.get("our_stock", 0) for r in rows),
        "margin_total": round(sum(r.get("margin", 0) for r in rows), 2),
    }