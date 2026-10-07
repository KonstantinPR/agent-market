"""Расчёт маржинальности по детализациям продаж WB/Ozon (detail-отчёты) и воронке WB."""
from datetime import date
from typing import Optional

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.services import funnel as funnel_service
from app.services import ozon_article
from app.services.common import like_col, like_re
from app.services.sync import _is_goods_row, _redistribute_articleless, storage_split, marketplace_id


def funnel_dataframe(
    db: Session,
    date_from=None,
    date_to=None,
    article_like: Optional[str] = None,
) -> pd.DataFrame:
    """Прибыльность по Воронке Продаж WB (funnel_metric + себестоимость).

    Воронка WB — срез метрик за период (не по дням), поэтому выбирается один
    срез через pick_funnel_window (как в WB API → Воронка продаж): точное окно →
    самый широкий внутри запрошенного → самый свежий пересекающийся → последний
    в базе. Маржа — ОЦЕНКА «до расходов WB»: выручка (или avg_price × orders)
    минус себестоимость (net_cost × orders); комиссий/логистики/хранения WB
    в воронке нет. Хранение (storage_est) считается отдельно по детализации и
    в маржу не входит. article_like фильтрует только вывод и не влияет на
    агрегацию. В attrs: date_from/date_to — показанный срез, matched — совпал ли
    срез с запрошенным диапазоном.
    """
    base_cols = [c for c in funnel_service.funnel_columns()
                 if c not in ("past_json", "comparison_json")]
    extra_cols = ["name", "cart_pct", "order_pct", "net_cost", "margin", "margin_pct",
                  "storage_est"]
    all_cols = (base_cols + extra_cols
                + ["past_" + k for k in funnel_service.FUNNEL_PAST_KEYS]
                + ["dy_" + k for k in funnel_service.FUNNEL_DY_KEYS])

    def _empty():
        return pd.DataFrame(columns=all_cols)

    def _num(v):
        try:
            f = float(v)
            return int(f) if f.is_integer() else f
        except (TypeError, ValueError):
            return 0

    # 1. Выбор среза: как в /api/funnel (pick_funnel_window).
    latest = db.execute(
        select(models.FunnelMetric.date_from, models.FunnelMetric.date_to)
        .order_by(models.FunnelMetric.date_to.desc(), models.FunnelMetric.date_from.desc())
        .limit(1)
    ).first()
    if latest is None:
        return _empty()
    windows = [
        (r[0], r[1])
        for r in db.execute(
            select(models.FunnelMetric.date_from, models.FunnelMetric.date_to).distinct()
        )
    ]
    requested = None
    if date_from is not None and date_to is not None:
        requested = (date_from, date_to)
        chosen = funnel_service.pick_funnel_window(windows, requested[0], requested[1])
        from_, to_ = chosen if chosen is not None else (latest[0], latest[1])
    else:
        from_, to_ = latest[0], latest[1]

    name_subq = (
        select(models.Product.name)
        .where(models.Product.article == models.FunnelMetric.article)
        .limit(1)
        .scalar_subquery()
    )
    cost_subq = (
        select(models.Product.net_cost)
        .where(models.Product.article == models.FunnelMetric.article)
        .limit(1)
        .scalar_subquery()
    )
    q = (
        select(
            models.FunnelMetric,
            name_subq.label("p_name"),
            cost_subq.label("p_cost"),
        )
        .where(models.FunnelMetric.date_from == from_,
               models.FunnelMetric.date_to == to_)
        .order_by(models.FunnelMetric.revenue.desc(), models.FunnelMetric.article)
    )

    storage_est_map = storage_split(db, date_from=from_, date_to=to_)

    art_check = like_re(article_like)
    seen = set()
    recs = []
    for fm, p_name, p_cost in db.execute(q):
        if fm.article in seen:
            continue
        seen.add(fm.article)
        r = {
            "nm_id": _num(fm.nm_id or 0),
            "article": str(fm.article or ""),
            "views": _num(fm.views or 0),
            "opens": _num(fm.opens or 0),
            "adds": _num(fm.adds or 0),
            "orders": _num(fm.orders or 0),
            "cancelled": _num(fm.cancelled or 0),
            "buyouts": _num(fm.buyouts or 0),
            "avg_price": _num(fm.avg_price or 0),
            "revenue": _num(fm.revenue or 0),
            "buyout_sum": _num(fm.buyout_sum or 0),
            "subject_name": str(fm.subject_name or ""),
            "brand_name": str(fm.brand_name or ""),
            "product_rating": _num(fm.product_rating or 0),
            "feedback_rating": _num(fm.feedback_rating or 0),
            "stock_wb": _num(fm.stock_wb or 0),
            "stock_mp": _num(fm.stock_mp or 0),
            "stock_balance_sum": _num(fm.stock_balance_sum or 0),
            "cancel_sum": _num(fm.cancel_sum or 0),
            "avg_orders_per_day": _num(fm.avg_orders_per_day or 0),
            "share_order_percent": _num(fm.share_order_percent or 0),
            "add_to_wishlist": _num(fm.add_to_wishlist or 0),
            "time_to_ready_min": _num(fm.time_to_ready_min or 0),
            "localization_percent": _num(fm.localization_percent or 0),
            "conv_to_cart_percent": _num(fm.conv_to_cart_percent or 0),
            "conv_cart_to_order_percent": _num(fm.conv_cart_to_order_percent or 0),
            "conv_buyout_percent": _num(fm.conv_buyout_percent or 0),
            "wb_club_order_count": _num(fm.wb_club_order_count or 0),
            "wb_club_order_sum": _num(fm.wb_club_order_sum or 0),
            "wb_club_buyout_count": _num(fm.wb_club_buyout_count or 0),
            "wb_club_buyout_sum": _num(fm.wb_club_buyout_sum or 0),
            "wb_club_cancel_count": _num(fm.wb_club_cancel_count or 0),
            "wb_club_cancel_sum": _num(fm.wb_club_cancel_sum or 0),
            "wb_club_avg_price": _num(fm.wb_club_avg_price or 0),
            "wb_club_buyout_percent": _num(fm.wb_club_buyout_percent or 0),
            "wb_club_avg_orders_per_day": _num(fm.wb_club_avg_orders_per_day or 0),
            "title": str(fm.title or ""),
            "subject_id": _num(fm.subject_id or 0),
            "tags": str(fm.tags or ""),
            "past_json": str(fm.past_json or ""),
            "comparison_json": str(fm.comparison_json or ""),
        }
        funnel_service.flatten_past_dy(r)
        name = str(fm.title or p_name or "")
        orders = int(r["orders"])
        net_cost = float(p_cost or 0)
        revenue_eff = float(r["revenue"]) or float(r["avg_price"]) * orders
        margin = round(revenue_eff - net_cost * orders, 2)
        r.update({
            "name": name,
            "cart_pct": round((r["adds"] / r["views"] * 100) if r["views"] else 0.0, 2),
            "order_pct": round((orders / r["views"] * 100) if r["views"] else 0.0, 2),
            "net_cost": net_cost,
            "margin": margin,
            "margin_pct": round(margin / revenue_eff * 100, 2) if revenue_eff else 0.0,
            "storage_est": round(storage_est_map.get(
                r["article"].strip().upper(), 0.0), 2),
        })
        recs.append(r)

    if art_check is not None:
        recs = [r for r in recs if art_check.search(r["article"])]
    for r in recs:
        for c in all_cols:
            if c not in r:
                r[c] = 0
    out = pd.DataFrame(recs, columns=all_cols)
    if not out.empty:
        out = out.sort_values("margin", ascending=False).reset_index(drop=True)
    out.attrs["date_from"] = str(from_)
    out.attrs["date_to"] = str(to_)
    out.attrs["matched"] = bool(requested) and (from_, to_) == requested
    return out


DETAIL_MARGIN_COLUMNS = [
    "article", "nm_id", "name", "sells", "returns_qty",
    "stock_qty", "stock_total", "stock_in_way",
    "revenue", "commission", "logistics", "logistics_out", "logistics_in",
    "storage", "services", "income", "margin_gross", "net_cost", "net_cost_est",
    "margin", "margin_per_one", "margin_pct", "margin_pct_income",
    "commission_per_one", "logistics_per_one",
    "logistics_out_per_one", "logistics_in_per_one",
    "storage_per_one", "income_per_one", "revenue_per_one",
    "margin_gross_per_one", "return_rate",
]


def _wb_stock_map(db: Session, date_to) -> dict:
    """Остатки WB на конец окна: {UPPER(article): (quantity, quantity_full, in_way)}.

    Срез стоков — последняя дата <= date_to (строки «ВБ Остатки» по дням);
    если такого среза нет — возвращается {} (нули в колонках). Количество
    суммируется по всем складам и chrt_id артикула (в детализации продаж
    артикул уже агрегирован).
    """
    if isinstance(date_to, str):
        date_to = date.fromisoformat(date_to)
    mp = marketplace_id(db, "wb")
    snap = db.scalar(
        select(func.max(models.Stock.date)).where(
            models.Stock.marketplace_id == mp,
            models.Stock.date <= date_to,
        )
    )
    if snap is None:
        return {}
    out: dict = {}
    q = (
        select(
            models.Stock.article,
            func.sum(models.Stock.quantity).label("q"),
            func.sum(models.Stock.quantity_full).label("qf"),
            func.sum(models.Stock.in_way).label("w"),
        )
        .where(models.Stock.date == snap, models.Stock.marketplace_id == mp)
        .group_by(models.Stock.article)
    )
    for art, qty, full, way in db.execute(q):
        a = (art or "").strip().upper()
        if a:
            out[a] = (int(qty or 0), int(full or 0), int(way or 0))
    return out


def margin_detail_dataframe(
    db: Session,
    date_from=None,
    date_to=None,
    article_like: Optional[str] = None,
    default_net_cost: float = 0.0,
) -> pd.DataFrame:
    """Прибыльность по «Детализации продаж» WB напрямую из wb_detail_rows.

    Одна строка — операция (SRID). Возврат («Возврат» в типе документа)
    вычитается из выручки/комиссии/«к перечислению»; расходы WB (логистика,
    хранение, услуги) всегда остаются расходами, знак не меняют.
    Себестоимость: из каталога products — матчинг UPPER(article) (артикулы
    в products.xlsx в верхнем регистре, в детализации — как в файле), при
    промахе — фолбэк по barcode строк детализации (sku -> products.barcode).
    Если себестоимость не найдена/0 — берётся default_net_cost,
    и флаг net_cost_est=True помечает товар как «оценку».

    Формула: margin_gross (Маржа, до себестоимости) = income (к перечислению,
    с учётом знака возвратов) − логистика − хранение − услуги;
    margin (Маржа-себест.) = margin_gross − net_cost × продано.
    margin_pct — рентабельность от выручки (реализовано WB).

    Дополнительные аналитические поля:
    logistics_out/logistics_in — логистика туда/обратно (по типу строки);
    *_per_one — деление на количество проданных (sells);
    return_rate — % возвратов от общего количества (продажи + возвраты).

    article_like фильтрует только вывод: доли безартикульных плат (хранение,
    логистика, услуги) всегда считаются по всему окну периода и не меняются
    от фильтра в строке поиска.
    """
    cols = ["article", "nm_id", "name", "sells", "returns_qty",
            "stock_qty", "stock_total", "stock_in_way",
            "revenue", "commission", "logistics", "logistics_out", "logistics_in",
            "storage", "services", "income", "margin_gross", "net_cost",
            "net_cost_est", "margin", "margin_per_one", "margin_pct", "margin_pct_income",
            "commission_per_one", "logistics_per_one",
            "logistics_out_per_one", "logistics_in_per_one",
            "storage_per_one", "income_per_one", "revenue_per_one",
            "margin_gross_per_one", "return_rate"]
    out = pd.DataFrame(columns=cols)

    q = select(models.WbDetailRow)
    if date_from:
        q = q.where(models.WbDetailRow.sale_dt >= date_from)
    if date_to:
        q = q.where(models.WbDetailRow.sale_dt <= date_to)
    rows = list(db.execute(q).scalars().all())
    if not rows:
        return out

    # Фильтр поиска применяется только к выводу (см. like_re): распределение
    # безартикульных плат «Хранение», логистики и услуг не пересчитывается под
    # фильтр в строке поиска — доли считаются по всему окну периода.
    art_check = like_re(article_like)

    # Распределение безартикульных плат «Хранение» по товарам продаж пропорц.
    # «объём × тариф × (остаток + проданное×0.5)» (storage_costs × stocks +
    # продажи периода). Распределение идёт по всем товарам окна; отфильтрованный
    # вид показывает только их доли.
    storage_est = storage_split(
        db,
        date_from=date_from,
        date_to=date_to,
    )

    # Остатки WB на конец окна: последний срез стоков <= date_to.
    stock_map = _wb_stock_map(db, date_to=date_to) if date_to else {}

    # Два запроса вместо OR: по OR Postgres не применяет план к UPPER(...)
    # (Seq Scan + сравнение с массивом ~16k элементов ≈ 9 с на окне 365 дней),
    # раздельные IN отдают тот же набор строк за ~0.2 с.
    arts_up = {(r.article or "").strip().upper() for r in rows if (r.article or "").strip()}
    skus_up = {(r.sku or "").strip().upper() for r in rows if (r.sku or "").strip()}
    prods = []
    if arts_up:
        prods += db.execute(select(models.Product).where(
            func.upper(models.Product.article).in_(arts_up)
        )).scalars().all()
    if skus_up:
        prods += db.execute(select(models.Product).where(
            func.upper(func.coalesce(models.Product.barcode, "")).in_(skus_up)
        )).scalars().all()
    # Себестоимость: UPPER(article) -> product; если артикул не матчится по
    # регистру — пробуем barcode от любой строки этого артикула (sku-колонка).
    prods_by_upper = {p.article.strip().upper(): p for p in prods}
    prods_by_barcode = {p.barcode.strip().upper(): p for p in prods if (p.barcode or "").strip()}
    art_skus: dict = {}
    for r in rows:
        art = (r.article or "").strip()
        sku = (r.sku or "").strip().upper()
        # sku из Excel хранится как float ('2047932869792.0')
        if sku.endswith(".0"):
            sku = sku[:-2]
        if art and sku and sku in prods_by_barcode:
            art_skus.setdefault(art.upper(), set())
            art_skus[art.upper()].add(sku)

    def _find_product(art: str):
        """Находит Product для артикула: точный/UPPER, затем любой barcode строк."""
        p = prods_by_upper.get(art.strip().upper())
        if p is not None:
            return p
        for sku in art_skus.get(art.strip().upper(), set()):
            q = prods_by_barcode.get(sku)
            if q is not None:
                return q
        return None

    def is_ret(t) -> bool:
        return "возврат" in str(t or "").lower() or "return" in str(t or "").lower()
    cells: dict = {}
    titles: dict = {}
    nms: dict = {}
    for r in rows:
        art = r.article
        if not art or r.sale_dt is None:
            continue
        nm = (r.nm_id or "").strip()
        if nm and nm != "0" and art not in nms:
            nms[art] = nm
        sign = -1 if is_ret(r.doc_type_name) else 1
        if art not in cells:
            cells[art] = [0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        c = cells[art]
        qty = r.quantity if _is_goods_row(
            r.doc_type_name, r.retail_amount, r.for_pay) else 0
        c[0] += qty * sign                       # sells
        c[1] += float(r.retail_amount or 0) * sign      # revenue
        c[2] += float(r.ppvz_sales_commission or 0) * sign
        c[3] += float(r.delivery_service or 0)
        c[4] += float(r.paid_storage or 0)
        c[5] += float(r.penalty or 0) + float(r.deduction or 0) \
            + float(r.additional_payment or 0) + float(r.rebill_logistic_cost or 0)
        c[6] += float(r.for_pay or 0) * sign            # income
        # Логистика туда/обратно и возвраты
        if is_ret(r.doc_type_name):
            c[8] += float(r.delivery_service or 0)       # inbound logistics
            if _is_goods_row(r.doc_type_name, r.retail_amount, r.for_pay):
                c[9] += qty                              # returns_qty
        else:
            c[7] += float(r.delivery_service or 0)       # outbound logistics
        if (r.title or "").strip():
            titles.setdefault(art, str(r.title).strip())
    # Безартикульные расходы (логистика, услуги) распределяются
    # пропорционально весу статьи (фолбэк abs(sells)) — как storage_split.
    _redistribute_articleless(rows, cells, {
        "logistics": (3, lambda r: float(r.delivery_service or 0)),
        "logistics_out": (7, lambda r: float(r.delivery_service or 0)
                          if "возврат" not in str(r.doc_type_name or "").lower()
                          and "return" not in str(r.doc_type_name or "").lower() else 0.0),
        "logistics_in": (8, lambda r: float(r.delivery_service or 0)
                         if "возврат" in str(r.doc_type_name or "").lower()
                         or "return" in str(r.doc_type_name or "").lower() else 0.0),
        "services": (5, lambda r: float(r.penalty or 0) + float(r.deduction or 0)
                     + float(r.additional_payment or 0) + float(r.rebill_logistic_cost or 0)),
    }, weight_idx=(0,))
    if not cells:
        return out

    recs = []
    for art, c in cells.items():
        if art_check is not None and art_check.search(art) is None:
            continue
        (sells, revenue, commission, logistics, storage, services, income,
         logistics_out, logistics_in, returns_qty) = c
        prod = _find_product(art)
        name = (prod.name or "") if prod else ""
        if not name:
            name = titles.get(art, "")
        net_cost = float(prod.net_cost or 0) if prod else 0.0
        if net_cost <= 0:
            net_cost = default_net_cost
            est = True
        else:
            est = False
        # Хранение: фактическое по артикулу (обычно 0 — платы безартикульные)
        # + распределённая оценка (объём × тариф × остаток).
        storage_full = storage + storage_est.get(art.strip().upper(), 0.0)
        margin_gross = round(income - logistics - storage_full - services, 2)
        margin = round(margin_gross - net_cost * max(0, sells), 2)
        margin_per_one = round(margin / sells, 2) if sells > 0 else 0.0
        margin_pct = round(margin / revenue * 100, 2) if revenue > 0 and sells > 0 else 0.0
        margin_pct_income = round(margin / income * 100, 2) if income > 0 and sells > 0 else 0.0
        # Пересчитанные на единицу
        _s = sells if sells > 0 else 0
        commission_per_one = round(commission / _s, 2) if _s else 0.0
        logistics_per_one = round(logistics / _s, 2) if _s else 0.0
        logistics_out_per_one = round(logistics_out / _s, 2) if _s else 0.0
        logistics_in_per_one = round(logistics_in / _s, 2) if _s else 0.0
        storage_per_one = round(storage_full / _s, 2) if _s else 0.0
        income_per_one = round(income / _s, 2) if _s else 0.0
        revenue_per_one = round(revenue / _s, 2) if _s else 0.0
        margin_gross_per_one = round(margin_gross / _s, 2) if _s else 0.0
        denom = sells + returns_qty
        return_rate = round(returns_qty / denom * 100, 1) if denom > 0 else 0.0
        st_q, st_f, st_w = stock_map.get(art.strip().upper(), (0, 0, 0))
        recs.append({
            "article": art, "nm_id": nms.get(art, ""), "name": name, "sells": sells,
            "returns_qty": returns_qty,
            "stock_qty": st_q, "stock_total": st_f, "stock_in_way": st_w,
            "revenue": round(revenue, 2),
            "commission": round(commission, 2),
            "logistics": round(logistics, 2),
            "logistics_out": round(logistics_out, 2),
            "logistics_in": round(logistics_in, 2),
            "storage": round(storage_full, 2),
            "services": round(services, 2),
            "income": round(income, 2), "margin_gross": margin_gross,
            "net_cost": round(net_cost, 2),
            "net_cost_est": est, "margin": margin,
            "margin_per_one": margin_per_one, "margin_pct": margin_pct,
            "margin_pct_income": margin_pct_income,
            "commission_per_one": commission_per_one,
            "logistics_per_one": logistics_per_one,
            "logistics_out_per_one": logistics_out_per_one,
            "logistics_in_per_one": logistics_in_per_one,
            "storage_per_one": storage_per_one,
            "income_per_one": income_per_one,
            "revenue_per_one": revenue_per_one,
            "margin_gross_per_one": margin_gross_per_one,
            "return_rate": return_rate,
        })
    out = pd.DataFrame(recs, columns=DETAIL_MARGIN_COLUMNS).sort_values(
        "margin", ascending=False).reset_index(drop=True)
    return out


OZON_DETAIL_MARGIN_COLUMNS = [
    "article", "nm_id", "name", "size", "sizes_count", "offers_count",
    "sells", "returns_qty", "postings",
    "revenue", "amount", "commission", "services", "income",
    "margin_gross", "storage",
    "net_cost", "net_cost_est", "margin", "margin_per_one", "margin_pct",
    "commission_per_one", "services_per_one", "storage_per_one",
    "income_per_one", "revenue_per_one", "return_rate",
    "accrued_sale", "accrued_commission", "accrued_logistics",
    "accrued_services", "accrued_other", "accrued_net",
    "accrued_diff", "accrued_coverage", "has_detail",
    "margin_accrued",
]


def ozon_margin_detail_dataframe(
    db: Session,
    date_from=None,
    date_to=None,
    article_like: Optional[str] = None,
    default_net_cost: float = 0.0,
    by_size: bool = False,
) -> pd.DataFrame:
    """Прибыльность по «Детализации продаж» Ozon из ozon_detail_rows.

    Аналог margin_detail_dataframe для WB: строка — операция (постинг/выкуп на
    артикул). Возвраты учитываются отдельно (returns_qty и в деньгах income),
    продано остаётся гроссом — как в своде oz-detail. income в Ozon уже чистый к
    перечислению (комиссия и услуги из него вычтены), поэтому маржа-себест. =
    income − net_cost × продано, а commission/services показываются справочно.

    by_size=False (по умолчанию) — строка это товар: артикулы размеров свёрнуты
    в базовый артикул (app/services/ozon_article.py), себестоимость берётся у
    базового артикула, в колонках размеры/артикулы показываются количеством.
    by_size=True — прежнее поведение: строка это артикул конкретного размера.

    Себестоимость: из каталога products — матчинг UPPER по базовому артикулу,
    затем по артикулу размера, затем по barcode строки детализации;
    промах → default_net_cost c флагом net_cost_est=True.

    Артикул WB (nm_id) подтягивается из marketplace_cards (ozon) по совпадению
    vendor_code с offer_id (зависит от enrich_oz_cards_with_wb_nm).
    """
    cols = list(OZON_DETAIL_MARGIN_COLUMNS)
    out = pd.DataFrame(columns=cols)

    q = select(models.OzonDetailRow)
    if date_from:
        q = q.where(models.OzonDetailRow.date >= date_from)
    if date_to:
        q = q.where(models.OzonDetailRow.date <= date_to)
    if article_like:
        # ищем и по полному артикулу размера, и по базовому артикулу товара
        q = q.where(like_col(models.OzonDetailRow.offer_id, article_like)
                    | like_col(models.OzonDetailRow.base_article, article_like))
    rows = list(db.execute(q).scalars().all())
    if not rows:
        return out

    # Стоимость размещения (хранение) по артикулам за окно: ozon_placements.
    storage_by_art: dict = {}
    pq = select(models.OzonPlacement)
    if date_from:
        pq = pq.where(models.OzonPlacement.date >= date_from)
    if date_to:
        pq = pq.where(models.OzonPlacement.date <= date_to)
    for p in db.execute(pq).scalars().all():
        art = (p.offer_id or "").strip()
        if not art:
            continue
        storage_by_art[art] = storage_by_art.get(art, 0.0) + float(p.storage or 0)

    # Точные начисления (аккруалы) по артикулам за окно: ozon_accruals.
    # реализация по корзинам sale/commission/logistics/services/other;
    # сюда попадают только строки с привязанным offer_id.
    accrual_by_art: dict = {}
    aq = select(models.OzonAccrual)
    if date_from:
        aq = aq.where(models.OzonAccrual.date >= date_from)
    if date_to:
        aq = aq.where(models.OzonAccrual.date <= date_to)
    for acc in db.execute(aq).scalars().all():
        art = (acc.offer_id or "").strip()
        if not art:
            continue
        bucket = (acc.bucket or "").strip() or "other"
        d = accrual_by_art.setdefault(art, {"_cov": False})
        d["_cov"] = True
        d[bucket] = d.get(bucket, 0.0) + float(acc.amount or 0)

    # Артикул WB: marketplace_cards (ozon): vendor_code (offer_id) -> nm_id.
    mp_oz = db.execute(
        select(models.Marketplace.id).where(models.Marketplace.code == "ozon")
    ).scalar_one_or_none()
    nms: dict = {}
    if mp_oz is not None:
        for c in db.execute(select(
                models.MarketplaceCard.vendor_code, models.MarketplaceCard.nm_id)
                .where(
                    models.MarketplaceCard.marketplace_id == mp_oz,
                    models.MarketplaceCard.nm_id != "",
                )).all():
            vc = (c[0] or "").strip().upper()
            nm = (c[1] or "").strip()
            if vc and nm and nm != "0" and vc not in nms:
                nms[vc] = nm

    # Группировка: базовый артикул (товар) либо полный артикул размера.
    omap = ozon_article.build_offer_map(
        db, offers={(r.offer_id or "").strip() for r in rows}
        | {(r.barcode or "").strip() for r in rows} - {""}
        | set(storage_by_art) | set(accrual_by_art))
    group_of: dict = {}
    for art in set(storage_by_art) | set(accrual_by_art) | {
            (r.offer_id or "").strip() for r in rows}:
        group_of[art] = ozon_article.group_key(art, omap, by_size)

    def _roll_up(src: dict) -> dict:
        """Суммирует словари по артикулам в словари по группам."""
        out_: dict = {}
        for art, d in src.items():
            g = group_of.get(art, art)
            dst = out_.setdefault(g, {})
            for k, v in d.items():
                if k == "_cov":
                    dst["_cov"] = bool(dst.get("_cov")) or bool(v)
                else:
                    dst[k] = dst.get(k, 0.0) + float(v)
        return out_

    storage_by_key = ({a: round(v, 2) for a, v in storage_by_art.items()} if by_size
                      else {k: round(d["s"], 2) for k, d in _roll_up(
                          {a: {"s": v} for a, v in storage_by_art.items()}).items()})
    accrual_by_key = ({a: d for a, d in accrual_by_art.items()} if by_size
                      else _roll_up(accrual_by_art))

    prods = db.execute(select(models.Product).where(
        func.upper(models.Product.article).in_(
            {(r.offer_id or "").strip().upper() for r in rows if (r.offer_id or "").strip()}
            | {g.upper() for g in group_of.values() if g})
        | func.upper(func.coalesce(models.Product.barcode, "")).in_(
            {(r.barcode or "").strip().upper() for r in rows if (r.barcode or "").strip()})
    )).scalars().all()
    prods_by_upper = {p.article.strip().upper(): p for p in prods}
    prods_by_barcode = {p.barcode.strip().upper(): p for p in prods if (p.barcode or "").strip()}
    art_bcs: dict = {}
    for r in rows:
        art = (r.offer_id or "").strip()
        bc = (r.barcode or "").strip().upper()
        if art and bc and bc in prods_by_barcode:
            art_bcs.setdefault(art.upper(), set()).add(bc)

    def _find_product(art: str, group: str = ""):
        """Себестоимость: сначала базовый артикул, потом артикул размера."""
        for cand in ([group] if group else []) + [art]:
            p = prods_by_upper.get(cand.strip().upper())
            if p is not None:
                return p
        for bc in art_bcs.get(art.strip().upper(), set()):
            qq = prods_by_barcode.get(bc)
            if qq is not None:
                return qq
        return None

    cells: dict = {}
    titles: dict = {}
    postings: dict = {}
    detail_ops: dict = {}
    group_sizes: dict = {}
    group_offers: dict = {}
    group_nm: dict = {}
    for r in rows:
        art = (r.offer_id or "").strip()
        if not art or r.date is None:
            continue
        key = group_of.get(art) or art
        if key not in cells:
            cells[key] = [0, 0, 0.0, 0.0, 0.0, 0.0, 0.0]
        c = cells[key]
        c[0] += int(r.quantity or 0)                         # sells (гросс)
        c[1] += int(r.return_qty or 0)                       # returns_qty
        c[2] += float(r.seller_price or 0) * int(r.quantity or 0)   # revenue
        c[3] += float(r.commission or 0)                     # commission (в минус)
        c[4] += float(r.standard_fee or 0)                   # services (в минус)
        c[5] += float(r.income or 0)                         # income
        c[6] += float(r.amount or 0)                         # amount (бизнес-база)
        postings.setdefault(key, set()).add(str(r.posting_number or ""))
        detail_ops[key] = detail_ops.get(key, 0) + 1
        if (r.name or "").strip():
            titles.setdefault(key, str(r.name).strip())
        _sz = (r.size or "").strip() or ozon_article.size_of(art, omap)
        if _sz:
            group_sizes.setdefault(key, set()).add(_sz)
        group_offers.setdefault(key, set()).add(art)
        _nm = nms.get(art.upper(), "")
        if _nm:
            group_nm.setdefault(key, _nm)
    # Артикулы, у которых есть начисления, но нет строк детализации в окне
    # (например, только логистика/услуги по товару без продаж за период) —
    # показываем отдельной строкой, чтобы итог по начислениям сходился.
    for key in accrual_by_key:
        if key not in cells:
            cells[key] = [0, 0, 0.0, 0.0, 0.0, 0.0, 0.0]

    recs = []
    for key, c in cells.items():
        (sells, returns_qty, revenue, commission, services, income, amount) = c
        offers = sorted(group_offers.get(key) or ([key] if by_size else []))
        art0 = offers[0] if offers else key
        prod = _find_product(art0, key)
        name = (prod.name or "") if prod else ""
        if not name:
            name = titles.get(key, "")
        net_cost = float(prod.net_cost or 0) if prod else 0.0
        if net_cost <= 0:
            net_cost = default_net_cost
            est = True
        else:
            est = False
        storage = round(storage_by_key.get(key, 0.0), 2)
        margin = round(income - net_cost * max(0, sells) + storage, 2)
        margin_gross = round(income + storage, 2)   # до себестоимости: margin + net_cost * sells
        margin_per_one = round(margin / sells, 2) if sells > 0 else 0.0
        margin_pct = margin / revenue * 100 if revenue > 0 and sells > 0 else 0.0
        _s = sells if sells > 0 else 0
        commission_per_one = round(commission / _s, 2) if _s else 0.0
        services_per_one = round(services / _s, 2) if _s else 0.0
        storage_per_one = round(storage / _s, 2) if _s else 0.0
        income_per_one = round(income / _s, 2) if _s else 0.0
        revenue_per_one = round(revenue / _s, 2) if _s else 0.0
        denom = sells + returns_qty
        return_rate = round(returns_qty / denom * 100, 1) if denom > 0 else 0.0
        _acc = accrual_by_key.get(key, {})
        recs.append({
            "article": key, "nm_id": group_nm.get(key, ""), "name": name,
            "size": (ozon_article.size_of(art0, omap) if by_size else ""),
            "sizes_count": len(group_sizes.get(key) or ()),
            "offers_count": len(offers) or 1,
            "sells": sells, "returns_qty": returns_qty,
            "postings": len(postings.get(key, set())),
            "revenue": round(revenue, 2), "amount": round(amount, 2),
            "commission": round(commission, 2),
            "services": round(services, 2), "income": round(income, 2),
            "margin_gross": margin_gross, "storage": storage,
            "net_cost": round(net_cost, 2), "net_cost_est": est,
            "margin": margin, "margin_per_one": margin_per_one,
            "margin_pct": margin_pct,
            "commission_per_one": commission_per_one,
            "services_per_one": services_per_one,
            "storage_per_one": storage_per_one,
            "income_per_one": income_per_one,
            "revenue_per_one": revenue_per_one,
            "return_rate": return_rate,
            "accrued_sale": round(_acc.get("sale", 0.0), 2),
            "accrued_commission": round(_acc.get("commission", 0.0), 2),
            "accrued_logistics": round(_acc.get("logistics", 0.0), 2),
            "accrued_services": round(_acc.get("services", 0.0), 2),
            "accrued_other": round(_acc.get("other", 0.0), 2),
            "has_detail": 1 if detail_ops.get(key) else 0,
        })
    out = pd.DataFrame(recs)
    # Итоговый выигрыш от начислений и его сверка с детализацией.
    out["accrued_net"] = (out["accrued_sale"] + out["accrued_commission"]
                          + out["accrued_logistics"] + out["accrued_services"]
                          + out["accrued_other"]).round(2)
    out["accrued_diff"] = (out["accrued_net"] - out["income"] - out["services"]).round(2)
    out["accrued_coverage"] = [
        1 if accrual_by_key.get(a.strip() if a else "", {}).get("_cov") else 0
        for a in out["article"]
    ]
    # Прибыль по начислениям: точная сумма «на р/с» минус себестоимость проданного.
    # Заполняется только там, где по артикулу есть начисления за окно (coverage=1).
    _acc_net = out["accrued_net"] - out["net_cost"] * out["sells"].clip(lower=0)
    out["margin_accrued"] = [
        round(float(v), 2) if cov else None
        for v, cov in zip(_acc_net, out["accrued_coverage"])
    ]
    out = out[OZON_DETAIL_MARGIN_COLUMNS].sort_values(
        "margin", ascending=False).reset_index(drop=True)
    return out


def compare_margin_periods(current: pd.DataFrame, prev: pd.DataFrame) -> pd.DataFrame:
    """Навешивает на текущую сводку маржинальности показатели пред. периода.

    prev — сводка за предыдущий аналогичный период (та же длительность окна,
    сдвинутая назад). Для каждого артикула текущей сводки ищется строка из prev:
    sells_pp, margin_pp, delta_ru (руб), delta_pct (к предыдущей марже).
    Артикулы без данных в пред. периоде получают пустые показатели.
    """
    cur = current.copy()
    if prev is None or prev.empty:
        for c in ("sells_pp", "margin_pp", "delta_ru", "delta_pct"):
            cur[c] = pd.Series([None] * len(cur), index=cur.index, dtype=object)
        return cur

    prev_map = prev.set_index("article")["margin"].to_dict()
    prev_sells_map = prev.set_index("article")["sells"].to_dict()
    cur["sells_pp"] = cur["article"].map(prev_sells_map)
    cur["margin_pp"] = cur["article"].map(prev_map)
    cur["delta_ru"] = cur.apply(
        lambda r: round(r["margin"] - r["margin_pp"], 2)
        if pd.notna(r["margin_pp"]) else None, axis=1)
    cur["delta_pct"] = cur.apply(
        lambda r: round((r["margin"] - r["margin_pp"]) / abs(r["margin_pp"]) * 100, 2)
        if pd.notna(r["margin_pp"]) and r["margin_pp"] != 0 else None, axis=1)
    # NaN → None для JSON-безопасности. Сначала object-dtype: присвоение None
    # в числовые колонки pandas-ом обратно превращается в NaN.
    for c in ("sells_pp", "margin_pp", "delta_ru", "delta_pct"):
        col = cur[c]
        cur[c] = col.astype(object).where(col.notna(), None)
    return cur