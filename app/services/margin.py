"""Расчёт маржинальности по продажам (агрегация sales + products)."""
from typing import Optional

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.config import settings
from app.services.sync import _is_goods_row, _redistribute_articleless, storage_split


def funnel_dataframe(
    db: Session,
    date_from=None,
    date_to=None,
    article_like: Optional[str] = None,
) -> pd.DataFrame:
    """Прибыльность по Воронке Продаж WB (funnel_metric + себестоимость).

    Воронка WB — срез метрик за период (не по дням), поэтому выбираем один
    последний срез, пересекающий запрошенный диапазон, и не суммируем срезы.
    Это ОЦЕНКА: в воронке нет комиссий/логистики/хранения WB, поэтому маржа =
    выручка (или avg_price*orders) минус себестоимость (маржинальность «до расходов WB»).
    """
    rows = pd.DataFrame(columns=[
        "article", "name", "views", "opens", "adds", "orders", "cancelled",
        "avg_price", "revenue", "cart_pct", "order_pct", "net_cost", "margin", "margin_pct",
    ])

    # 1. Последний срез воронки в диапазоне (максимальная дата окончания)
    snap = db.execute(
        select(models.FunnelMetric.date_from, models.FunnelMetric.date_to)
        .where(models.FunnelMetric.date_from <= date_to,
               models.FunnelMetric.date_to >= date_from)
        .order_by(models.FunnelMetric.date_to.desc())
    ).first()
    if snap is None:
        return rows

    from_, to_ = snap.date_from, snap.date_to

    # 2. Метрики по артикулам за этот срез + себестоимость из products
    q = (
        select(
            models.FunnelMetric.article,
            func.sum(models.FunnelMetric.views).label("views"),
            func.sum(models.FunnelMetric.opens).label("opens"),
            func.sum(models.FunnelMetric.adds).label("adds"),
            func.sum(models.FunnelMetric.orders).label("orders"),
            func.sum(models.FunnelMetric.cancelled).label("cancelled"),
            func.max(models.FunnelMetric.avg_price).label("avg_price"),
            func.sum(models.FunnelMetric.revenue).label("revenue"),
            models.Product.name.label("name"),
            models.Product.net_cost.label("net_cost"),
        )
        .outerjoin(models.Product, models.FunnelMetric.article == models.Product.article)
        .where(models.FunnelMetric.date_from == from_,
               models.FunnelMetric.date_to == to_)
        .group_by(models.FunnelMetric.article, models.Product.name, models.Product.net_cost)
    )
    if article_like:
        q = q.where(models.FunnelMetric.article.ilike(f"%{article_like}%"))

    for r in db.execute(q):
        orders = int(r.orders)
        net_cost = float(r.net_cost or 0)
        avg_price = float(r.avg_price or 0)
        revenue = float(r.revenue or 0)
        revenue_eff = revenue if revenue else avg_price * orders
        cost = net_cost * orders
        margin = round(revenue_eff - cost, 2)
        margin_pct = round(margin / revenue_eff * 100, 2) if revenue_eff else 0.0
        cart_pct = round((r.adds / r.views * 100) if r.views else 0.0, 2)
        order_pct = round((orders / r.views * 100) if r.views else 0.0, 2)
        rows = pd.concat([rows, pd.DataFrame([{
            "article": r.article,
            "name": r.name or "",
            "views": int(r.views),
            "opens": int(r.opens),
            "adds": int(r.adds),
            "orders": orders,
            "cancelled": int(r.cancelled),
            "avg_price": avg_price,
            "revenue": revenue_eff,
            "cart_pct": cart_pct,
            "order_pct": order_pct,
            "net_cost": net_cost,
            "margin": margin,
            "margin_pct": margin_pct,
        }])], ignore_index=True)

    rows = rows.sort_values("margin", ascending=False).reset_index(drop=True)
    rows.attrs["date_from"] = str(from_)
    rows.attrs["date_to"] = str(to_)
    return rows


def margin_columns() -> list:
    """Колонки итогового df маржинальности (в т.ч. вычисляемые)."""
    return [
        "article", "name", "sells", "revenue", "commission", "logistics",
        "storage", "services", "income", "margin_gross", "net_cost", "other",
        "margin", "margin_per_one", "margin_pct",
    ]


def compute_margin(df: pd.DataFrame) -> pd.DataFrame:
    """Считает производные колонки маржинальности из агрегированного df.

    Ожидаются колонки: article, name, sells, revenue, commission, logistics,
    storage, services, income, net_cost. Ничего не пишет в БД — чистый расчёт.
    Маржа (margin_gross) — прибыль до себестоимости, «Маржа-себест.» (margin)
    — после вычета net_cost за проданный (не возвращённый) товар.
    """
    df = df.copy()
    df["net_cost"] = pd.to_numeric(df["net_cost"], errors="coerce").fillna(0)
    df["other"] = (
        df["income"] - (df["revenue"] + df["commission"]
                        + df["logistics"] + df["storage"] + df["services"])
    ).round(2)
    df["margin_gross"] = round(
        df["income"] - df["logistics"] - df["storage"] - df["services"], 2
    )
    df["margin"] = df["income"] - df["net_cost"] * np.maximum(0, df["sells"])
    df["margin_per_one"] = np.where(
        df["sells"] > 0, df["margin"] / np.where(df["sells"] == 0, 1, df["sells"]), 0
    )
    df["margin_pct"] = np.where(
        (df["income"] > 0) & (df["sells"] > 0), df["margin"] / df["income"] * 100, 0
    )
    df = df.sort_values("margin", ascending=False).reset_index(drop=True)
    df["article"] = df["article"].astype(str)
    return df


def empty_margin_df() -> pd.DataFrame:
    return pd.DataFrame(columns=margin_columns())


DETAIL_MARGIN_COLUMNS = [
    "article", "name", "sells", "returns_qty", "revenue", "commission",
    "logistics", "logistics_out", "logistics_in", "storage", "services",
    "income", "margin_gross", "net_cost", "net_cost_est", "margin",
    "margin_per_one", "margin_pct",
    "commission_per_one", "logistics_per_one",
    "logistics_out_per_one", "logistics_in_per_one",
    "storage_per_one", "income_per_one", "revenue_per_one",
    "margin_gross_per_one", "return_rate",
]


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
    """
    cols = ["article", "name", "sells", "returns_qty", "revenue", "commission",
            "logistics", "logistics_out", "logistics_in", "storage", "services",
            "income", "margin_gross", "net_cost", "net_cost_est", "margin",
            "margin_per_one", "margin_pct",
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
    if article_like:
        q = q.where(models.WbDetailRow.article.ilike(f"%{article_like}%"))
    rows = list(db.execute(q).scalars().all())
    if not rows:
        return out

    # Распределение безартикульных плат «Хранение» по товарам продаж пропорц.
    # «объём × тариф × (остаток + проданное×0.5)» (storage_costs × stocks +
    # продажи периода). Распределение идёт только по артикулам с операциями
    # в окне — орфанные доли не теряются.
    storage_est = storage_split(
        db,
        date_from=date_from,
        date_to=date_to,
        target_articles={r.article.strip().upper() for r in rows
                         if (r.article or "").strip()},
    )

    prods = db.execute(select(models.Product).where(
        func.upper(models.Product.article).in_(
            {(r.article or "").strip().upper() for r in rows if (r.article or "").strip()})
        | func.upper(func.coalesce(models.Product.barcode, "")).in_(
            {(r.sku or "").strip().upper() for r in rows if (r.sku or "").strip()})
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

    is_ret = (lambda t: "возврат" in str(t or "").lower() or "return" in str(t or "").lower())
    cells: dict = {}
    titles: dict = {}
    for r in rows:
        art = r.article
        if not art or r.sale_dt is None:
            continue
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
        recs.append({
            "article": art, "name": name, "sells": sells,
            "returns_qty": returns_qty,
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
    out = pd.DataFrame(recs).sort_values("margin", ascending=False).reset_index(drop=True)
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


def margin_dataframe(
    db: Session,
    date_from=None,
    date_to=None,
    marketplace=None,
    article_like: Optional[str] = None,
    source: Optional[str] = None,
) -> pd.DataFrame:
    query = (
        select(
            models.Sale.article,
            func.max(models.Product.name).label("name"),
            func.sum(models.Sale.quantity).label("sells"),
            func.sum(models.Sale.revenue).label("revenue"),
            func.sum(models.Sale.commission).label("commission"),
            func.sum(models.Sale.logistics).label("logistics"),
            func.sum(models.Sale.storage).label("storage"),
            func.sum(models.Sale.services).label("services"),
            func.sum(models.Sale.income).label("income"),
            func.max(models.Product.net_cost).label("net_cost"),
        )
        .select_from(models.Sale)
        .join(models.Product, models.Sale.article == models.Product.article)
        .group_by(models.Sale.article)
    )
    if date_from:
        query = query.where(models.Sale.date >= pd.Timestamp(date_from).date())
    if date_to:
        query = query.where(models.Sale.date <= pd.Timestamp(date_to).date())
    if marketplace is not None and marketplace != "":
        ids = marketplace if isinstance(marketplace, (list, tuple, set)) else [marketplace]
        query = query.where(models.Sale.marketplace_id.in_(ids))
    if article_like:
        query = query.where(models.Sale.article.ilike(f"%{article_like}%"))
    if source:
        query = query.where(models.Sale.source == source)
    else:
        query = query.where(models.Sale.source != "detail")

    df = pd.read_sql(query, db.bind)
    if df.empty:
        return empty_margin_df()

    return compute_margin(df)