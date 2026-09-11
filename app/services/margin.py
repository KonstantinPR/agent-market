"""Расчёт маржинальности по продажам (агрегация sales + products)."""
from typing import Optional

import numpy as np
import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models


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
        df = pd.DataFrame(columns=[
            "article", "name", "sells", "revenue", "commission", "logistics",
            "storage", "services", "income", "net_cost", "other",
            "margin", "margin_per_one", "margin_pct",
        ])
        return df

    df["net_cost"] = pd.to_numeric(df["net_cost"], errors="coerce").fillna(0)
    df["other"] = (
        df["income"] - (df["revenue"] + df["commission"]
                        + df["logistics"] + df["storage"] + df["services"])
    ).round(2)
    df["margin"] = df["income"] - df["net_cost"] * df["sells"]
    df["margin_per_one"] = np.where(
        df["sells"] > 0, df["margin"] / np.where(df["sells"] == 0, 1, df["sells"]), 0
    )
    df["margin_pct"] = np.where(df["income"] != 0, df["margin"] / df["income"] * 100, 0)
    df = df.sort_values("margin", ascending=False).reset_index(drop=True)
    df["article"] = df["article"].astype(str)
    return df