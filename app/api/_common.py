# -*- coding: utf-8 -*-
"""Общий багаж, кросс-групповые хелперы и вьюхи, переиспользуемые экспортами."""
import os
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from typing import List, Optional

import pandas as pd
from fastapi import APIRouter, Body, Depends, HTTPException, UploadFile, File
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.config import settings
from app.database import get_db
from app.providers import factory as provider_factory
from app.providers.errors import WbApiError
from app.providers.ozon import OZON_RU_COLUMNS
from app.providers.wb import DETAIL_RU_COLUMNS, DETAIL_UPLOAD_RENAME, SALES_RU_COLUMNS, V5_RU_COLUMNS
from app.services import (
    base_price as base_price_service,
    common as common_service,
    dashboard as dashboard_service,
    excel_import,
    excel_io,
    funnel as funnel_service,
    margin as margin_service,
    ozon_article,
    pdf_demand,
    photos as photos_service,
    pricing as pricing_service,
    refresh as refresh_service,
    replenish as replenish_service,
    sync as sync_service,
    thumbs as thumbs_service,
    tickets as tickets_service,
    warehouse as warehouse_service,
    yandex_disk as yandex_service,
)
from app.services.window import parse_window


router = APIRouter()



def _parse_window400(date_from=None, date_to=None):
    """parse_window + превращает некорректную дату в HTTP 400 (а не 500)."""
    try:
        return parse_window(date_from, date_to)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Некорректная дата: {e}")


def _df_totals(df: pd.DataFrame, extra_skip=None) -> dict:
    """Итоговая строка по числовым колонкам df (для UI-футера)."""
    if df is None or df.empty:
        return {}
    skip = {"margin_pct", "delta_pct", "margin_per_one", "net_cost_est",
            "commission_per_one", "logistics_per_one", "logistics_out_per_one",
            "logistics_in_per_one", "storage_per_one", "services_per_one",
            "income_per_one",
            "revenue_per_one", "margin_gross_per_one", "return_rate",
            "avg_price", "product_rating", "feedback_rating",
            "avg_orders_per_day", "share_order_percent", "time_to_ready_min",
            "localization_percent", "conv_to_cart_percent",
            "conv_cart_to_order_percent", "conv_buyout_percent",
            "wb_club_avg_price", "wb_club_buyout_percent",
            "wb_club_avg_orders_per_day",
            "buyout_percent", "commission_ratio", "deduction_by_category_percent",
            "vat_percent",
            "past_avg_price",
            "dy_views", "dy_adds", "dy_orders", "dy_cancelled", "dy_buyouts",
            "dy_revenue", "dy_avg_price",
            # счётчики групп (суммировать бессмысленно — это «сколько в строке»)
            "sizes_count", "offers_count"}
    if extra_skip:
        skip = skip | set(extra_skip)
    out: dict = {}
    for c in df.columns:
        if c in skip:
            continue
        s = df[c]
        if s.dtype.kind in "iuf":
            out[c] = round(float(s.sum()), 2)
    return out


def _cashflow_received(db, from_, to_):
    """Фактически получено на р/с: -(сумма payments_amount) по периодам
    движения средств, пересекающимся с окном. Возвращает (received, periods)."""
    q = select(func.coalesce(func.sum(models.OzonCashFlow.payments_amount), 0.0))
    if from_:
        q = q.where(models.OzonCashFlow.period_end >= from_)
    if to_:
        q = q.where(models.OzonCashFlow.period_begin <= to_)
    try:
        paid = float(db.execute(q).scalar())
        periods = int(db.execute(
            select(func.count()).select_from(models.OzonCashFlow)
        ).scalar() or 0)
        return round(-paid, 2), periods
    except Exception:  # noqa: BLE001
        return None, 0


def req_window(date_from, date_to) -> dict:
    """Запрошенное окно — UI пишет фактический период запроса, а не зашитый."""
    return {"date_from": date_from or None, "date_to": date_to or None}


def ozon_date_range(db, model, column=None) -> dict:
    """Границы фактического покрытия по датам в таблице Ozon.

    Нужно UI, чтобы пустой результат объяснять фактами («в базе покрыто
    2026-02-21 … 2026-08-30»), а не зашитым в JS текстом: покрытие меняется при
    каждой загрузке, а окно запроса — нет.
    """
    col = column if column is not None else model.date
    try:
        row = db.execute(
            select(func.min(col), func.max(col), func.count())
        ).fetchone()
    except Exception:  # noqa: BLE001
        return {"date_from": None, "date_to": None, "rows": 0}
    d1, d2, n = row if row else (None, None, 0)
    return {
        "date_from": d1.isoformat() if d1 else None,
        "date_to": d2.isoformat() if d2 else None,
        "rows": int(n or 0),
    }


def ozon_detail_range(db) -> dict:
    """Покрытие детализации Ozon (ozon_detail_rows) — для маржи и раздела OZON API."""
    return ozon_date_range(db, models.OzonDetailRow)


# Удельные/процентные колонки детализации маржинальности: в «Итого» — среднее.
_MARGIN_AVG_TOTALS = (
    "margin_per_one", "margin_pct", "commission_per_one",
    "logistics_per_one", "logistics_out_per_one", "logistics_in_per_one",
    "storage_per_one", "income_per_one", "revenue_per_one",
    "margin_gross_per_one", "services_per_one", "return_rate", "delta_pct",
)


def _margin_detail_totals(df: pd.DataFrame) -> dict:
    """Итоги для «Детализации» маржинальности: суммы + средние по удельным.

    _df_totals даёт суммы денежных/количественных колонок (включая остатки WB);
    сверху добавляются `{avg}` для удельных и процентных показателей —
    фронт рендерит их как «≈ среднее по видимым строкам».
    """
    totals = _df_totals(df)
    # Колонки сравнения периодов: суммы (артикулы без данных пред. периода дают None).
    for c in ("sells_pp", "margin_pp", "delta_ru"):
        if c not in df.columns:
            continue
        s = pd.to_numeric(pd.Series(df[c]), errors="coerce").dropna()
        if len(s):
            totals[c] = round(float(s.sum()), 2)
    for c in _MARGIN_AVG_TOTALS:
        if c not in df.columns:
            continue
        s = pd.to_numeric(pd.Series(df[c]), errors="coerce").dropna()
        if len(s):
            totals[c] = {"avg": round(float(s.mean()), 2)}
    return totals


@router.get("/sales")
def api_sales(
    marketplace: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)

    query = (
        select(
            models.Marketplace.code.label("marketplace"),
            models.Sale.date,
            models.Sale.article,
            func.max(models.Product.name).label("name"),
            func.sum(models.Sale.quantity).label("quantity"),
            func.sum(models.Sale.revenue).label("revenue"),
            func.sum(models.Sale.income).label("income"),
        )
        .select_from(models.Sale)
        .join(models.Marketplace, models.Sale.marketplace_id == models.Marketplace.id)
        .join(models.Product, models.Sale.article == models.Product.article)
        .where(models.Sale.date >= from_, models.Sale.date <= to_,
               models.Sale.source != "detail")
        .group_by(models.Marketplace.code, models.Sale.date, models.Sale.article)
        .order_by(models.Sale.date.desc(), models.Sale.article)
    )
    if marketplace:
        query = query.where(models.Marketplace.code == marketplace)

    rows = [
        {
            "date": str(r.date),
            "marketplace": r.marketplace,
            "article": r.article,
            "name": r.name,
            "quantity": int(r.quantity or 0),
            "revenue": float(r.revenue or 0),
            "income": float(r.income or 0),
        }
        for r in db.execute(query)
    ]
    _t = _df_totals(pd.DataFrame(rows)) if rows else {}
    return {"rows": rows, "count": len(rows), "date_from": str(from_), "date_to": str(to_), "totals": _t}


@router.get("/funnel")
def api_funnel(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Строки воронки продаж WB из funnel_metric.

    Без дат — окно последней загрузки (max(date_to)); иначе запрошенный период.
    Срез выбирается через pick_funnel_window (точное окно → самый широкий
    внутри запрошенного → самый свежий пересекающийся → последний в базе).
    В ответе date_from/date_to — запрошенный период, snapshot_from/snapshot_to —
    фактически показанный срез, matched — совпали ли они.
    """
    latest_win = db.execute(
        select(models.FunnelMetric.date_from, models.FunnelMetric.date_to)
        .order_by(models.FunnelMetric.date_to.desc(), models.FunnelMetric.date_from.desc())
        .limit(1)
    ).first()
    windows = [
        (r[0], r[1])
        for r in db.execute(
            select(models.FunnelMetric.date_from, models.FunnelMetric.date_to).distinct()
        )
    ]

    requested = None
    if date_from is not None and date_to is not None:
        requested = _parse_window400(date_from, date_to)
        if latest_win is None:
            from_, to_ = requested
        else:
            chosen = funnel_service.pick_funnel_window(windows, requested[0], requested[1])
            from_, to_ = chosen if chosen is not None else (latest_win[0], latest_win[1])
    else:
        if latest_win is None:
            return {"rows": [], "count": 0, "date_from": "", "date_to": "",
                    "snapshot_from": "", "snapshot_to": "", "matched": False, "totals": {}}
        from_, to_ = latest_win[0], latest_win[1]

    name_subq = (
        select(models.Product.name)
        .where(models.Product.article == models.FunnelMetric.article)
        .limit(1)
        .scalar_subquery()
    )

    def snapshot_rows(sf: date, st: date) -> list:
        q = (
            select(
                models.FunnelMetric.date_from,
                models.FunnelMetric.date_to,
                models.FunnelMetric.nm_id,
                models.FunnelMetric.article,
                name_subq.label("name"),
                models.FunnelMetric.views,
                models.FunnelMetric.opens,
                models.FunnelMetric.adds,
                models.FunnelMetric.orders,
                models.FunnelMetric.cancelled,
                models.FunnelMetric.buyouts,
                models.FunnelMetric.avg_price,
                models.FunnelMetric.revenue,
                models.FunnelMetric.buyout_sum,
                models.FunnelMetric.subject_name,
                models.FunnelMetric.brand_name,
                models.FunnelMetric.product_rating,
                models.FunnelMetric.feedback_rating,
                models.FunnelMetric.stock_wb,
                models.FunnelMetric.stock_mp,
                models.FunnelMetric.stock_balance_sum,
                models.FunnelMetric.cancel_sum,
                models.FunnelMetric.avg_orders_per_day,
                models.FunnelMetric.share_order_percent,
                models.FunnelMetric.add_to_wishlist,
                models.FunnelMetric.time_to_ready_min,
                models.FunnelMetric.localization_percent,
                models.FunnelMetric.conv_to_cart_percent,
                models.FunnelMetric.conv_cart_to_order_percent,
                models.FunnelMetric.conv_buyout_percent,
                models.FunnelMetric.wb_club_order_count,
                models.FunnelMetric.wb_club_order_sum,
                models.FunnelMetric.wb_club_buyout_count,
                models.FunnelMetric.wb_club_buyout_sum,
                models.FunnelMetric.wb_club_cancel_count,
                models.FunnelMetric.wb_club_cancel_sum,
                models.FunnelMetric.wb_club_avg_price,
                models.FunnelMetric.wb_club_buyout_percent,
                models.FunnelMetric.wb_club_avg_orders_per_day,
                models.FunnelMetric.title,
                models.FunnelMetric.subject_id,
                models.FunnelMetric.tags,
                models.FunnelMetric.past_json,
                models.FunnelMetric.comparison_json,
            )
            .where(models.FunnelMetric.date_from == sf,
                   models.FunnelMetric.date_to == st)
            .order_by(models.FunnelMetric.revenue.desc(), models.FunnelMetric.article)
        )
        if article_like:
            q = q.where(common_service.like_col(models.FunnelMetric.article, article_like))
        out_cols = [
            "date_from", "date_to", "nm_id", "article", "name",
            "views", "opens", "adds", "orders", "cancelled", "buyouts",
            "avg_price", "revenue", "buyout_sum", "subject_name", "brand_name",
            "product_rating", "feedback_rating", "stock_wb", "stock_mp",
            "stock_balance_sum", "cancel_sum", "avg_orders_per_day",
            "share_order_percent", "add_to_wishlist", "time_to_ready_min",
            "localization_percent", "conv_to_cart_percent",
            "conv_cart_to_order_percent", "conv_buyout_percent",
            "wb_club_order_count", "wb_club_order_sum", "wb_club_buyout_count",
            "wb_club_buyout_sum", "wb_club_cancel_count", "wb_club_cancel_sum",
            "wb_club_avg_price", "wb_club_buyout_percent",
            "wb_club_avg_orders_per_day",
            "title", "subject_id", "tags", "past_json", "comparison_json",
        ]

        def _row(r):
            d = {}
            for c in out_cols:
                v = getattr(r, c)
                if isinstance(v, (date, datetime)):
                    d[c] = str(v)
                elif isinstance(v, Decimal):
                    f = float(v)
                    d[c] = int(f) if f.is_integer() else f
                elif isinstance(v, float):
                    d[c] = float(v)
                elif isinstance(v, int):
                    d[c] = int(v)
                else:
                    d[c] = str(v or "")
            return d

        return [_row(r) for r in db.execute(q)]

    rows = snapshot_rows(from_, to_)
    if not rows and latest_win is not None and (from_, to_) != (latest_win[0], latest_win[1]):
        rows = snapshot_rows(latest_win[0], latest_win[1])
    for r in rows:
        funnel_service.flatten_past_dy(r)
    return {"rows": rows, "count": len(rows),
            "date_from": str(requested[0]) if requested else str(from_),
            "date_to": str(requested[1]) if requested else str(to_),
            "snapshot_from": str(from_), "snapshot_to": str(to_),
            "matched": bool(requested) and (from_, to_) == (requested[0], requested[1]),
            "totals": _df_totals(pd.DataFrame(rows)) if rows else {}}


@router.get("/stocks")
def api_stocks(
    marketplace: Optional[str] = None,
    db: Session = Depends(get_db),
):
    latest_q = select(func.max(models.Stock.date))
    if marketplace:
        latest = db.scalar(
            latest_q.where(
                models.Stock.marketplace_id ==
                select(models.Marketplace.id).where(models.Marketplace.code == marketplace).scalar_subquery()
            )
        )
    else:
        latest = db.scalar(latest_q)
    query = (
        select(
            models.Marketplace.code.label("marketplace"),
            models.Stock.article,
            models.Product.name,
            models.Stock.chrt_id,
            models.Stock.size,
            models.Stock.barcode,
            models.Stock.warehouse,
            models.Stock.quantity,
            models.Stock.quantity_full,
            models.Stock.in_way,
        )
        .select_from(models.Stock)
        .join(models.Marketplace, models.Stock.marketplace_id == models.Marketplace.id)
        .outerjoin(models.Product, models.Stock.article == models.Product.article)
        .where(models.Stock.date == latest)
    )
    if marketplace:
        query = query.where(models.Marketplace.code == marketplace)

    rows = [
        {
            "date": str(latest),
            "marketplace": r.marketplace,
            "article": r.article,
            "name": r.name,
            "chrt_id": str(r.chrt_id or ""),
            "size": str(r.size or ""),
            "barcode": str(r.barcode or ""),
            "warehouse": r.warehouse,
            "quantity": int(r.quantity or 0),
            "quantity_full": int(r.quantity_full or 0),
            "in_way": int(r.in_way or 0),
        }
        for r in db.execute(query)
    ]
    return {"date": str(latest), "rows": rows, "count": len(rows)}


@router.get("/prices")
def api_prices(
    marketplace: str = "wb",
    article_like: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Срез текущих цен/скидок из price_snapshots (последняя загрузка)."""
    name_subq = (
        select(models.Product.name)
        .where(models.Product.article == models.PriceSnapshot.article)
        .limit(1)
        .scalar_subquery()
    )
    q = (
        select(
            models.PriceSnapshot.article,
            models.PriceSnapshot.nm_id,
            models.PriceSnapshot.size,
            name_subq.label("name"),
            models.PriceSnapshot.price,
            models.PriceSnapshot.discounted_price,
            models.PriceSnapshot.discount,
            models.PriceSnapshot.updated_at,
        )
        .where(models.PriceSnapshot.marketplace == marketplace)
        .order_by(models.PriceSnapshot.article, models.PriceSnapshot.size)
    )
    if article_like:
        q = q.where(common_service.like_col(models.PriceSnapshot.article, article_like))
    rows = [
        {
            "article": r.article, "nm_id": str(r.nm_id or ""),
            "size": str(r.size or ""), "name": str(r.name or ""),
            "price": float(r.price or 0),
            "discounted_price": float(r.discounted_price or 0),
            "discount": float(r.discount or 0),
            "updated_at": str(r.updated_at),
        }
        for r in db.execute(q)
    ]
    updated = db.scalar(select(func.max(models.PriceSnapshot.updated_at)))
    return {"rows": rows, "count": len(rows), "updated_at": str(updated or "")}


@router.get("/storage-cost")
def api_storage_cost(
    article_like: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Срез стоимости хранения WB из storage_costs (последняя загрузка)."""
    name_subq = (
        select(models.Product.name)
        .where(models.Product.article == models.StorageCost.article)
        .limit(1)
        .scalar_subquery()
    )
    q = (
        select(
            models.StorageCost.nm_id,
            models.StorageCost.article,
            name_subq.label("name"),
            models.StorageCost.barcodes_count,
            models.StorageCost.volume,
            models.StorageCost.storage_price,
            models.StorageCost.warehouse_price,
            models.StorageCost.updated_at,
        )
        .order_by(models.StorageCost.article)
    )
    if article_like:
        q = q.where(common_service.like_col(models.StorageCost.article, article_like))
    rows = [
        {
            "nm_id": r.nm_id, "article": r.article, "name": str(r.name or ""),
            "barcodes_count": int(r.barcodes_count or 0),
            "volume": float(r.volume or 0),
            "storage_price": float(r.storage_price or 0),
            "warehouse_price": float(r.warehouse_price or 0),
            "updated_at": str(r.updated_at),
        }
        for r in db.execute(q)
    ]
    updated = db.scalar(select(func.max(models.StorageCost.updated_at)))
    return {"rows": rows, "count": len(rows), "updated_at": str(updated or "")}


def _catalog_size_map(db: Session) -> dict:
    """{article: [{size, barcode}, ...]} из product_sizes (сортировка по размеру)."""
    out: dict = {}
    for s in db.execute(select(models.ProductSize).order_by(models.ProductSize.size)).scalars():
        out.setdefault(s.article, []).append({"size": s.size, "barcode": s.barcode})
    return out


def _catalog_tags(db: Session) -> dict:
    """{article: [коды маркетплейсов]} по карточкам (артикул разрешается через алиасы)."""
    alias = {a: c for a, c in db.execute(
        select(models.ProductAlias.alias_article, models.ProductAlias.article))}
    tags: dict = {}
    q = (
        select(models.Marketplace.code, models.MarketplaceCard.vendor_code)
        .join(models.Marketplace, models.MarketplaceCard.marketplace_id == models.Marketplace.id)
    )
    for code, vendor in db.execute(q):
        art = alias.get(vendor, vendor)
        if art:
            tags.setdefault(art, set()).add(code)
    return {a: sorted(v) for a, v in tags.items()}


def _catalog_stock_maps(db: Session):
    """Остатки по последнему срезу.

    mp_stock:      {(мрп, артикул): кол-во} — агрегат по артикулу;
    mp_stock_size: {(мрп, артикул, размер): кол-во} — для WB (размер из techSize карточек);
    mp_covers:     {артикул: {размер: кол-во}} — WB-остатки в разрезе размеров;
    ozon_total:    {артикул: кол-во} — Ozon без размера (суммируется целиком на строку);
    own:           {артикул: баланс склада}.
    """
    mp_stock: dict = {}
    mp_stock_size: dict = {}
    ozon_total: dict = {}
    for code in ("wb", "ozon"):
        mp_id = select(models.Marketplace.id).where(models.Marketplace.code == code).scalar_subquery()
        latest = db.scalar(select(func.max(models.Stock.date)).where(models.Stock.marketplace_id == mp_id))
        if latest is None:
            continue
        for art, size, qty in db.execute(
            select(models.Stock.article, func.coalesce(models.Stock.size, ""),
                   func.sum(models.Stock.quantity))
            .where(models.Stock.marketplace_id == mp_id, models.Stock.date == latest)
            .group_by(models.Stock.article, models.Stock.size)
        ):
            mp_stock[(code, art)] = mp_stock.get((code, art), 0) + int(qty or 0)
            mp_stock_size[(code, art, size or "")] = int(qty or 0)
            if code == "ozon":
                ozon_total[art] = ozon_total.get(art, 0) + int(qty or 0)
    own = {r["article"]: float(r["balance"]) for r in warehouse_service.stock_view(db)}
    mp_covers = {art: {} for art in {a for (_, a, _) in mp_stock_size if (_, a) and _ in ("wb",)}}
    for (code, art, size), qty in mp_stock_size.items():
        if code == "wb" and size:
            mp_covers.setdefault(art, {})[size] = mp_covers.get(art, {}).get(size, 0) + qty
    return mp_stock, mp_stock_size, mp_covers, ozon_total, own


def _product_prefix(article) -> str:
    """Префикс артикула до первой «-» (для группировки похожих товаров)."""
    return str(article or "").strip().split("-", 1)[0].strip()

def _prefix_avg_cost_map(prods) -> dict:
    """Префикс → средневзвешенная (по объёму) себестоимость среди товаров с net_cost>0."""
    acc: dict = {}
    for o in prods:
        cost = float(o.net_cost or 0)
        if cost <= 0:
            continue
        key = _product_prefix(o.article)
        if not key:
            continue
        w = max(float(o.volume_l or 0), 1.0)
        a = acc.setdefault(key, [0.0, 0.0])
        a[0] += cost * w
        a[1] += w
    return {k: (v[0] / v[1]) if v[1] else 0.0 for k, v in acc.items()}

def _eff_net_cost(article, raw_cost, prefix_map) -> float:
    """Себестоимость для расчёта: своя; если 0 — выводить из среднего по префиксу артикула."""
    cost = float(raw_cost or 0)
    if cost > 0:
        return cost
    return float(prefix_map.get(_product_prefix(article), 0) or 0)


def _unit_econ(rows) -> Optional[dict]:
    """Усреднённая единичная экономика по строкам продаж (как pricing._unit_economics).

    rows — итерируемые записи с полями revenue, commission, logistics, storage,
    services, qty. Возвращает {"comm_rate", "logistics_unit", "storage_unit",
    "other_unit"} или None, если данных мало.
    """
    revenue = 0.0
    qty = 0.0
    commission = 0.0
    logistics = 0.0
    storage = 0.0
    services = 0.0
    for r in rows:
        qty += max(float(getattr(r, "qty", 0) or 0), 0)
        revenue += float(getattr(r, "revenue", 0) or 0)
        commission += float(getattr(r, "commission", 0) or 0)
        logistics += float(getattr(r, "logistics", 0) or 0)
        storage += float(getattr(r, "storage", 0) or 0)
        services += float(getattr(r, "services", 0) or 0)
    if revenue <= 0 or qty <= 0:
        return None
    comm_rate = min(0.5, abs(commission) / revenue)
    return {
        "comm_rate": float(comm_rate),
        "logistics_unit": abs(logistics) / qty,
        "storage_unit": abs(storage) / qty,
        "other_unit": abs(services) / qty,
    }


def _sale_agg_rows(db: Session, days: int):
    """Агрегированные продажи WB за последние days дней: article -> строка(суммы + qty)."""
    start = date.today() - timedelta(days=days)
    q = (
        select(
            models.Sale.article,
            func.sum(models.Sale.quantity - models.Sale.returns_qty).label("qty"),
            func.sum(models.Sale.revenue).label("revenue"),
            func.sum(models.Sale.commission).label("commission"),
            func.sum(models.Sale.logistics).label("logistics"),
            func.sum(models.Sale.storage).label("storage"),
            func.sum(models.Sale.services).label("services"),
        )
        .where(models.Sale.date >= start)
        .group_by(models.Sale.article)
    )
    return {str(r.article).strip().upper(): r for r in db.execute(q)}


def _unit_economics_map(db: Session, days: int = 30) -> tuple:
    """Карта UPPER(article) -> unit-экономика за окно + глобальное среднее.

    Возвращает (by_article, global_ue): by_article — для артикулов с собственными
    продажами; global_ue — усреднённое по всем продажам окна (fallback для
    артикулов без истории или None). Ключи by_article — UPPER(article).
    """
    rows = _sale_agg_rows(db, days)
    by_article: dict = {}
    for art, r in rows.items():
        ue = _unit_econ([r])
        if ue:
            by_article[art] = ue
    global_ue = _unit_econ(rows.values())
    return by_article, global_ue


def _catalog_price_fields(prod, settings, net_cost=None, ue=None) -> dict:
    cost = float(prod.net_cost or 0) if net_cost is None else float(net_cost or 0)
    comp = base_price_service.price_components(cost, prod.volume_l, settings)
    return {
        "recommended_price": base_price_service.recommended_price(cost, prod.volume_l, settings),
        "markup": comp["markup"],
        "f_cost": comp["f_cost"],
        "f_vol": comp["f_vol"],
        "min_price": base_price_service.minimum_price(cost, ue, settings),
        "ue_comm_rate": round(float(ue.get("comm_rate") or 0), 4) if ue else None,
        "ue_logistics": round(float(ue.get("logistics_unit") or 0), 2) if ue else None,
        "ue_storage": round(float(ue.get("storage_unit") or 0), 2) if ue else None,
        "ue_other": round(float(ue.get("other_unit") or 0), 2) if ue else None,
    }


@router.get("/products")
def api_products(
    like: Optional[str] = None,
    sizes: int = 0,
    stocks: int = 0,
    db: Session = Depends(get_db),
):
    """Каталог «Наш склад → Товары».

    sizes=0 — агрегат по товару (кол-во размеров, первый баркод, теги WB/Ozon);
    sizes=1 — строки по размерам (product_sizes);
    stocks=1 — добавляет own_stock (баланс склада) и mp_stock (последний срез WB+Ozon),
    а также рекомендуемую цену/наценку (base_price).
    """
    prods = list(db.execute(
        select(models.Product).order_by(models.Product.article)
    ).scalars())
    prefix_map = _prefix_avg_cost_map(prods)
    ue_map, ue_global = _unit_economics_map(db)

    def ue_for(article: str):
        return ue_map.get((article or "").strip().upper()) or ue_global

    if like and like.strip():
        prods = [p for p in prods if common_service.like_match(p.article, like)
                 or common_service.like_match(p.name, like)
                 or common_service.like_match(p.brand, like)
                 or common_service.like_match(p.barcode, like)
                 or common_service.like_match(p.subject, like)]
    size_map = _catalog_size_map(db)
    tags = _catalog_tags(db)
    mp_stock: dict = {}
    mp_stock_size: dict = {}
    mp_covers: dict = {}
    ozon_total: dict = {}
    own: dict = {}
    with_stock = bool(int(stocks))
    if with_stock:
        mp_stock, mp_stock_size, mp_covers, ozon_total, own = _catalog_stock_maps(db)
    settings = base_price_service.PRICE_DEFAULTS

    def mp_total(article: str) -> int:
        return mp_stock.get(("wb", article), 0) + mp_stock.get(("ozon", article), 0)

    def wb_size_qty(article: str, size: str) -> int:
        """WB-остаток в разрезе размера (0 если данных по размеру нет)."""
        return int(mp_stock_size.get(("wb", article, size or ""), 0))

    rows = []
    if int(sizes):
        for p in prods:
            for s in size_map.get(p.article, [{"size": "", "barcode": p.barcode}]):
                row = {
                    "article": p.article, "name": p.name, "brand": p.brand,
                    "subject": p.subject, "size": s["size"], "barcode": s["barcode"],
                    "volume_l": float(p.volume_l or 0), "composition": p.composition,
                    "net_cost": float(p.net_cost or 0), "replenishable": bool(p.replenishable),
                    "tags": tags.get(p.article, []),
                }
                if with_stock:
                    row["own_stock"] = own.get(p.article, 0.0)
                    row["mp_stock"] = wb_size_qty(p.article, s["size"])
                    row["ozon_stock"] = int(ozon_total.get(p.article, 0))
                row.update(_catalog_price_fields(p, settings, _eff_net_cost(p.article, p.net_cost, prefix_map), ue_for(p.article)))
                rows.append(row)
    else:
        for p in prods:
            sizes_lst = size_map.get(p.article, [])
            row = {
                "article": p.article, "name": p.name, "brand": p.brand,
                "subject": p.subject, "sizes_count": len(sizes_lst),
                "barcode": (sizes_lst[0]["barcode"] if sizes_lst else p.barcode),
                "volume_l": float(p.volume_l or 0), "composition": p.composition,
                "net_cost": float(p.net_cost or 0), "replenishable": bool(p.replenishable),
                "tags": tags.get(p.article, []),
            }
            if with_stock:
                row["own_stock"] = own.get(p.article, 0.0)
                row["mp_stock"] = mp_total(p.article)
                row["ozon_stock"] = int(ozon_total.get(p.article, 0))
            row.update(_catalog_price_fields(p, settings, _eff_net_cost(p.article, p.net_cost, prefix_map), ue_for(p.article)))
            rows.append(row)
    totals = {}
    if with_stock and rows:
        if int(sizes):
            totals["own_stock"] = sum(own.get(p.article, 0.0) for p in prods)
            totals["mp_stock"] = sum(mp_total(p.article) for p in prods)
        else:
            totals["own_stock"] = sum(r["own_stock"] for r in rows)
            totals["mp_stock"] = sum(r["mp_stock"] for r in rows)
    return {"rows": rows, "count": len(rows), "price_settings": settings, "totals": totals}


_REPLENISH_EXPORT = {
    "article": "Артикул", "name": "Наименование", "barcode": "Баркод",
    "actual_mp": "Карточки", "status_label": "Статус",
    "demand": "Спрос, шт/день", "demand_wb": "Спрос WB, шт/день",
    "demand_oz": "Спрос Ozon, шт/день",
    "sells": "Продано, шт", "returns_qty": "Возвраты, шт", "return_rate": "Возвраты, %",
    "wb_sells": "Продано WB, шт",
    "our_stock": "У нас, шт", "our_cost": "Себестоимость, руб",
    "wb_qty": "WB склад, шт", "wb_avail": "WB доступно, шт", "wb_in_way": "WB в пути, шт",
    "wb_doc": "WB, дней запаса", "wb_def": "WB дефицит, шт", "to_sort": "Дослать на WB, шт",
    "oz_qty": "Ozon склад, шт", "oz_avail": "Ozon доступно, шт", "oz_in_way": "Ozon в пути, шт",
    "oz_doc": "Ozon, дней запаса", "oz_def": "Ozon дефицит, шт",
    "ship_wb": "Отгрузить на WB, шт", "ship_oz": "Отгрузить на Ozon, шт",
    "need_buy": "Купить у поставщика, шт",
    "income": "К перечислению, руб", "margin": "Маржа, руб",
    "margin_per_one": "Маржа/шт, руб", "margin_pct": "Рентабельность, %",
}


_REPLENISH_EXPORT_SIZES = {
    "article": "Артикул", "size": "Размер", "barcode": "Штрихкод",
    "name": "Наименование", "actual_mp": "Карточки", "status_label": "Статус",
    "wb_sells": "Продано WB, шт", "wb_ret": "Возвраты WB, шт",
    "wb_net": "Продажи WB нетто, шт", "wb_vel": "Спрос WB, шт/день",
    "wb_qty": "WB склад, шт", "wb_avail": "WB доступно, шт", "wb_in_way": "WB в пути, шт",
    "wb_doc": "WB, дней запаса", "wb_def": "WB дефицит, шт", "to_sort": "Дослать на WB, шт",
    "ship_wb": "Отгрузить на WB, шт",
    "our_stock": "У нас, шт",
    "margin_per_one": "Маржа/шт, руб", "margin_pct": "Рентабельность, %", "margin": "Маржа, руб",
}


#: Ключи колонок из «Вида таблицы» → ключи строк экспорта. В таблице статус
#: рисуется тегом по ключу ``status`` (в строке нужны и код, и подпись), а в
#: карточке PDF и в Excel печатается готовая подпись по ключу ``status_label``.
_REPLENISH_COL_ALIASES = {"status": "status_label"}


def _replenish_cols(cols: Optional[str], key_map: dict) -> list[str]:
    """CSV ключей видимых колонок UI → ключи, которые есть в карте экспорта."""
    out: list[str] = []
    for raw in (cols or "").split(","):
        k = _REPLENISH_COL_ALIASES.get(raw.strip(), raw.strip())
        if k and k in key_map and k not in out:
            out.append(k)
    return out


PDF_MEDIA = "application/pdf"

#: Сколько товаров класть в один PDF по умолчанию. Замерено на живых данных:
#: 100 карточек × 4 фото ≈ 18 МБ и ≈ 20 с (миниатюры уже в кэше `data/thumbs`).
PDF_DEFAULT_LIMIT = 100
#: Жёсткий потолок, чтобы случайный `limit=100000` не положил сервер.
PDF_MAX_LIMIT = 500
#: Потолок фото на карточку (предел разумной полосы на страницу).
PDF_MAX_PHOTOS = 9


def _fmt_days(v) -> str:
    """Дни для подписи PDF: 7.5 -> «7.5», 8.0 -> «8» (без хвостовых нулей)."""
    return f"{float(v):.2f}".rstrip("0").rstrip(".") or "0"


_MAX_CARDS_UPLOAD = 100 * 1024 * 1024  # 100 МБ


@router.get("/cards")
def api_cards(
    marketplace: str = "wb",
    like: Optional[str] = None,
    limit: int = 300,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Сырые карточки маркетплейса (marketplace_cards): одна строка = размер/SKU.

    Серверная пагинация: offset/limit; total — реальное число строк по фильтру.
    """
    base_where = [models.Marketplace.code == marketplace]
    if like and like.strip():
        base_where.append(
            common_service.like_col(
                func.concat(
                    models.MarketplaceCard.vendor_code, " ",
                    models.MarketplaceCard.barcode, " ",
                    models.MarketplaceCard.brand, " ",
                    models.MarketplaceCard.name, " ",
                    models.MarketplaceCard.nm_id,
                ),
                like,
            )
        )
    total = db.scalar(
        select(func.count(func.distinct(models.MarketplaceCard.id)))
        .select_from(models.MarketplaceCard)
        .join(models.Marketplace, models.MarketplaceCard.marketplace_id == models.Marketplace.id)
        .where(*base_where)
    ) or 0
    limit = min(max(int(limit), 1), 5000)
    offset = max(int(offset), 0)
    q = (
        select(models.MarketplaceCard)
        .join(models.Marketplace, models.MarketplaceCard.marketplace_id == models.Marketplace.id)
        .where(*base_where)
        .order_by(models.MarketplaceCard.imported_at.desc(), models.MarketplaceCard.id.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = db.execute(q).scalars().all()
    return {
        "marketplace": marketplace,
        "total": total,
        "count": len(rows),
        "offset": offset,
        "limit": limit,
        "rows": [
            {
                "chrt_id": r.chrt_id,
                "nm_id": r.nm_id,
                "vendor_code": r.vendor_code,
                "brand": r.brand,
                "subject": r.subject,
                "size": r.size,
                "barcode": r.barcode,
                "volume_l": float(r.volume_l or 0),
                "composition": r.composition,
                "name": r.name,
                "imported_at": str(r.imported_at) if r.imported_at else "",
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# Загрузки данных через WB API (раздел «WB API» в меню)
# ---------------------------------------------------------------------------
XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _xlsx_response(df: pd.DataFrame, filename: str, count: int):
    buf = excel_io.df_to_excel_stream(df)
    return StreamingResponse(
        buf,
        media_type=XLSX_MEDIA,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Count": str(count),
        },
    )


def _pull_json(res: dict):
    return {"ok": True, "count": res.get("count", 0), "rows": res.get("rows", 0),
            "window": res.get("window", "")}


OZON_DETAIL_RU_COLUMNS = {
    "date": "Дата", "posting_number": "Постинг", "offer_id": "Артикул",
    "name": "Наименование", "sku": "SKU", "barcode": "Штрихкод",
    "quantity": "Кол-во", "seller_price": "Цена, руб", "amount": "Сумма, руб",
    "commission_ratio": "Доля комиссии", "commission": "Комиссия, руб",
    "standard_fee": "Услуги, руб", "income": "К перечислению, руб",
    "return_qty": "Возврат, шт", "return_total": "Возврат, руб",
}

OZON_ACCRUAL_RU_COLUMNS = {
    "date": "Дата", "accrual_id": "ID начисления", "bucket": "Корзина",
    "type_id": "Тип", "sku": "SKU", "offer_id": "Артикул",
    "unit_number": "Постинг", "quantity": "Кол-во",
    "amount": "Сумма, руб", "seller_price": "Цена, руб",
    "sale_price": "Цена покупателя, руб",
}

OZON_BUYOUT_RU_COLUMNS = {
    "posting_number": "Постинг", "offer_id": "Артикул", "name": "Наименование",
    "sku": "SKU", "quantity": "Кол-во", "seller_price": "Цена, руб",
    "buyout_price": "Цена выкупа, руб", "amount": "Сумма выкупа, руб",
    "deduction_by_category_percent": "Дед., %", "vat_percent": "НДС, %",
}


OZON_PLACEMENT_RU_COLUMNS = {
    "date": "Дата", "sku": "SKU", "offer_id": "Артикул",
    "warehouse": "Склад", "paid_quantity": "Платных экз.",
    "paid_volume": "Платный объём, мл", "storage": "Начислено, руб",
}

OZON_PLACEMENT_SUMMARY_RU_COLUMNS = {
    "article": "Артикул", "name": "Наименование", "size": "Размер",
    "sizes_count": "Размеров", "offers_count": "Артикулов",
    "days": "Дней хранения",
    "paid_quantity": "Платных экз.", "paid_volume": "Платный объём, мл",
    "storage": "Начислено, руб", "ops_count": "Операций",
}

OZON_CASHFLOW_RU_COLUMNS = {
    "period_begin": "Период с", "period_end": "Период по",
    "begin_balance": "Баланс на начало",
    "payments_amount": "Выплаты на р/с",
    "delivery_total": "Логистика", "return_total": "Возвраты",
    "services_total": "Услуги", "others_total": "Прочее",
    "end_balance": "Баланс на конец",
}


def _ozon_placement_agg(db, from_, to_, article_like=None, by_size=False) -> list:
    """Свод размещений Ozon по товару (или по артикулу размера).

    Общая часть для /ozon/placement-summary и экспорта, чтобы by_size и
    группировка по базовому артикулу нигде не расходились.
    """
    q = select(models.OzonPlacement).where(models.OzonPlacement.date.isnot(None))
    if from_:
        q = q.where(models.OzonPlacement.date >= from_)
    if to_:
        q = q.where(models.OzonPlacement.date <= to_)
    if article_like:
        q = q.where(common_service.like_col(models.OzonPlacement.offer_id, article_like)
                    | common_service.like_col(models.OzonPlacement.base_article, article_like))
    rows = list(db.execute(q).scalars().all())
    omap = ozon_article.build_offer_map(
        db, offers={(r.offer_id or "").strip() for r in rows})
    cells: dict = {}
    titles: dict = {}
    group_sizes: dict = {}
    group_offers: dict = {}
    for r in rows:
        art = (r.offer_id or "").strip()
        if not art:
            continue
        key = ozon_article.group_key(art, omap, by_size)
        c = cells.setdefault(key, [set(), 0, 0.0, 0.0, 0])
        if r.date:
            c[0].add(r.date)
        c[1] += int(r.paid_quantity or 0)
        c[2] += float(r.paid_volume or 0)
        c[3] += float(r.storage or 0)
        c[4] += 1  # операций
        titles.setdefault(key, art)
        _sz = (r.size or "").strip() or ozon_article.size_of(art, omap)
        if _sz:
            group_sizes.setdefault(key, set()).add(_sz)
        group_offers.setdefault(key, set()).add(art)
    out = []
    for key, c in cells.items():
        offers = sorted(group_offers.get(key) or ([key] if by_size else []))
        out.append({
            "article": key, "name": titles.get(key, ""),
            "size": (ozon_article.size_of(offers[0], omap) if by_size and offers else ""),
            "sizes_count": len(group_sizes.get(key) or ()),
            "offers_count": len(offers) or 1,
            "days": len(c[0]), "paid_quantity": c[1],
            "paid_volume": round(c[2], 2), "storage": round(c[3], 2),
            "ops_count": c[4],
        })
    return sorted(out, key=lambda x: x["storage"])


# ------------------------------------------------------------------ тикеты

def _ticket_or_error(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except tickets_service.TicketError as e:
        raise HTTPException(status_code=e.status, detail=str(e))


# ----------------------------------------------------------- акции WB (календарь)


def _promo_row(p: models.Promotion) -> dict:
    """Строка акции для UI: участие, потолок, уровни ranging."""
    from app.services import pricing as _pricing

    tiers = _pricing._parse_ranging(p.ranging_json)  # noqa: SLF001
    return {
        "promo_id": p.promo_id,
        "name": p.name or "",
        "adv_type": p.adv_type or "",
        "starts_at": p.starts_at.isoformat(sep=" ") if p.starts_at else None,
        "ends_at": p.ends_at.isoformat(sep=" ") if p.ends_at else None,
        "participation_percent": float(p.participation_percent)
        if p.participation_percent is not None else None,
        "in_promo_total": p.in_promo_total or 0,
        "not_in_promo_total": p.not_in_promo_total or 0,
        "exception_count": p.exception_count or 0,
        "cap_pct": _pricing._promo_cap_pct(p.description),
        "description": (p.description or "")[:300],
        "tiers": tiers,
        "fetched_at": p.fetched_at.isoformat(sep=" ") if p.fetched_at else None,
    }
