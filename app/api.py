import re
import zipfile
from datetime import date, timedelta
from io import BytesIO
from typing import List, Optional

import pandas as pd
from fastapi import APIRouter, Body, Depends, HTTPException, UploadFile, File
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.config import settings
from app.database import get_db
from app.providers import factory as provider_factory
from app.providers.ozon import OZON_RU_COLUMNS
from app.providers.wb import DETAIL_RU_COLUMNS, DETAIL_UPLOAD_RENAME, SALES_RU_COLUMNS, V5_RU_COLUMNS
from app.services import (
    common as common_service,
    excel_io,
    margin as margin_service,
    pricing as pricing_service,
    refresh as refresh_service,
    sync as sync_service,
    tickets as tickets_service,
    warehouse as warehouse_service,
    yandex_disk as yandex_service,
)
from app.services.window import parse_window

router = APIRouter(prefix="/api")


def _parse_window400(date_from=None, date_to=None):
    """parse_window + превращает некорректную дату в HTTP 400 (а не 500)."""
    try:
        return parse_window(date_from, date_to)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Некорректная дата: {e}")


def _df_totals(df: pd.DataFrame) -> dict:
    """Итоговая строка по числовым колонкам df (для UI-футера)."""
    if df is None or df.empty:
        return {}
    skip = {"margin_pct", "delta_pct", "margin_per_one", "net_cost_est",
            "commission_per_one", "logistics_per_one", "logistics_out_per_one",
            "logistics_in_per_one", "storage_per_one", "income_per_one",
            "revenue_per_one", "margin_gross_per_one", "return_rate"}
    out: dict = {}
    for c in df.columns:
        if c in skip:
            continue
        s = df[c]
        if s.dtype.kind in "iuf":
            out[c] = round(float(s.sum()), 2)
    return out


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
    return {"rows": rows, "count": len(rows), "date_from": str(from_), "date_to": str(to_)}


@router.get("/margin")
def api_margin(
    marketplace: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    source: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)

    mp_ids = common_service.resolve_marketplace_ids(db, marketplace)

    df = margin_service.margin_dataframe(
        db, date_from=from_, date_to=to_, marketplace=mp_ids,
        article_like=article_like, source=source,
    )
    return {
        "rows": df.replace({None: ""}).to_dict("records"),
        "count": len(df),
    }


@router.get("/margin/detail")
def api_margin_detail(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    compare: int = 0,
    db: Session = Depends(get_db),
):
    """Прибыльность по Детализации Продаж WB напрямую из wb_detail_rows.

    Себестоимость из каталога; без неё — оценка (settings.default_net_cost),
    помечается net_cost_est=True.

    compare=1 добавляет показатели предыдущего аналогичного периода
    (та же длительность окна, сдвинутая назад): sells_pp, margin_pp,
    delta_ru (руб), delta_pct (%). Требует даты date_from/date_to.
    """
    from_, to_ = _parse_window400(date_from, date_to)

    df = margin_service.margin_detail_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like,
        default_net_cost=settings.default_net_cost,
    )
    prev_window = None
    if compare and date_from and date_to:
        try:
            f = date.fromisoformat(date_from)
            t = date.fromisoformat(date_to)
            delta = (t - f).days
            prev_from = f - timedelta(days=delta)
            prev_to = f - timedelta(days=1)
            prev_df = margin_service.margin_detail_dataframe(
                db, date_from=prev_from, date_to=prev_to, article_like=article_like,
                default_net_cost=settings.default_net_cost,
            )
            df = margin_service.compare_margin_periods(df, prev_df)
            prev_window = {"date_from": prev_from.isoformat(), "date_to": prev_to.isoformat()}
        except (ValueError, TypeError):
            prev_window = None
    detail_articles = 0
    if from_ and to_:
        q = select(func.count(func.distinct(models.WbDetailRow.article))).where(
            models.WbDetailRow.article != "",
            models.WbDetailRow.sale_dt.isnot(None),
            models.WbDetailRow.sale_dt >= from_,
            models.WbDetailRow.sale_dt <= to_,
        )
        if article_like:
            q = q.where(models.WbDetailRow.article.ilike(f"%{article_like}%"))
        detail_articles = int(db.execute(q).scalar_one() or 0)
    estimated = int(df["net_cost_est"].sum()) if not df.empty and "net_cost_est" in df else 0
    totals = _df_totals(df)
    return {
        "rows": df.replace({None: ""}).to_dict("records"),
        "count": len(df),
        "detail_articles": detail_articles,
        "estimated": estimated,
        "default_net_cost": settings.default_net_cost,
        "prev_window": prev_window,
        "totals": totals,
    }


@router.get("/margin/funnel")
def api_margin_funnel(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Прибыльность по Воронке Продаж WB (данные funnel_metric, оценка)."""
    from_, to_ = _parse_window400(date_from, date_to)

    df = margin_service.funnel_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like
    )
    return {
        "rows": df.replace({None: ""}).to_dict("records"),
        "count": len(df),
        "snapshot_from": df.attrs.get("date_from", ""),
        "snapshot_to": df.attrs.get("date_to", ""),
    }


@router.get("/funnel")
def api_funnel(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Строки воронки продаж WB из funnel_metric.

    Без дат — окно последней загрузки (max(date_to)); иначе заданное окно.
    """
    snapshot = db.execute(
        select(func.max(models.FunnelMetric.date_to))
    ).scalar()
    if date_from is None or date_to is None:
        win = db.execute(
            select(models.FunnelMetric.date_from, models.FunnelMetric.date_to)
            .order_by(models.FunnelMetric.date_to.desc(), models.FunnelMetric.date_from.desc())
            .limit(1)
        ).first()
        if win is None:
            return {"rows": [], "count": 0, "date_from": "", "date_to": "",
                    "snapshot_from": "", "snapshot_to": ""}
        from_, to_ = win[0], win[1]
    else:
        from_, to_ = _parse_window400(date_from, date_to)

    name_subq = (
        select(models.Product.name)
        .where(models.Product.article == models.FunnelMetric.article)
        .limit(1)
        .scalar_subquery()
    )
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
        )
        .where(models.FunnelMetric.date_from == from_,
               models.FunnelMetric.date_to == to_)
        .order_by(models.FunnelMetric.revenue.desc(), models.FunnelMetric.article)
    )
    if article_like:
        q = q.where(models.FunnelMetric.article.ilike(f"%{article_like}%"))

    rows = [
        {
            "date_from": str(r.date_from), "date_to": str(r.date_to),
            "nm_id": str(r.nm_id or ""), "article": str(r.article),
            "name": str(r.name or ""),
            "views": int(r.views or 0), "opens": int(r.opens or 0),
            "adds": int(r.adds or 0), "orders": int(r.orders or 0),
            "cancelled": int(r.cancelled or 0), "buyouts": int(r.buyouts or 0),
            "avg_price": float(r.avg_price or 0), "revenue": float(r.revenue or 0),
            "buyout_sum": float(r.buyout_sum or 0),
        }
        for r in db.execute(q)
    ]
    return {"rows": rows, "count": len(rows),
            "date_from": str(from_), "date_to": str(to_),
            "snapshot_to": str(snapshot or "")}


@router.get("/pulls")
def api_pulls(db: Session = Depends(get_db)):
    """Лог последних успешных загрузок из API маркетплейсов."""
    from sqlalchemy import desc

    rows = db.execute(
        select(
            models.ApiPull.api, models.ApiPull.kind,
            models.ApiPull.last_success_at, models.ApiPull.rows,
            models.ApiPull.db_rows, models.ApiPull.window,
        ).order_by(desc(models.ApiPull.last_success_at))
    ).all()
    return [
        {
            "api": api, "kind": kind,
            "last_success_at": last_success_at.isoformat(sep=" ") if last_success_at else None,
            "rows": rows_n, "db_rows": db_rows, "window": window,
        }
        for api, kind, last_success_at, rows_n, db_rows, window in rows
    ]


def _real_ozon_articles(db, from_: date, to_: date):
    """Реальные артикулы Ozon = offer_id из живого /v2/finance/realization за месяцы окна."""
    real: set = set()
    months = set()
    yy, mm = from_.year, from_.month
    while True:
        months.add((yy, mm))
        if (yy, mm) == (to_.year, to_.month):
            break
        mm += 1
        if mm == 13:
            mm, yy = 1, yy + 1

    ok, fail = 0, 0
    try:
        prov = provider_factory.get_oz_provider()
        for year_, month_ in sorted(months):
            try:
                df = prov.get_realization(month_, year_)
                ok += 1
                for offer_id in df["offer_id"]:
                    s = str(offer_id).strip()
                    if s:
                        real.add(s)
            except Exception:  # noqa: BLE001
                fail += 1
    except Exception as exc:  # noqa: BLE001
        return real, f"OzonProvider не доступен: {exc}"

    if real or (ok and not fail):
        return real, f"live /v2/finance/realization (ok={ok}, fail={fail})"

    # фоллбэк-эвристика: реальные артикулы Ozon не выглядят как моки
    q = (
        select(models.Sale.article)
        .join(models.Marketplace, models.Sale.marketplace_id == models.Marketplace.id)
        .where(models.Marketplace.code == "ozon")
    )
    for (art,) in db.execute(q):
        s = str(art).strip()
        if s and not re.fullmatch(r"\d+", s) and not re.fullmatch(r"JBG-10\d{2}", s):
            real.add(s)
    return real, f"fallback-heuristic (live недоступен: ok={ok}, fail={fail})"


def _pick_test_articles(db, by_art, real_ozon):
    """Автоотбор: реальные Ozon с себестоимостью + без неё (демо), плюс WB."""
    def key(a):
        return by_art[a]["income"]

    real_cost = sorted(
        (a for a in real_ozon if a in by_art and by_art[a]["net_cost"] > 0),
        key=key, reverse=True,
    )[:7]
    real_nocost = sorted(
        (a for a in real_ozon if a in by_art
         and by_art[a]["net_cost"] <= 0 and by_art[a]["sells"] > 0),
        key=key, reverse=True,
    )[:2]
    wb = sorted(
        (a for a in by_art if "wb" in by_art[a]["marketplaces"]
         and by_art[a]["net_cost"] > 0),
        key=key, reverse=True,
    )[:2]
    return real_cost + real_nocost + wb


@router.get("/margin/test")
def api_margin_test(
    articles: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Тест маржинальности на выборке товаров (read-only).

    Сверяет значения из БД с эталоном реальных артикулов Ozon
    (живой /v2/finance/realization) и ставит флаги источника/себестоимости.
    """
    from_, to_ = _parse_window400(date_from, date_to)

    real_ozon, prov_note = _real_ozon_articles(db, from_, to_)

    agg_q = (
        select(
            models.Marketplace.code.label("marketplace"),
            models.Sale.article,
            func.max(models.Product.name).label("name"),
            func.sum(models.Sale.quantity).label("sells"),
            func.sum(models.Sale.returns_qty).label("returns_qty"),
            func.sum(models.Sale.revenue).label("revenue"),
            func.sum(models.Sale.commission).label("commission"),
            func.sum(models.Sale.logistics).label("logistics"),
            func.sum(models.Sale.storage).label("storage"),
            func.sum(models.Sale.services).label("services"),
            func.sum(models.Sale.income).label("income"),
            func.max(models.Product.net_cost).label("net_cost"),
        )
        .select_from(models.Sale)
        .join(models.Marketplace, models.Sale.marketplace_id == models.Marketplace.id)
        .join(models.Product, models.Sale.article == models.Product.article)
        .where(models.Sale.date >= from_, models.Sale.date <= to_)
        .group_by(models.Marketplace.code, models.Sale.article)
        .order_by(models.Sale.article)
    )

    by_art: dict = {}
    for r in db.execute(agg_q):
        mp, art = r.marketplace, str(r.article)
        rec = by_art.setdefault(art, {
            "article": art, "name": r.name or "",
            "marketplaces": [], "sells": 0, "returns_qty": 0,
            "revenue": 0.0, "commission": 0.0, "logistics": 0.0,
            "storage": 0.0, "services": 0.0, "income": 0.0,
            "net_cost": float(r.net_cost or 0),
            "income_ozon": 0.0, "income_wb": 0.0,
        })
        rec["name"] = rec["name"] or (r.name or "")
        rec["marketplaces"].append(mp)
        rec["sells"] += int(r.sells or 0)
        rec["returns_qty"] += int(r.returns_qty or 0)
        rec["revenue"] += float(r.revenue or 0)
        rec["commission"] += float(r.commission or 0)
        rec["logistics"] += float(r.logistics or 0)
        rec["storage"] += float(r.storage or 0)
        rec["services"] += float(r.services or 0)
        rec["income"] += float(r.income or 0)
        if mp == "ozon":
            rec["income_ozon"] += float(r.income or 0)
        elif mp == "wb":
            rec["income_wb"] += float(r.income or 0)

    requested = None
    if articles:
        requested = [a.strip() for a in articles.split(",") if a.strip()]

    tested = requested if requested is not None else _pick_test_articles(db, by_art, real_ozon)
    if requested is not None:
        tested = [a for a in requested if a in by_art]

    rows = []
    for art in tested:
        rec = by_art[art]
        nc = rec["net_cost"]
        margin = rec["income"] - nc * rec["sells"]
        margin_per_one = margin / rec["sells"] if rec["sells"] else 0.0
        margin_pct = margin / rec["income"] * 100 if rec["income"] else 0.0
        comp = rec["revenue"] + rec["commission"] + rec["logistics"] + rec["storage"] + rec["services"]
        other = rec["income"] - comp
        src_parts = []
        if "ozon" in rec["marketplaces"]:
            src_parts.append("Ozon (реал)" if art in real_ozon else "Ozon (мок)")
        if "wb" in rec["marketplaces"]:
            src_parts.append("WB")
        rows.append({
            "article": art,
            "name": rec["name"],
            "marketplaces": rec["marketplaces"],
            "source": " + ".join(src_parts),
            "is_real": art in real_ozon,
            "has_net_cost": nc > 0,
            "sells": rec["sells"],
            "returns_qty": rec["returns_qty"],
            "revenue": round(rec["revenue"], 2),
            "commission": round(rec["commission"], 2),
            "logistics": round(rec["logistics"], 2),
            "storage": round(rec["storage"], 2),
            "services": round(rec["services"], 2),
            "other": round(other, 2),
            "income": round(rec["income"], 2),
            "income_ozon": round(rec["income_ozon"], 2),
            "income_wb": round(rec["income_wb"], 2),
            "net_cost": nc,
            "cost_total": round(nc * rec["sells"], 2),
            "margin": round(margin, 2),
            "margin_per_one": round(margin_per_one, 2),
            "margin_pct": round(margin_pct, 2),
            "identity_ok": abs(other) <= 0.02,
        })
    rows.sort(key=lambda x: (-x["sells"], x["article"]))

    sold_no_cost = sorted(
        a for a, rec in by_art.items() if rec["net_cost"] <= 0 and rec["sells"] > 0
    )
    ozon_mock_left = sum(
        1 for a, rec in by_art.items()
        if "ozon" in rec["marketplaces"] and a not in real_ozon and rec["sells"] > 0
    )

    return {
        "rows": rows,
        "count": len(rows),
        "date_from": str(from_),
        "date_to": str(to_),
        "meta": {
            "real_ozon": sorted(real_ozon),
            "real_ozon_note": prov_note,
            "sold_without_cost": len(sold_no_cost),
            "sold_without_cost_sample": sold_no_cost[:10],
            "ozon_mock_leftovers": ozon_mock_left,
            "articles_param": requested is not None,
        },
    }


@router.get("/dashboard")
def api_dashboard(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)

    query = (
        select(
            models.Marketplace.code.label("marketplace"),
            func.sum(models.Sale.quantity).label("sells"),
            func.sum(models.Sale.revenue).label("revenue"),
            func.sum(models.Sale.income).label("income"),
            func.sum(models.Sale.commission).label("commission"),
            func.sum(models.Sale.logistics).label("logistics"),
            func.sum(models.Sale.storage).label("storage"),
        )
        .select_from(models.Sale)
        .join(models.Marketplace, models.Sale.marketplace_id == models.Marketplace.id)
        .where(models.Sale.date >= from_, models.Sale.date <= to_,
               models.Sale.source != "detail")
        .group_by(models.Marketplace.code)
    )
    per_mp = []
    total = {"sells": 0, "revenue": 0.0, "income": 0.0}
    for r in db.execute(query):
        per_mp.append({
            "marketplace": r.marketplace,
            "sells": int(r.sells or 0),
            "revenue": float(r.revenue or 0),
            "income": float(r.income or 0),
            "commission": float(r.commission or 0),
            "logistics": float(r.logistics or 0),
            "storage": float(r.storage or 0),
        })
        total["sells"] += int(r.sells or 0)
        total["revenue"] += float(r.revenue or 0)
        total["income"] += float(r.income or 0)

    daily_q = (
        select(
            models.Sale.date,
            func.sum(models.Sale.revenue).label("revenue"),
            func.sum(models.Sale.income).label("income"),
            func.sum(models.Sale.quantity).label("sells"),
        )
        .where(models.Sale.date >= from_, models.Sale.date <= to_,
               models.Sale.source != "detail")
        .group_by(models.Sale.date)
        .order_by(models.Sale.date)
    )
    daily = [
        {
            "date": str(r.date),
            "revenue": float(r.revenue or 0),
            "income": float(r.income or 0),
            "sells": int(r.sells or 0),
        }
        for r in db.execute(daily_q)
    ]
    return {
        "per_marketplace": per_mp,
        "total": total,
        "daily": daily,
        "date_from": str(from_),
        "date_to": str(to_),
    }


@router.get("/stocks")
def api_stocks(
    marketplace: Optional[str] = None,
    db: Session = Depends(get_db),
):
    latest = db.scalar(select(func.max(models.Stock.date)))
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
    article_like: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Срез текущих цен/скидок WB из price_snapshots (последняя загрузка)."""
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
        .where(models.PriceSnapshot.marketplace == "wb")
        .order_by(models.PriceSnapshot.article, models.PriceSnapshot.size)
    )
    if article_like:
        q = q.where(models.PriceSnapshot.article.ilike(f"%{article_like}%"))
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
        q = q.where(models.StorageCost.article.ilike(f"%{article_like}%"))
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


@router.get("/products")
def api_products(db: Session = Depends(get_db)):
    rows = [
        {
            "article": r.article,
            "name": r.name,
            "brand": r.brand,
            "barcode": r.barcode,
            "net_cost": float(r.net_cost or 0),
            "replenishable": bool(r.replenishable),
        }
        for r in db.execute(
            select(
                models.Product.article,
                models.Product.name,
                models.Product.brand,
                models.Product.barcode,
                models.Product.net_cost,
                models.Product.replenishable,
            ).order_by(models.Product.article)
        )
    ]
    return {"rows": rows, "count": len(rows)}


@router.post("/products/replenishable")
def product_replenishable(payload: dict = Body(...), db: Session = Depends(get_db)):
    """Переключает флаг «докупаемый» у товара (влияет на автопилот цен WB)."""
    article = str(payload.get("article", "")).strip()
    if not article:
        raise HTTPException(status_code=400, detail="Не указан article")
    prod = db.scalar(select(models.Product).where(models.Product.article == article))
    if prod is None:
        raise HTTPException(status_code=404, detail=f"Товар {article} не найден")
    prod.replenishable = bool(payload.get("value", False))
    db.commit()
    return {"article": article, "replenishable": bool(prod.replenishable)}


# ---------------------------------------------------------------------------
# Автопилот цен Wildberries
# ---------------------------------------------------------------------------
@router.get("/pricing/defaults")
def pricing_defaults():
    return {"defaults": pricing_service.PRICING_DEFAULTS}


@router.post("/pricing/recommendations")
def pricing_recommendations(payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """Read-only расчёт рекомендаций по правилам R1-R10. body = настройки (перекрытие дефолтов)."""
    prices_df = provider_factory.get_wb_provider().get_prices()
    return pricing_service.recommendations(db, settings=payload, prices_df=prices_df)


@router.post("/pricing/apply")
def pricing_apply(payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """Применяет рекомендованные скидки через WB API upload/task и пишет журнал price_changes."""
    prov = provider_factory.get_wb_provider()
    prices_df = prov.get_prices()
    try:
        return pricing_service.apply_recommendations(
            db, settings=payload, prices_df=prices_df, provider=prov,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"WB API не принял изменение цен: {e}")


@router.get("/pricing/history")
def pricing_history(limit: int = 50, db: Session = Depends(get_db)):
    """Журнал решений автопилота (последние события)."""
    rows = db.execute(
        select(models.PriceChange)
        .order_by(models.PriceChange.calculated_at.desc())
        .limit(min(max(limit, 1), 200))
    ).scalars().all()
    return {
        "rows": [
            {
                "article": r.article,
                "nm_id": r.nm_id,
                "calculated_at": str(r.calculated_at) if r.calculated_at else None,
                "applied_at": str(r.applied_at) if r.applied_at else None,
                "before_discount": float(r.before_discount or 0),
                "after_discount": float(r.after_discount) if r.after_discount is not None else None,
                "action": r.action,
                "status": r.status,
                "reason": r.reason,
            }
            for r in rows
        ],
        "count": len(rows),
    }


PRICING_ACTION_RU = {
    "RAISE": "поднять цену", "LOWER": "снизить цену", "HOLD": "держать", "SKIP": "пропустить",
}


@router.post("/pricing/export")
def pricing_export(payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """Рекомендации автопилота в Excel. Изменения в WB API НЕ вносятся."""
    cols = payload.get("cols")
    prices_df = provider_factory.get_wb_provider().get_prices()
    rec = pricing_service.recommendations(db, settings=payload, prices_df=prices_df)
    df = pd.DataFrame(rec["rows"])
    if not df.empty:
        df["action"] = df["action"].map(PRICING_ACTION_RU)
        df["replenishable"] = df["replenishable"].map({True: "да", False: "нет"})
    keep = [
        "article", "name", "price", "current_vis", "current_discount", "target_vis",
        "target_discount", "action", "status", "reason", "doc", "velocity", "trend",
        "conv_pct", "backlog", "stock", "avg_price", "eff", "floor_price",
        "max_discount_item", "margin_pct_at_target", "replenishable",
    ]
    df = df[[c for c in keep if c in df.columns]]
    df, ru = excel_io.project_export(df, {
        "article": "Артикул", "name": "Наименование", "price": "Цена базовая, руб",
        "current_vis": "Цена сейчас, руб", "current_discount": "Скидка сейчас, %",
        "target_vis": "Целевая цена, руб", "target_discount": "Целевая скидка, %",
        "action": "Решение", "status": "Статус", "reason": "Причина",
        "doc": "DOC, дн", "velocity": "Продажи, шт/дн", "trend": "Тренд",
        "conv_pct": "Конверсия, %", "backlog": "В корзине", "stock": "Остаток",
        "avg_price": "Ср. цена факт, руб", "eff": "База расчёта, руб",
        "floor_price": "Пол (break-even), руб", "max_discount_item": "Макс. скидка, %",
        "margin_pct_at_target": "Маржа при цели, %", "replenishable": "Докупаемый",
    }, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Автопилот")
    fname = f"pricing_{date.today().isoformat()}.xlsx"
    return StreamingResponse(
        buf,
        media_type=XLSX_MEDIA,
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "X-Count": str(len(df)),
        },
    )


@router.get("/custom-stock")
def api_custom_stock(db: Session = Depends(get_db)):
    rows = [
        {
            "article": r.article,
            "name": r.name,
            "quantity": int(r.quantity or 0),
            "net_cost": float(r.net_cost or 0),
            "updated_at": str(r.updated_at) if r.updated_at else "",
        }
        for r in db.execute(
            select(
                models.CustomStock.article,
                models.CustomStock.quantity,
                models.CustomStock.net_cost,
                models.CustomStock.updated_at,
                models.Product.name,
            )
            .select_from(models.CustomStock)
            .join(models.Product, models.CustomStock.article == models.Product.article, isouter=True)
        )
    ]
    return {"rows": rows, "count": len(rows)}


@router.post("/import/products")
async def import_products(file: UploadFile = File(...), db: Session = Depends(get_db)):
    data = await file.read()
    df = excel_io.read_excel_bytes(data)
    df = df.rename(columns={
        "Артикул поставщика": "article",
        "Артикул": "article",
        "article": "article",
        "Себестоимость": "net_cost",
        "Себестоимость БАЗА": "net_cost",
        "net_cost": "net_cost",
        "Наименование": "name",
        "name": "name",
    })
    n = sync_service.upsert_products(db, df)
    return {"imported": n, "total": len(df), "filename": file.filename}


@router.post("/import/net-cost")
async def import_net_cost(file: UploadFile = File(...), db: Session = Depends(get_db)):
    """Обновляет себестоимость (net_cost) товаров из файла с колонками article/net_cost."""
    data = await file.read()
    df = excel_io.read_excel_bytes(data)
    df = df.rename(columns={
        "Артикул": "article",
        "article": "article",
        "Себестоимость": "net_cost",
        "net_cost": "net_cost",
    })
    if "article" not in df.columns or "net_cost" not in df.columns:
        raise HTTPException(status_code=400, detail="В файле нет колонок article и net_cost")
    n = sync_service.upsert_products(db, df)
    filled = common_service.count_products_with_cost(db)
    return {"imported": n, "total": len(df), "with_cost_total": filled, "filename": file.filename}


@router.post("/import/custom-stock")
async def import_custom_stock(file: UploadFile = File(...), db: Session = Depends(get_db)):
    data = await file.read()
    df = excel_io.read_excel_bytes(data)
    df = df.rename(columns={
        "Артикул": "article",
        "Количество": "quantity",
        "Кол-во": "quantity",
        "Закупочная цена": "net_cost",
        "Закуп. цена": "net_cost",
        "Себестоимость": "net_cost",
    })
    n = sync_service.upsert_custom_stock(db, df)
    return {"imported": n, "filename": file.filename}


# ── «Наш склад»: контрагенты, приход/отгрузка, остатки ────────────────────────

@router.get("/warehouse/counterparties")
def api_cp_list(db: Session = Depends(get_db)):
    rows = [
        {
            "id": cp.id, "name": cp.name, "inn": cp.inn, "ctype": cp.ctype,
            "phone": cp.phone, "note": cp.note,
        }
        for cp in db.query(models.Counterparty).order_by(models.Counterparty.name).all()
    ]
    return {"rows": rows, "count": len(rows), "labels": warehouse_service.CP_TYPE_LABELS}


@router.post("/warehouse/counterparties")
def api_cp_save(
    id: Optional[int] = None,
    name: str = Body(...),
    inn: str = Body(""),
    ctype: str = Body("other"),
    phone: str = Body(""),
    note: str = Body(""),
    db: Session = Depends(get_db),
):
    if id:
        cp = db.get(models.Counterparty, id)
        if not cp:
            raise HTTPException(404, "Контрагент не найден")
    else:
        existing = db.query(models.Counterparty).filter(models.Counterparty.name == name).first()
        if existing:
            cp = existing
        else:
            cp = models.Counterparty(name=name)
            db.add(cp)
    cp.name = name
    cp.inn = inn
    cp.ctype = ctype
    cp.phone = phone
    cp.note = note
    db.commit()
    return {"id": cp.id, "ok": True}


@router.delete("/warehouse/counterparties/{cp_id}")
def api_cp_delete(cp_id: int, db: Session = Depends(get_db)):
    cp = db.get(models.Counterparty, cp_id)
    if not cp:
        raise HTTPException(404)
    db.delete(cp)
    db.commit()
    return {"ok": True}


@router.post("/warehouse/import/counterparties")
async def import_cp(file: UploadFile = File(...), db: Session = Depends(get_db)):
    df = excel_io.read_excel_bytes(await file.read(), sheet=0)
    r = warehouse_service.import_counterparties(db, df)
    return {"filename": file.filename, **r}


@router.post("/warehouse/import/docs")
async def import_docs(
    type: str = "receipt",
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    if type not in ("receipt", "shipment"):
        raise HTTPException(400, f"Тип {type!r} не поддерживается")
    df = excel_io.read_excel_bytes(await file.read(), sheet=0)
    r = warehouse_service.import_docs(db, df, doc_type=type)
    return {"filename": file.filename, "type": type, **r}


@router.get("/warehouse/docs")
def api_docs_list(
    type: str = "receipt",
    from_: Optional[str] = None,
    to: Optional[str] = None,
    limit: int = 200,
    db: Session = Depends(get_db),
):
    if type not in ("receipt", "shipment"):
        raise HTTPException(400, "type должен быть receipt или shipment")
    q = db.query(models.WarehouseDoc).filter(models.WarehouseDoc.doc_type == type)
    if from_:
        q = q.filter(models.WarehouseDoc.doc_date >= from_)
    if to:
        q = q.filter(models.WarehouseDoc.doc_date <= to)
    docs = q.order_by(models.WarehouseDoc.doc_date.desc()).limit(limit).all()
    cp_ids = {d.counterparty_id for d in docs if d.counterparty_id}
    names = {}
    if cp_ids:
        names = {c.id: c.name for c in db.query(models.Counterparty).filter(models.Counterparty.id.in_(cp_ids)).all()}
    rows = [
        {
            "id": d.id, "doc_num": d.doc_num, "date": d.doc_date.isoformat() if d.doc_date else "",
            "counterparty": names.get(d.counterparty_id, ""), "total": float(d.total or 0),
            "items_count": len(d.items), "source": d.source,
        }
        for d in docs
    ]
    return {"rows": rows, "count": len(rows), "type": type}


@router.get("/warehouse/docs/{doc_id}/items")
def api_doc_items(doc_id: int, db: Session = Depends(get_db)):
    d = db.get(models.WarehouseDoc, doc_id)
    if not d:
        raise HTTPException(404)
    rows = [
        {"article": it.article, "name": it.name, "quantity": float(it.quantity), "price": float(it.price), "amount": float(it.amount)}
        for it in d.items
    ]
    return {"doc_id": doc_id, "doc_num": d.doc_num, "date": d.doc_date.isoformat() if d.doc_date else "", "type": d.doc_type, "rows": rows}


@router.post("/warehouse/docs")
def api_doc_create(
    type: str = Body("receipt"),
    doc_num: str = Body(""),
    doc_date: str = Body("..."),
    counterparty_id: Optional[int] = Body(None),
    note: str = Body(""),
    items: List[dict] = Body(...),
    db: Session = Depends(get_db),
):
    from datetime import datetime as _dt
    try:
        dt = _dt.fromisoformat(doc_date).date()
    except Exception:
        raise HTTPException(400, "Некорректная дата")
    if not items:
        raise HTTPException(400, "Строки документа пусты")
    r = warehouse_service.create_doc(db, doc_type=type, doc_num=doc_num, doc_date=dt,
                                     counterparty_id=counterparty_id, note=note, items=items)
    return r


@router.delete("/warehouse/docs/{doc_id}")
def api_doc_delete(doc_id: int, db: Session = Depends(get_db)):
    try:
        warehouse_service.delete_doc(db, doc_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@router.get("/warehouse/stock")
def api_stock(article_like: Optional[str] = None, db: Session = Depends(get_db)):
    rows = warehouse_service.stock_view(db, article_like=article_like or "")
    return {"rows": rows, "count": len(rows)}


@router.get("/warehouse/turnover")
def api_turnover(db: Session = Depends(get_db)):
    return {"rows": warehouse_service.turnover_view(db)}


@router.get("/warehouse/export/{kind}")
def api_wh_export(kind: str, type: str = "receipt", db: Session = Depends(get_db)):
    try:
        buf = warehouse_service.file_for(kind, db, doc_type=type)
    except ValueError as e:
        raise HTTPException(400, str(e))
    filename = f"{kind}_{type}.xlsx" if "docs" in kind else f"{kind}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/warehouse/todisk")
async def api_wh_todisk(kind: str = "docs", type: str = "receipt", db: Session = Depends(get_db)):
    """Сформировать Excel и загрузить на Яндекс.Диск в папку /agent_market/Наш склад/<kind>."""
    if kind == "docs" and type not in ("receipt", "shipment"):
        raise HTTPException(400, "type должен быть receipt или shipment")
    try:
        token = yandex_service.require_token()
        buf = warehouse_service.file_for(kind, db, doc_type=type)
        filename = f"{kind}_{type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx" if kind == "docs" else f"{kind}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        folder = f"/agent_market/Наш склад"
        r = yandex_service.upload_bytes(token, folder, filename, buf.read())
        return {"ok": True, "path": r.get("path", f"{folder}/{filename}")}
    except yandex_service.YandexDiskError as e:
        raise HTTPException(502, str(e))


@router.post("/warehouse/fromdisk")
async def api_wh_fromdisk(
    type: str = "receipt",
    db: Session = Depends(get_db),
):
    """Импорт всех .xlsx из папки «Наш склад/Приход или Отгрузка» на Диске."""
    folder_map = {"receipt": "/agent_market/Наш склад/Приход", "shipment": "/agent_market/Наш склад/Отгрузка",
                  "counterparties": "/agent_market/Наш склад/Контрагенты"}
    folder = folder_map.get(type)
    if not folder:
        raise HTTPException(400, f"type {type!r} не поддерживается")
    results = []
    try:
        token = yandex_service.require_token()
        files = yandex_service.list_files(token, folder).get("items", [])
        for f in files:
            if not f["name"].endswith(".xlsx"):
                continue
            data = yandex_service.download_bytes(token, f["path"])
            df = excel_io.read_excel_bytes(data, sheet=0)
            if type == "counterparties":
                r = warehouse_service.import_counterparties(db, df)
            else:
                r = warehouse_service.import_docs(db, df, doc_type=type, source="disk")
            results.append({"file": f["name"], **r})
    except yandex_service.YandexDiskError as e:
        raise HTTPException(502, str(e))
    return {"results": results}


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
        pat = f"%{like.strip()}%"
        base_where.append(
            func.concat(
                models.MarketplaceCard.vendor_code, " ",
                models.MarketplaceCard.barcode, " ",
                models.MarketplaceCard.brand, " ",
                models.MarketplaceCard.name, " ",
                models.MarketplaceCard.nm_id,
            ).ilike(pat)
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


@router.post("/import/cards")
async def import_cards(
    marketplace: str = "wb",
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
):
    """Загрузка карточек товара маркетплейса из Excel.

    Принимает 1 файл, несколько файлов или zip-архив с Excel-файлами (*.xlsx, *.xlsm).
    Пишет в marketplace_cards и обновляет общий каталог (products + nm_articles).
    """
    if marketplace not in ("wb", "ozon"):
        raise HTTPException(status_code=400, detail="marketplace должен быть wb или ozon")
    api_code = "wb" if marketplace == "wb" else "ozon"
    frames = []
    total_rows = 0
    errors = []
    fileinfo = []
    accum = 0
    for upf in files:
        data = await upf.read()
        accum += len(data)
        if accum > _MAX_CARDS_UPLOAD:
            raise HTTPException(status_code=413, detail="Слишком большой объём файлов (> 100 МБ)")
        name = upf.filename or "file"
        if name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(BytesIO(data)) as zf:
                    sub = 0
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        if not info.filename.lower().endswith((".xlsx", ".xlsm")):
                            continue
                        try:
                            d = excel_io.read_excel_bytes(zf.read(info))
                            frames.append(d)
                            sub += len(d)
                        except Exception as e:  # noqa: BLE001
                            errors.append(f"{name}/{info.filename}: {e}")
                fileinfo.append({"name": name, "rows": sub, "ok": True})
                total_rows += sub
            except zipfile.BadZipFile as e:
                errors.append(f"{name}: не является zip-архивом ({e})")
                fileinfo.append({"name": name, "rows": 0, "ok": False, "error": str(e)})
            continue
        if not name.lower().endswith((".xlsx", ".xlsm")):
            errors.append(f"{name}: пропущен (не Excel)")
            fileinfo.append({"name": name, "rows": 0, "ok": False, "error": "не Excel"})
            continue
        try:
            d = excel_io.read_excel_bytes(data)
            frames.append(d)
            total_rows += len(d)
            fileinfo.append({"name": name, "rows": len(d), "ok": True})
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            fileinfo.append({"name": name, "rows": 0, "ok": False, "error": str(e)})
    if not frames:
        raise HTTPException(status_code=400,
                            detail="Нет данных для импорта: " + ("; ".join(errors) or "нет файлов"))
    df = pd.concat(frames, ignore_index=True)
    mdf = sync_service.normalize_marketplace_cards(df)
    n_sk = sync_service.upsert_marketplace_cards(db, mdf, marketplace) if mdf is not None else 0
    n_pr, n_nm = sync_service.refresh_products_from_cards(db, marketplace)
    sync_service.record_api_pull(db, api_code, "cards_excel", int(total_rows), int(n_sk),
                                 f"файлы: {len(files)}")
    return {
        "imported": n_sk,
        "skus": n_sk,
        "products": n_pr,
        "nm_articles": n_nm,
        "total": int(total_rows),
        "files": fileinfo,
        "errors": errors,
    }


@router.get("/export/margin")
def export_margin(
    marketplace: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)

    mp_ids = common_service.resolve_marketplace_ids(db, marketplace)
    df = margin_service.margin_dataframe(
        db, date_from=from_, date_to=to_, marketplace=mp_ids, article_like=article_like
    )
    df, ru = excel_io.project_export(df, {
        "article": "Артикул", "name": "Наименование", "sells": "Продано, шт",
        "revenue": "Выручка, руб", "commission": "Комиссия, руб",
        "logistics": "Логистика, руб", "storage": "Хранение, руб",
        "services": "Услуги, руб", "income": "К перечислению, руб",
        "net_cost": "Себестоимость, руб", "other": "Прочее, руб",
        "margin_gross": "Маржа, до себестоимости, руб",
        "margin": "Маржа, руб",
        "margin_per_one": "Маржа на ед., руб", "margin_pct": "Маржа, %",
    }, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Маржа")
    fname = f"margin_{from_}_{to_}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export/margin/detail")
def export_margin_detail(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    missing_only: int = 0,
    compare: int = 0,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)

    df = margin_service.margin_detail_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like,
        default_net_cost=settings.default_net_cost,
    )
    if compare and date_from and date_to:
        try:
            f = date.fromisoformat(date_from)
            t = date.fromisoformat(date_to)
            delta = (t - f).days
            prev_df = margin_service.margin_detail_dataframe(
                db, date_from=f - timedelta(days=delta), date_to=f - timedelta(days=1),
                article_like=article_like, default_net_cost=settings.default_net_cost,
            )
            df = margin_service.compare_margin_periods(df, prev_df)
        except (ValueError, TypeError):
            pass
    if missing_only:
        df = df[df["net_cost_est"] == True].drop(columns=["net_cost_est"])
    df, ru = excel_io.project_export(df, {
        "article": "Артикул", "name": "Наименование", "sells": "Продано, шт",
        "returns_qty": "Возвращено, шт",
        "revenue": "Выручка, руб", "commission": "Комиссия, руб",
        "logistics": "Логистика, руб", "storage": "Хранение, руб",
        "services": "Услуги, руб", "income": "К перечислению, руб",
        "net_cost": "Себестоимость, руб", "margin_gross": "Маржа, до себестоимости, руб",
        "margin": "Прибыль, руб",
        "margin_per_one": "Прибыль на ед., руб", "margin_pct": "Прибыль, %",
        "net_cost_est": "Себестоимость оценка",
        "sells_pp": "Пред. период: Продано, шт", "margin_pp": "Пред. период: Прибыль, руб",
        "delta_ru": "Δ прибыли, руб", "delta_pct": "Δ прибыли, %",
        "logistics_out": "Логистика туда, руб",
        "logistics_in": "Логистика обратно, руб",
        "commission_per_one": "Комиссия на ед., руб",
        "logistics_per_one": "Логистика на ед., руб",
        "logistics_out_per_one": "Логистика туда на ед., руб",
        "logistics_in_per_one": "Логистика обратно на ед., руб",
        "storage_per_one": "Хранение на ед., руб",
        "income_per_one": "К перечисл. на ед., руб",
        "revenue_per_one": "Средняя цена, руб",
        "margin_gross_per_one": "Маржа до себест. на ед., руб",
        "return_rate": "Доля возвратов, %",
    }, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Маржа")
    fname = (("detail_missing_cost" if missing_only else "margin_detail")) + f"_{from_}_{to_}.xlsx"
    if compare:
        fname = fname.replace(".xlsx", "_compare.xlsx")
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export/margin/funnel")
def export_margin_funnel(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)

    df = margin_service.funnel_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like
    )
    df, ru = excel_io.project_export(df, {
        "article": "Артикул", "name": "Наименование", "views": "Просмотры",
        "opens": "Открытия карточки", "adds": "В корзину", "orders": "Заказы",
        "cancelled": "Отмены", "avg_price": "Ср. цена, руб",
        "revenue": "Выручка (оценка), руб",
        "cart_pct": "В корзину, %", "order_pct": "Заказы, %",
        "net_cost": "Себестоимость, руб", "margin": "Маржа (оценка), руб",
        "margin_pct": "Маржа, %",
    }, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Воронка")
    fname = f"margin_funnel_{from_}_{to_}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export/sales")
def export_sales(
    marketplace: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    payload = api_sales(marketplace, date_from, date_to, db)
    df = pd.DataFrame(payload["rows"])
    if article_like:
        needle = article_like.lower()
        keep = df["article"].str.lower().str.contains(needle, regex=False)
        df = df[keep].reset_index(drop=True)
    df, ru = excel_io.project_export(df, {
        "date": "Дата", "marketplace": "Маркетплейс", "article": "Артикул",
        "name": "Наименование", "quantity": "Продано, шт",
        "revenue": "Выручка, руб", "income": "К перечислению, руб",
    }, cols)
    df = df.rename(columns=ru)
    fname = f"sales_{payload['date_from']}_{payload['date_to']}.xlsx"
    return _xlsx_response(df, fname, len(df))


@router.post("/sync/{code}")
def api_sync(code: str, date_from: Optional[str] = None, date_to: Optional[str] = None):
    if code not in ("wb", "ozon"):
        return {"error": "unknown marketplace"}, 400
    from_, to_ = _parse_window400(date_from, date_to)
    return sync_service.sync_sales_window(code, from_, to_)


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


@router.post("/wb/cards")
def wb_cards(write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_wb_cards(db, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    if not int(excel):
        return _pull_json(res)
    return _xlsx_response(refresh_service.expand_wb_card_export(res["df"]), "wb_cards.xlsx", res["count"])


@router.post("/wb/stock")
def wb_stock(write_db: int = 1, by_size: int = 1, excel: int = 1,
             db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_wb_stock(db, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        wb_error(e)
    if not int(excel):
        return _pull_json(res)
    df = res["df"]
    if not df.empty and not int(by_size):
        df = df.groupby(["article", "warehouse"], as_index=False).agg({
            "quantity": "sum", "quantity_full": "sum", "in_way": "sum", "date": "first",
        })
    return _xlsx_response(df, "wb_stock.xlsx", len(df))


@router.post("/wb/funnel")
def wb_funnel(date_from: Optional[str] = None, date_to: Optional[str] = None,
              write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    from_, to_ = _parse_window400(date_from, date_to)
    try:
        res = refresh_service.pull_wb_funnel(db, from_, to_, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    if not int(excel):
        return _pull_json(res)
    return _xlsx_response(res["df"], f"wb_funnel_{from_}_{to_}.xlsx", res["count"])


@router.post("/wb/prices")
def wb_prices(write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_wb_prices(db, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    if not int(excel):
        return _pull_json(res)
    return _xlsx_response(res["df"], "wb_prices.xlsx", res["count"])


@router.post("/wb/storage")
def wb_storage(days: int = 7, write_db: int = 1, excel: int = 1,
               db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_wb_storage(db, days, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    if not int(excel):
        return _pull_json(res)
    return _xlsx_response(res["df"], "wb_storage.xlsx", res["count"])


@router.post("/wb/sales")
def wb_sales(date_from: Optional[str] = None, date_to: Optional[str] = None,
             write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    from_, to_ = _parse_window400(date_from, date_to)
    try:
        res = refresh_service.pull_wb_sales(db, from_, to_, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    if not int(excel):
        return _pull_json(res)
    df = res["df"]
    if "sa_name" in df.columns:
        export = df.rename(columns=V5_RU_COLUMNS)
    elif "supplierArticle" in df.columns:
        export = df.rename(columns=SALES_RU_COLUMNS)
    else:
        export = df
    return _xlsx_response(export, f"wb_sales_{from_}_{to_}.xlsx", res["count"])


@router.post("/wb/detail")
def wb_detail(date_from: Optional[str] = None, date_to: Optional[str] = None,
              write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    from_, to_ = _parse_window400(date_from, date_to)
    try:
        res = refresh_service.pull_wb_detail(db, from_, to_, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    if not int(excel):
        return _pull_json(res)
    df = res["df"]
    export = df.rename(columns=DETAIL_RU_COLUMNS) if not df.empty else df
    return _xlsx_response(export, f"wb_detail_{from_}_{to_}.xlsx", res["count"])


@router.post("/wb/detail-upload")
async def wb_detail_upload(
    files: List[UploadFile] = File(...),
    write_db: int = 1,
    db: Session = Depends(get_db),
):
    """Ручная загрузка детализации продаж WB из Excel/zip (без finance-api, без лимита).

    Принимает файлы WB с русскими заголовками («Детализация продаж»), см.
    DETAIL_UPLOAD_RENAME. Записывает в продажи source='detail'.
    """
    frames = []
    total_rows = 0
    errors = []
    fileinfo = []
    accum = 0
    for upf in files:
        data = await upf.read()
        accum += len(data)
        if accum > _MAX_CARDS_UPLOAD:
            raise HTTPException(status_code=413, detail="Слишком большой объём файлов (> 100 МБ)")
        name = upf.filename or "file"
        if name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(BytesIO(data)) as zf:
                    sub = 0
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        if not info.filename.lower().endswith((".xlsx", ".xlsm")):
                            continue
                        try:
                            d = excel_io.read_excel_bytes(zf.read(info))
                            frames.append(d)
                            sub += len(d)
                        except Exception as e:  # noqa: BLE001
                            errors.append(f"{name}/{info.filename}: {e}")
                    fileinfo.append({"name": name, "rows": sub, "ok": True})
                    total_rows += sub
            except zipfile.BadZipFile as e:
                errors.append(f"{name}: не является zip-архивом ({e})")
                fileinfo.append({"name": name, "rows": 0, "ok": False, "error": str(e)})
            continue
        if not name.lower().endswith((".xlsx", ".xlsm")):
            errors.append(f"{name}: пропущен (не Excel)")
            fileinfo.append({"name": name, "rows": 0, "ok": False, "error": "не Excel"})
            continue
        try:
            d = excel_io.read_excel_bytes(data)
            frames.append(d)
            total_rows += len(d)
            fileinfo.append({"name": name, "rows": len(d), "ok": True})
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            fileinfo.append({"name": name, "rows": 0, "ok": False, "error": str(e)})
    if not frames:
        raise HTTPException(status_code=400,
                            detail="Нет данных для импорта: " + ("; ".join(errors) or "нет файлов"))
    df = pd.concat(frames, ignore_index=True)
    rename = {k: v for k, v in DETAIL_UPLOAD_RENAME.items() if k in df.columns}
    df = df.rename(columns=rename)
    ndf = sync_service.normalize_wb_detail(df, source="excel")
    if ndf is None or ndf.empty:
        raise HTTPException(
            status_code=400,
            detail="Не удалось распознать структуру отчёта: нужны колонки "
                   "«Артикул поставщика/продавца» и «Дата продажи». " + "; ".join(errors[:5]),
        )
    n = 0
    if write_db:
        try:
            sync_service.upsert_wb_detail_rows(db, ndf, source="excel")
            n = sync_service.rebuild_sales_from_detail(db)
        except Exception as e:  # noqa: BLE001
            db.rollback()
            raise HTTPException(status_code=500,
                                detail=f"Ошибка записи детализации: {e}") from e
    sync_service.record_api_pull(db, "wb", "detail", int(total_rows), int(n), "ручной импорт")
    return {
        "imported": int(n),
        "rows": int(total_rows),
        "files": fileinfo,
        "errors": errors,
    }


@router.get("/wb/detail-rows")
def api_wb_detail_rows(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Сырые строки «Детализации продаж» WB (wb_detail_rows) с фильтрами и пагинацией."""
    from_, to_ = _parse_window400(date_from, date_to)
    q = select(models.WbDetailRow).where(models.WbDetailRow.sale_dt.isnot(None))
    if from_:
        q = q.where(models.WbDetailRow.sale_dt >= from_)
    if to_:
        q = q.where(models.WbDetailRow.sale_dt <= to_)
    if article_like:
        q = q.where(models.WbDetailRow.article.ilike(f"%{article_like}%"))
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = db.execute(
        q.order_by(models.WbDetailRow.sale_dt.desc(), models.WbDetailRow.id.desc())
        .offset(offset).limit(limit)
    ).scalars().all()
    out = [{
        "date": r.sale_dt.isoformat() if r.sale_dt else "",
        "article": r.article,
        "title": r.title,
        "doc_type": r.doc_type_name,
        "quantity": r.quantity,
        "retail_price": float(r.retail_price or 0),
        "retail_amount": float(r.retail_amount or 0),
        "commission": float(r.ppvz_sales_commission or 0),
        "for_pay": float(r.for_pay or 0),
        "logistics": float(r.delivery_service or 0),
        "storage": float(r.paid_storage or 0),
        "services": float((r.penalty or 0) + (r.deduction or 0) + (r.additional_payment or 0)),
        "office": r.office_name,
        "srid": r.srid,
        "source": r.source,
    } for r in rows]
    return {"rows": out, "total": int(total)}


@router.get("/wb/detail-summary")
def api_wb_detail_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Свод «Детализации продаж» WB по артикулам (сырые деньги операций, без маржи)."""
    from_, to_ = _parse_window400(date_from, date_to)
    df = sync_service.detail_summary_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like
    )
    return {
        "rows": df.replace({None: ""}).to_dict("records"),
        "count": len(df),
        "totals": _df_totals(df),
    }


@router.get("/export/wb/detail-summary")
def export_wb_detail_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)
    df = sync_service.detail_summary_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like
    )
    df, ru = excel_io.project_export(df, {
        "article": "Артикул", "title": "Наименование", "sells": "Продано, шт",
        "returns_qty": "Возвращено, шт", "revenue": "Реализовано, руб",
        "commission": "Комиссия, руб", "for_pay": "К перечислению, руб",
        "logistics": "Доставка, руб", "delivery_count": "Доставок, шт",
        "return_delivery_count": "Возврат доставок, шт",
        "storage": "Хранение, руб",
        "pvz_compensation": "ПВЗ-компенсации, руб",
        "payment_services": "Платёжные услуги, руб",
        "services": "Услуги/штрафы, руб", "ops_count": "Операций",
        "sources": "Источник",
    }, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Детализация по артикулам")
    fname = f"wb_detail_summary_{from_}_{to_}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export/wb/detail-rows")
def export_wb_detail_rows(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    limit: int = 5000,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Экспорт сырых строк «Детализации продаж» WB (wb_detail_rows) в Excel."""
    from_, to_ = _parse_window400(date_from, date_to)
    q = (
        select(models.WbDetailRow)
        .where(models.WbDetailRow.sale_dt.isnot(None))
        .order_by(models.WbDetailRow.sale_dt, models.WbDetailRow.id)
        .limit(limit)
    )
    if from_:
        q = q.where(models.WbDetailRow.sale_dt >= from_)
    if to_:
        q = q.where(models.WbDetailRow.sale_dt <= to_)
    if article_like:
        q = q.where(models.WbDetailRow.article.ilike(f"%{article_like}%"))
    rows = db.execute(q).scalars().all()
    recs = [{
        "date": r.sale_dt.isoformat() if r.sale_dt else "",
        "article": r.article, "title": r.title, "doc_type": r.doc_type_name,
        "quantity": r.quantity, "retail_price": float(r.retail_price or 0),
        "retail_amount": float(r.retail_amount or 0),
        "commission": float(r.ppvz_sales_commission or 0),
        "for_pay": float(r.for_pay or 0),
        "logistics": float(r.delivery_service or 0),
        "storage": float(r.paid_storage or 0),
        "services": float((r.penalty or 0) + (r.deduction or 0) + (r.additional_payment or 0)),
        "office": r.office_name, "srid": r.srid, "source": r.source,
    } for r in rows]
    df = pd.DataFrame(recs, columns=["date", "article", "title", "doc_type", "quantity",
                                      "retail_price", "retail_amount", "commission",
                                      "for_pay", "logistics", "storage", "services",
                                      "office", "srid", "source"])
    df, ru = excel_io.project_export(df, {
        "date": "Дата", "article": "Артикул", "title": "Наименование",
        "doc_type": "Тип документа", "quantity": "Кол-во",
        "retail_price": "Цена розничная", "retail_amount": "Реализовано, руб",
        "commission": "Комиссия, руб", "for_pay": "К перечислению, руб",
        "logistics": "Доставка, руб", "storage": "Хранение, руб",
        "services": "Услуги/штрафы, руб", "office": "Склад",
        "srid": "SRID", "source": "Источник",
    }, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Строки детализации")
    fname = f"wb_detail_rows_{from_}_{to_}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )

# ------------------------------------------------------- экспорт разделов WB API (вьюхи)
@router.get("/export/wb/cards")
def export_wb_cards(
    marketplace: str = "wb",
    like: Optional[str] = None,
    limit: int = 5000,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Экспорт карточек WB из БД (marketplace_cards) в Excel."""
    payload = api_cards(marketplace=marketplace, like=like, limit=limit, offset=0, db=db)
    df = pd.DataFrame(payload["rows"], columns=[
        "chrt_id", "nm_id", "vendor_code", "brand", "subject", "size", "barcode",
        "volume_l", "composition", "name",
    ])
    df, ru = excel_io.project_export(df, {
        "chrt_id": "Код размера", "nm_id": "Артикул WB", "vendor_code": "Артикул продавца",
        "brand": "Бренд", "subject": "Предмет", "size": "Размер", "barcode": "Баркод",
        "volume_l": "Объём, л", "composition": "Состав", "name": "Наименование",
    }, cols)
    df = df.rename(columns=ru)
    return _xlsx_response(df, "wb_cards.xlsx", payload["total"])


@router.get("/export/wb/stock")
def export_wb_stock(
    marketplace: str = "wb",
    by_size: int = 1,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Экспорт остатков WB (последний срез stocks) в Excel.

    by_size=1 — по размерам (как таблица по умолчанию); иначе агрегат по артикулу+склад.
    """
    payload = api_stocks(marketplace=marketplace, db=db)
    recs = payload["rows"]
    if not by_size:
        agg = {}
        for r in recs:
            key = (r["article"], r["warehouse"])
            a = agg.setdefault(key, {
                "date": r["date"], "marketplace": r["marketplace"], "article": r["article"],
                "name": r["name"], "warehouse": r["warehouse"],
                "quantity": 0, "quantity_full": 0, "in_way": 0,
            })
            a["quantity"] += r["quantity"]
            a["quantity_full"] += r["quantity_full"]
            a["in_way"] += r["in_way"]
        recs = [
            {"date": a["date"], "marketplace": a["marketplace"], "article": a["article"],
             "name": a["name"], "warehouse": a["warehouse"], "quantity": a["quantity"],
             "quantity_full": a["quantity_full"], "in_way": a["in_way"]}
            for a in agg.values()
        ]
        df_cols = ["date", "marketplace", "article", "name", "warehouse", "quantity",
                   "quantity_full", "in_way"]
        ru = {
            "date": "Дата", "marketplace": "Маркетплейс", "article": "Артикул",
            "name": "Наименование", "warehouse": "Склад", "quantity": "Доступно",
            "quantity_full": "Всего на складах", "in_way": "В пути",
        }
    else:
        df_cols = ["date", "marketplace", "article", "name", "chrt_id", "size", "barcode",
                   "warehouse", "quantity", "quantity_full", "in_way"]
        ru = {
            "date": "Дата", "marketplace": "Маркетплейс", "article": "Артикул",
            "name": "Наименование", "chrt_id": "Код размера", "size": "Размер",
            "barcode": "Баркод", "warehouse": "Склад", "quantity": "Доступно",
            "quantity_full": "Всего на складах", "in_way": "В пути",
        }
    df = pd.DataFrame(recs, columns=df_cols)
    df, ru = excel_io.project_export(df, ru, cols)
    df = df.rename(columns=ru)
    fname = "wb_stock.xlsx" if by_size else "wb_stock_agg.xlsx"
    return _xlsx_response(df, fname, len(recs))


@router.get("/export/wb/prices")
def export_wb_prices(article_like: Optional[str] = None, cols: Optional[str] = None,
                     db: Session = Depends(get_db)):
    """Экспорт текущих цен/скидок WB (price_snapshots) в Excel."""
    payload = api_prices(article_like=article_like, db=db)
    df = pd.DataFrame(payload["rows"], columns=[
        "article", "nm_id", "size", "name", "price", "discounted_price", "discount",
    ])
    df, ru = excel_io.project_export(df, {
        "article": "Артикул", "nm_id": "Артикул WB", "size": "Размер",
        "name": "Наименование", "price": "Цена без скидки",
        "discounted_price": "Цена со скидкой", "discount": "Скидка, %",
    }, cols)
    df = df.rename(columns=ru)
    return _xlsx_response(df, "wb_prices.xlsx", payload["count"])


@router.get("/export/wb/storage")
def export_wb_storage(article_like: Optional[str] = None, cols: Optional[str] = None,
                      db: Session = Depends(get_db)):
    """Экспорт стоимости хранения WB (storage_costs) в Excel."""
    payload = api_storage_cost(article_like=article_like, db=db)
    df = pd.DataFrame(payload["rows"], columns=[
        "nm_id", "article", "name", "barcodes_count", "volume", "storage_price",
        "warehouse_price",
    ])
    df, ru = excel_io.project_export(df, {
        "nm_id": "Артикул WB", "article": "Артикул", "name": "Наименование",
        "barcodes_count": "Баркодов", "volume": "Объём, л",
        "storage_price": "Хранение за баркод", "warehouse_price": "Сумма хранения",
    }, cols)
    df = df.rename(columns=ru)
    return _xlsx_response(df, "wb_storage.xlsx", payload["count"])


@router.get("/export/wb/funnel")
def export_wb_funnel(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Экспорт воронки продаж WB (funnel_metric) в Excel."""
    payload = api_funnel(date_from=date_from, date_to=date_to, article_like=article_like, db=db)
    df = pd.DataFrame(payload["rows"], columns=[
        "date_from", "date_to", "nm_id", "article", "name", "views", "opens", "adds",
        "orders", "cancelled", "buyouts", "avg_price", "revenue", "buyout_sum",
    ])
    df, ru = excel_io.project_export(df, {
        "date_from": "С", "date_to": "По", "nm_id": "Артикул WB", "article": "Артикул",
        "name": "Наименование", "views": "Просмотры", "opens": "Открытия",
        "adds": "В корзину", "orders": "Заказы", "cancelled": "Отмены",
        "buyouts": "Выкупы", "avg_price": "Ср. цена", "revenue": "Выручка",
        "buyout_sum": "Сумма выкупа",
    }, cols)
    df = df.rename(columns=ru)
    return _xlsx_response(df, f"wb_funnel_{payload['date_from']}_{payload['date_to']}.xlsx",
                          payload["count"])


@router.post("/ozon/cards")
def ozon_cards(write_db: int = 1, db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_oz_cards(db, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    return _xlsx_response(res["df"], "ozon_cards.xlsx", res["count"])


@router.post("/ozon/stock")
def ozon_stock(write_db: int = 1, db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_oz_stock(db, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    return _xlsx_response(res["df"], "ozon_stock.xlsx", res["count"])


@router.post("/ozon/prices")
def ozon_prices(write_db: int = 1, db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_oz_prices(db, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    return _xlsx_response(res["df"], "ozon_prices.xlsx", res["count"])


@router.post("/ozon/realization")
def ozon_realization(month: Optional[int] = None, year: Optional[int] = None,
                     write_db: int = 1, db: Session = Depends(get_db)):
    if month is None or year is None:
        today = date.today().replace(day=1) - timedelta(days=1)
        year, month = today.year, today.month
    try:
        res = refresh_service.pull_oz_realization(db, month, year, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    df = res["df"]
    export = df.rename(columns=OZON_RU_COLUMNS) if not df.empty else df
    return _xlsx_response(export, f"ozon_realization_{year:04d}-{month:02d}.xlsx", res["count"])


@router.post("/ozon/cashflow")
def ozon_cashflow(date_from: Optional[str] = None, date_to: Optional[str] = None,
                  write_db: int = 1, db: Session = Depends(get_db)):
    from_, to_ = _parse_window400(date_from, date_to)
    try:
        res = refresh_service.pull_oz_cashflow(db, from_, to_, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    return _xlsx_response(res["df"], f"ozon_cashflow_{from_}_{to_}.xlsx", res["count"])


# ------------------------------------------------------- массовое обновление (кнопка)
@router.post("/refresh")
def api_refresh(api: str, detail: int = 0, date_from: Optional[str] = None,
                date_to: Optional[str] = None):
    """Запускает фоновое обновление маркетплейса (wb|ozon). Ошибки изолированы по видам."""
    if api not in ("wb", "ozon"):
        raise HTTPException(status_code=400, detail='Параметр api должен быть "wb" или "ozon"')
    try:
        return refresh_service.start_refresh(api, include_detail=bool(detail),
                                             date_from=date_from, date_to=date_to)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Некорректная дата: {e}")


@router.get("/refresh")
def api_refresh_list():
    return {"jobs": refresh_service.active_jobs()}


@router.get("/refresh/history")
def api_refresh_history(limit: int = 10, db: Session = Depends(get_db)):
    return {"runs": refresh_service.history(db, limit=min(max(limit, 1), 50))}


@router.get("/refresh/{job_id}")
def api_refresh_state(job_id: int):
    state = refresh_service.job_state(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Задание не найдено")
    return state


# ------------------------------------------------------- Яндекс.Диск (файлы отчётов)
@router.post("/yandex/upload")
async def yandex_upload(file: UploadFile = File(...), folder: str = "/agent_market"):
    try:
        token = yandex_service.require_token()
        data = await file.read()
        name = file.filename or "file.xlsx"
        return yandex_service.upload_bytes(token, folder, name, data)
    except yandex_service.YandexDiskError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/yandex/list")
def yandex_list(folder: str = "/agent_market"):
    try:
        token = yandex_service.require_token()
        return yandex_service.list_files(token, folder)
    except yandex_service.YandexDiskError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/yandex/download")
def yandex_download(path: str):
    try:
        token = yandex_service.require_token()
        data = yandex_service.download_bytes(token, path)
    except yandex_service.YandexDiskError as e:
        raise HTTPException(status_code=400, detail=str(e))
    name = path.rsplit("/", 1)[-1] or "file.xlsx"
    return StreamingResponse(
        BytesIO(data),
        media_type=XLSX_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.delete("/yandex/delete")
def yandex_delete(path: str):
    try:
        token = yandex_service.require_token()
        yandex_service.delete_file(token, path)
    except yandex_service.YandexDiskError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "path": path}


# ------------------------------------------------------------------ тикеты

def _ticket_or_error(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except tickets_service.TicketError as e:
        raise HTTPException(status_code=e.status, detail=str(e))


@router.get("/tickets")
def api_tickets():
    """Очередь тикетов из TICKETS.md."""
    return tickets_service.load()


@router.post("/tickets")
def api_tickets_create(body: dict = Body(...)):
    """Создаёт тикет в «Открытые»."""
    title = str(body.get("title", "")).strip()
    if not title:
        raise HTTPException(status_code=400, detail="Заголовок тикета обязателен")
    priority = str(body.get("priority", "medium")).strip().lower()
    return _ticket_or_error(
        tickets_service.create, title=title,
        body=str(body.get("body", "")), priority=priority,
    )


@router.post("/tickets/{tid}/start")
def api_ticket_start(tid: str):
    return _ticket_or_error(tickets_service.start, tid)


@router.post("/tickets/{tid}/block")
def api_ticket_block(tid: str):
    return _ticket_or_error(tickets_service.block, tid)


@router.post("/tickets/{tid}/unblock")
def api_ticket_unblock(tid: str):
    return _ticket_or_error(tickets_service.unblock, tid)


@router.post("/tickets/{tid}/close")
def api_ticket_close(tid: str, body: dict = Body(default={})):
    return _ticket_or_error(
        tickets_service.close, tid, commit=str(body.get("commit", "")),
    )


@router.post("/tickets/{tid}/decline")
def api_ticket_decline(tid: str):
    return _ticket_or_error(tickets_service.decline, tid)


@router.post("/tickets/{tid}/reopen")
def api_ticket_reopen(tid: str):
    return _ticket_or_error(tickets_service.reopen, tid)