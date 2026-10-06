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

router = APIRouter(prefix="/api")


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
            q = q.where(common_service.like_col(models.WbDetailRow.article, article_like))
        detail_articles = int(db.execute(q).scalar_one() or 0)
    estimated = int(df["net_cost_est"].sum()) if not df.empty and "net_cost_est" in df else 0
    totals = _margin_detail_totals(df)
    return {
        "rows": df.replace({None: ""}).to_dict("records"),
        "count": len(df),
        "detail_articles": detail_articles,
        "estimated": estimated,
        "default_net_cost": settings.default_net_cost,
        "prev_window": prev_window,
        "totals": totals,
    }


@router.get("/margin/ozon-detail")
def api_margin_ozon_detail(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    compare: int = 0,
    by_size: int = 0,
    db: Session = Depends(get_db),
):
    """Прибыльность по Детализации Продаж Ozon напрямую из ozon_detail_rows.

    Аналог margin/detail для WB. income в Ozon уже чистый к перечислению
    (комиссия и услуги вычтены), поэтому Прибыль = income − себестоимость×продано;
    commission/services показываются справочными колонками (в минусе).

    by_size=0 (по умолчанию) — строка это товар: артикулы размеров свёрнуты в
    базовый артикул, себестоимость берётся у базового артикула, размеры и
    артикулы показаны количеством. by_size=1 — строка это артикул размера.

    compare=1 добавляет показатели предыдущего аналогичного периода
    (та же длительность окна, сдвинутая назад): sells_pp, margin_pp,
    delta_ru (руб), delta_pct (%). Требует даты date_from/date_to.
    """
    from_, to_ = _parse_window400(date_from, date_to)

    df = margin_service.ozon_margin_detail_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like,
        default_net_cost=settings.default_net_cost, by_size=bool(by_size),
    )
    prev_window = None
    if compare and date_from and date_to:
        try:
            f = date.fromisoformat(date_from)
            t = date.fromisoformat(date_to)
            delta = (t - f).days
            prev_from = f - timedelta(days=delta + 1)
            prev_to = f - timedelta(days=1)
            prev_df = margin_service.ozon_margin_detail_dataframe(
                db, date_from=prev_from, date_to=prev_to, article_like=article_like,
                default_net_cost=settings.default_net_cost, by_size=bool(by_size),
            )
            df = margin_service.compare_margin_periods(df, prev_df)
            prev_window = {"date_from": prev_from.isoformat(), "date_to": prev_to.isoformat()}
        except (ValueError, TypeError):
            prev_window = None
    detail_articles = 0
    if from_ and to_:
        # сколько товаров в окне: в свёрнутом режиме считаем базовые артикулы
        art_col = (models.OzonDetailRow.offer_id if by_size
                   else func.coalesce(
                       func.nullif(models.OzonDetailRow.base_article, ""),
                       models.OzonDetailRow.offer_id))
        q = select(func.count(func.distinct(art_col))).where(
            models.OzonDetailRow.offer_id != "",
            models.OzonDetailRow.date.isnot(None),
            models.OzonDetailRow.date >= from_,
            models.OzonDetailRow.date <= to_,
        )
        if article_like:
            q = q.where(common_service.like_col(models.OzonDetailRow.offer_id, article_like)
                        | common_service.like_col(models.OzonDetailRow.base_article, article_like))
        detail_articles = int(db.execute(q).scalar_one() or 0)
    estimated = int(df["net_cost_est"].sum()) if not df.empty and "net_cost_est" in df else 0
    totals = _margin_detail_totals(df)
    received, periods = _cashflow_received(db, from_, to_)
    # Точные начисления (аккруалы) за окно — «сколько реально перечислит Ozon»:
    # из ozon_accruals (продажа минус комиссия, логистика, услуги, прочее).
    # Сверка: сумма по артикулам + нераспределённые = итог начислений за окно.
    accrued_total = None
    if not df.empty and "accrued_net" in df:
        accrued_total = round(float(df["accrued_net"].sum() or 0), 2)
    accrued_rows = 0
    accrued_other = 0.0
    accrued_unmapped = 0.0
    if from_ and to_:
        acc_art = (models.OzonAccrual.offer_id if by_size
                   else func.coalesce(
                       func.nullif(models.OzonAccrual.base_article, ""),
                       models.OzonAccrual.offer_id))
        q = select(func.count(func.distinct(acc_art))).where(
            models.OzonAccrual.offer_id != "",
            models.OzonAccrual.date.isnot(None),
            models.OzonAccrual.date >= from_,
            models.OzonAccrual.date <= to_,
        )
        accrued_rows = int(db.execute(q).scalar_one() or 0)
        aq = select(func.coalesce(func.sum(models.OzonAccrual.amount), 0.0)).where(
            models.OzonAccrual.date.isnot(None),
            models.OzonAccrual.date >= from_,
            models.OzonAccrual.date <= to_,
        )
        # «прочее» (NON_ITEM) — расходы без товара; «нераспределённые» — строки
        # со SKU, у которого не нашлось артикула (нет продаж в детализации).
        accrued_other = round(float(db.execute(
            aq.where(models.OzonAccrual.bucket == "other")).scalar_one() or 0), 2)
        accrued_unmapped = round(float(db.execute(
            aq.where(models.OzonAccrual.offer_id == "",
                     models.OzonAccrual.sku != "")).scalar_one() or 0), 2)
    # Оценка «на р/с за товар»: доля фактических выплат (движение средств за окно)
    # от начислений «к перечислению», распределённая пропорционально income.
    # Сумма cashflow_est по артикулам сходится с фактически полученным за окно.
    cashflow_ratio = None
    if not df.empty and "income" in df and received is not None:
        sum_income = float(df["income"].sum() or 0)
        if sum_income > 0:
            cashflow_ratio = round(received / sum_income * 100, 1)
            df["cashflow_est"] = (df["income"] * received / sum_income).round(2)
    totals = _margin_detail_totals(df)
    return {
        "rows": df.replace({None: ""}).to_dict("records"),
        "count": len(df),
        "detail_articles": detail_articles,
        "estimated": estimated,
        "default_net_cost": settings.default_net_cost,
        "prev_window": prev_window,
        "totals": totals,
        "cashflow_received": received,
        "cashflow_periods": periods,
        "cashflow_ratio": cashflow_ratio,
        "accrued_total": accrued_total,
        "accrued_rows": accrued_rows,
        "accrued_other": accrued_other,
        "accrued_unmapped": accrued_unmapped,
        "window": {"date_from": date_from or "", "date_to": date_to or ""},
        "detail_range": ozon_detail_range(db),
    }


@router.get("/margin/funnel")
def api_margin_funnel(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Прибыльность по Воронке Продаж WB (данные funnel_metric, оценка).

    Срез выбирается через pick_funnel_window, как в /api/funnel: точное окно →
    самый широкий внутри запрошенного → самый свежий пересекающийся → последний
    в базе. date_from/date_to — запрошенный период, snapshot_from/snapshot_to —
    фактически показанный срез, matched — совпали ли они.
    """
    from_, to_ = _parse_window400(date_from, date_to)

    df = margin_service.funnel_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like
    )
    return {
        "rows": df.replace({None: ""}).to_dict("records"),
        "count": len(df),
        "date_from": str(from_),
        "date_to": str(to_),
        "snapshot_from": df.attrs.get("date_from", ""),
        "snapshot_to": df.attrs.get("date_to", ""),
        "matched": bool(df.attrs.get("matched", False)),
    }


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
    marketplace: Optional[str] = None,
    compare: int = 0,
    top: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """Обзор: KPI из детализаций + топы + дельты цены + склад + свежесть.

    marketplace: 'wb' | 'ozon' | 'wb,ozon' | 'all' (пусто = все).
    compare=1 добавляет сравнение маржи с предыдущим аналогичным окном.
    top — необязательный размер топа прибыли/убытка и дельт цены (пусто = весь
    список; фронтенд сам делает сортировку/пагинацию/итоги).
    Остаётся совместимым со старым ответом: per_marketplace/total/daily
    (агрегат по таблице Продажи, source != detail).
    """
    from_, to_ = _parse_window400(date_from, date_to)
    mp_param = marketplace or "all"

    prev = None
    if compare and from_ and to_:
        delta = (to_ - from_).days
        prev = (from_ - timedelta(days=delta), from_ - timedelta(days=1))

    query = (
        select(
            models.Marketplace.code.label("marketplace"),
            models.Sale.date,
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
        .group_by(models.Marketplace.code, models.Sale.date)
    )
    per_mp = {}
    total = {"sells": 0, "revenue": 0.0, "income": 0.0}
    daily = {}
    wanted = dashboard_service._wanted(mp_param)
    for r in db.execute(query):
        if r.marketplace not in wanted:
            continue
        per_mp.setdefault(r.marketplace, {
            "marketplace": r.marketplace,
            "sells": 0, "revenue": 0.0, "income": 0.0,
            "commission": 0.0, "logistics": 0.0, "storage": 0.0,
        })
        p = per_mp[r.marketplace]
        p["sells"] += int(r.sells or 0)
        p["revenue"] += float(r.revenue or 0)
        p["income"] += float(r.income or 0)
        p["commission"] += float(r.commission or 0)
        p["logistics"] += float(r.logistics or 0)
        p["storage"] += float(r.storage or 0)
        dk = str(r.date)
        daily.setdefault(dk, {"date": dk, "revenue": 0.0, "income": 0.0, "sells": 0, "profit": 0.0})
        daily[dk]["revenue"] += float(r.revenue or 0)
        daily[dk]["income"] += float(r.income or 0)
        daily[dk]["sells"] += int(r.sells or 0)
        # Операционная прибыль = доход минус комиссия/логистика/хранение.
        # Знак затрат в данных бывает разным (WB пишет «+», Ozon «−») — нормируем abs().
        daily[dk]["profit"] += (float(r.income or 0)
                                - abs(float(r.commission or 0))
                                - abs(float(r.logistics or 0))
                                - abs(float(r.storage or 0)))
    for d in daily.values():
        d["profit"] = round(d["profit"], 2)
    if per_mp:
        total["sells"] = sum(p["sells"] for p in per_mp.values())
        total["revenue"] = round(sum(p["revenue"] for p in per_mp.values()), 2)
        total["income"] = round(sum(p["income"] for p in per_mp.values()), 2)

    return {
        "per_marketplace": list(per_mp.values()),
        "total": total,
        "daily": sorted(daily.values(), key=lambda d: d["date"]),
        "kpis": dashboard_service.dashboard_kpis(db, from_, to_, prev, mp_param),
        "tops": {
            "profit": dashboard_service.top_products(
                db, from_, to_, mp_param, limit=top, kind="profit"),
            "loss": dashboard_service.top_products(
                db, from_, to_, mp_param, limit=top, kind="loss"),
        },
        "price": dashboard_service.price_delta(db, from_, to_, prev, mp_param, limit=top),
        "prefixes": dashboard_service.prefix_margin(
            db, from_, to_, mp_param, limit=top),
        "stocks": dashboard_service.stocks_summary(db, to_, mp_param),
        "freshness": dashboard_service.freshness(db),
        "date_from": str(from_),
        "date_to": str(to_),
    }


@router.get("/export/dashboard")
def export_dashboard(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    marketplace: Optional[str] = None,
    compare: int = 0,
    db: Session = Depends(get_db),
):
    """Дашборд одним Excel-файлом: KPI, топы, изменения цен, группы."""
    from_, to_ = _parse_window400(date_from, date_to)
    mp_param = marketplace or "all"

    prev = None
    if compare and from_ and to_:
        delta = (to_ - from_).days
        prev = (from_ - timedelta(days=delta), from_ - timedelta(days=1))

    kpis = dashboard_service.dashboard_kpis(db, from_, to_, prev, mp_param)
    profit = dashboard_service.top_products(db, from_, to_, mp_param, kind="profit")
    loss = dashboard_service.top_products(db, from_, to_, mp_param, kind="loss")
    price = dashboard_service.price_delta(db, from_, to_, prev, mp_param)
    prefixes = dashboard_service.prefix_margin(db, from_, to_, mp_param)

    def _kpi_frame(row):
        return {
            "Показатель": row["marketplace"] or "Итого",
            "Выручка, руб": row["revenue"],
            "Доход, руб": row["income"],
            "Прибыль, руб": row["margin"],
            "Прибыль/шт, руб": row["margin_per_one"],
            "Рентабельность, %": row["margin_pct"],
            "Продано, шт": row["sells"],
            "Товаров": row["articles"],
        }

    kpi_rows = [_kpi_frame(kpis["total"])] + [_kpi_frame(m) for m in kpis["per_mp"]]
    if kpis["compare"]:
        kpi_rows.append({
            "Показатель": "Δ к прошлому периоду, руб",
            "Прибыль, руб": kpis["compare"]["delta_ru"],
            "Рентабельность, %": kpis["compare"]["delta_pct"],
        })

    tops_cols = ["article", "name", "marketplace", "sells", "returns_qty",
                 "revenue", "income", "margin", "margin_per_one", "margin_pct"]
    tops_ru = {
        "article": "Артикул", "name": "Наименование", "marketplace": "МП",
        "sells": "Продано, шт", "returns_qty": "Возвращено, шт",
        "revenue": "Выручка, руб", "income": "Доход, руб", "margin": "Прибыль, руб",
        "margin_per_one": "Прибыль/шт, руб", "margin_pct": "Рентабельность, %",
    }
    price_cols = ["article", "name", "avg", "avg_prev", "delta_ru", "delta_pct", "margin"]
    price_ru = {
        "article": "Артикул", "name": "Наименование", "avg": "Цена сейчас, руб",
        "avg_prev": "Цена прошлого пер., руб", "delta_ru": "Δ, руб",
        "delta_pct": "Δ, %", "margin": "Прибыль, руб",
    }
    prefix_cols = ["prefix", "articles", "sells", "revenue", "income", "margin",
                   "margin_per_one", "margin_pct"]
    prefix_ru = {
        "prefix": "Группа", "articles": "Товаров", "sells": "Продано, шт",
        "revenue": "Выручка, руб", "income": "Доход, руб", "margin": "Прибыль, руб",
        "margin_per_one": "Прибыль/шт, руб", "margin_pct": "Рентабельность, %",
    }

    def frame(rows, cols, ru):
        df = pd.DataFrame(rows, columns=cols)
        if df.empty:
            df = pd.DataFrame(columns=cols)
        return df.rename(columns=ru)

    sheets = {
        "KPI": pd.DataFrame(kpi_rows),
        "Прибыльные": frame(profit["rows"], tops_cols, tops_ru),
        "Убыточные": frame(loss["rows"], tops_cols, tops_ru),
        "Рост цены": frame(price["up"]["rows"], price_cols, price_ru),
        "Снижение цены": frame(price["down"]["rows"], price_cols, price_ru),
        "Группы": frame(prefixes["rows"], prefix_cols, prefix_ru),
    }
    buf = excel_io.dfs_to_excel_stream(sheets)
    fname = f"dashboard_{from_}_{to_}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


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


@router.post("/products/refresh")
def products_refresh(overwrite: int = 0, db: Session = Depends(get_db)):
    """Обновляет общий каталог из карточек WB+Ozon (pull_catalog)."""
    try:
        res = refresh_service.pull_catalog(db, overwrite=bool(overwrite))
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    return {"ok": True, **res}


@router.post("/products/preview")
def products_preview(payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """Пересчёт рекомендуемой (mode=recommended) или минимальной (mode=min) цены
    с пользовательскими коэффициентами (без записи)."""
    settings = base_price_service.merge_price_settings(payload.get("price_settings"))
    mode = str(payload.get("mode") or "recommended").strip().lower()
    like = str(payload.get("like") or "").strip().lower()
    ue_map, ue_global = _unit_economics_map(db)
    prods = list(db.execute(
        select(models.Product).order_by(models.Product.article)).scalars())
    prefix_map = _prefix_avg_cost_map(prods)

    def ue_for(article: str):
        return ue_map.get((article or "").strip().upper()) or ue_global

    rows = []
    for p in prods:
        if like and not (common_service.like_match(p.article, like)
                         or common_service.like_match(p.name, like)):
            continue
        row = {"article": p.article, "name": p.name,
               "net_cost": float(p.net_cost or 0), "volume_l": float(p.volume_l or 0)}
        fields = _catalog_price_fields(p, settings, _eff_net_cost(p.article, p.net_cost, prefix_map), ue_for(p.article))
        price = float(fields["min_price"] if mode == "min" else fields["recommended_price"])
        row.update(fields)
        row["price"] = price
        row["mode"] = mode
        rows.append(row)
    return {"rows": rows, "count": len(rows), "price_settings": settings}


@router.post("/products/prices/apply")
def products_prices_apply(payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """Применяет рекомендуемые (mode=recommended) или минимальные (mode=min) цены
    **только к видимым в таблице товарам** и пушит на Wildberries через WB API
    (v2/upload/task). Для минимальных цен учитывается порог WB (public API
    priceLimits.minPrice): итог = max(расчёт, мин. цена WB). Артикулы без nm_id
    (не заведены на WB) пропускаются молча [pushed]."""
    settings = base_price_service.merge_price_settings(payload.get("price_settings"))
    mode = str(payload.get("mode") or "recommended").strip().lower()
    like = str(payload.get("like") or "").strip().lower()
    sizes = int(payload.get("sizes") or 0)
    stocks = int(payload.get("stocks") or 0)

    prods = list(db.execute(
        select(models.Product).order_by(models.Product.article)
    ).scalars())
    prefix_map = _prefix_avg_cost_map(prods)
    ue_map, ue_global = _unit_economics_map(db)

    def ue_for(article: str):
        return ue_map.get((article or "").strip().upper()) or ue_global

    rows = []
    for p in prods:
        if like and not (common_service.like_match(p.article, like)
                         or common_service.like_match(p.name, like)):
            continue
        cost = _eff_net_cost(p.article, p.net_cost, prefix_map)
        if mode == "min":
            price = float(base_price_service.minimum_price(cost, ue_for(p.article), settings))
        else:
            price = float(base_price_service.recommended_price(cost, p.volume_l, settings))
        rows.append({
            "article": p.article,
            "name": p.name,
            "net_cost": float(p.net_cost or 0),
            "volume_l": float(p.volume_l or 0),
            "price": price,
            "mode": mode,
        })

    if not rows:
        return {"ok": True, "pushed": 0, "skipped": 0, "note": "Нет видимых товаров."}

    nm_map = {a.article: a.nm_id for a in db.execute(select(models.NmArticle)).scalars()}
    items = []
    skipped = []
    for r in rows:
        nm = nm_map.get(r["article"])
        if not nm:
            skipped.append(r["article"])
            continue
        items.append({
            "nmID": int(nm),
            "price": float(r["price"] or 0),
        })
    if not items:
        return {"ok": True, "pushed": 0, "skipped": len(skipped),
                "note": "Ни один из видимых товаров не заведён в WB (нет nm_id)."}
    prov = provider_factory.get_wb_provider()
    if mode == "min":
        wb_min_prices = {}
        try:
            wb_min_prices = prov.get_min_prices([it["nmID"] for it in items]) or {}
        except Exception:  # noqa: BLE001 — тихо, клампинг по WB пропускается
            wb_min_prices = {}
        for it in items:
            wb_min = float(wb_min_prices.get(str(it["nmID"]), 0) or 0)
            it["price"] = max(float(it["price"] or 0), wb_min)
    for it in items:
        it["discount"] = 0
    try:
        res = prov.update_prices(items)
    except WbApiError as e:
        return {"ok": False, "pushed": 0, "skipped": len(skipped), "error": str(e)}

    notes = [f"Отправлено товаров: {len(items)}"]
    if mode == "min":
        notes.append(" по минимальной цене (break-even)")
    else:
        notes.append(" по рекомендуемой цене")
    if sizes or stocks:
        notes.append(" с учётом фильтров «с размерами/остатки»")
    if skipped:
        notes.append(f"; пропущено без nm_id: {len(skipped)}")
    return {
        "ok": True,
        "pushed": len(items),
        "skipped": len(skipped),
        "task_id": res.get("task_id") if isinstance(res, dict) else None,
        "note": " ".join(notes),
    }


@router.get("/products/price-settings")
def products_price_settings():
    return {
        "defaults": base_price_service.PRICE_DEFAULTS,
        "labels": base_price_service.PRICE_LABELS,
        "hints": base_price_service.PRICE_HINTS,
    }


@router.get("/export/products")
def export_products(
    like: Optional[str] = None,
    sizes: int = 0,
    stocks: int = 0,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Экспорт каталога «Наш склад → Товары» в Excel по видимым колонкам."""
    payload = api_products(like=like, sizes=sizes, stocks=stocks, db=db)
    df = pd.DataFrame(payload["rows"])
    if "tags" in df.columns:
        df["tags"] = df["tags"].map(lambda v: ", ".join(v) if isinstance(v, list) else v)
    order = [c for c in ["article", "name", "brand", "subject", "size", "sizes_count",
                         "barcode", "volume_l", "composition", "net_cost", "replenishable",
                         "tags", "own_stock", "mp_stock", "recommended_price", "markup"]
             if c in df.columns]
    if order:
        df = df[order]
    df, ru = excel_io.project_export(df, {
        "article": "Артикул", "name": "Наименование", "brand": "Бренд",
        "subject": "Предмет", "size": "Размер", "sizes_count": "Размеров",
        "barcode": "Баркод", "volume_l": "Объём, л", "composition": "Состав",
        "net_cost": "Себестоимость", "replenishable": "Докупаемый",
        "tags": "Маркетплейсы", "own_stock": "Свой склад", "mp_stock": "Остаток МП",
        "recommended_price": "Рекоменд. цена", "markup": "Наценка",
    }, cols)
    df = df.rename(columns=ru)
    return _xlsx_response(df, "products.xlsx", payload["count"])


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
    prices_df, updated_at, source = pricing_service.resolve_prices(db)
    rec = pricing_service.recommendations(
        db, settings=payload, prices_df=prices_df,
    )
    rec["prices_source"] = source
    rec["prices_updated_at"] = updated_at.isoformat() if updated_at else None
    return rec


@router.post("/pricing/apply")
def pricing_apply(payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """Применяет скидки через WB API upload/task и пишет журнал price_changes.

    Если в теле передан list `ui_rows` (видимые строки таблицы автопилота) —
    применяет ровно их (то, что видит пользователь с учётом фильтров поиска и
    скрытых колонок). Иначе пересчитывает рекомендации и применяет всё.
    """
    prov = provider_factory.get_wb_provider()
    ui_rows = payload.get("ui_rows")
    try:
        if ui_rows:
            return pricing_service.apply_rows(
                db, ui_rows, settings=payload, provider=prov,
            )
        prices_df, updated_at, source = pricing_service.resolve_prices(db)
        res = pricing_service.apply_recommendations(
            db, settings=payload, prices_df=prices_df, provider=prov,
        )
        res["prices_source"] = source
        res["prices_updated_at"] = updated_at.isoformat() if updated_at else None
        return res
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except WbApiError as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))
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
    prices_df, _updated_at, _source = pricing_service.resolve_prices(db)
    rec = pricing_service.recommendations(
        db, settings=payload, prices_df=prices_df,
    )
    df = pd.DataFrame(rec["rows"])
    if not df.empty:
        df["action"] = df["action"].map(PRICING_ACTION_RU)
        df["replenishable"] = df["replenishable"].map({True: "да", False: "нет"})
    keep = [
        "article", "name", "price", "current_vis", "current_discount", "target_vis",
        "target_discount", "delta_discount", "net_cost", "action", "status", "reason",
        "doc", "velocity", "trend",
        "conv_pct", "backlog", "stock", "avg_price", "eff", "floor_price",
        "max_discount_item", "margin_pct_at_target", "replenishable",
        "product_rating", "buyouts", "conv_buyout_percent", "cancel_sum",
        "add_to_wishlist", "stock_wb", "return_rate", "margin_pct", "margin_per_one",
        "revenue_per_one", "income_per_one", "commission_per_one",
        "logistics_per_one", "storage_per_one", "detail_sells", "detail_returns_qty",
        "promo_count", "promo_names", "promo_part_pct", "promo_tier_pct",
        "promo_tier_boost", "promo_need_rows", "promo_cap_pct",
        "promo_push_applied", "promo_delta_discount", "promo_score",
        "promo_score_confidence", "last_sale_days_ago",
    ]
    df = df[[c for c in keep if c in df.columns]]
    df, ru = excel_io.project_export(df, {
        "article": "Артикул", "name": "Наименование", "price": "Цена базовая, руб",
        "current_vis": "Цена сейчас, руб", "current_discount": "Скидка сейчас, %",
        "target_vis": "Целевая цена, руб", "target_discount": "Целевая скидка, %",
        "delta_discount": "Дельта скидки, п.п.", "net_cost": "Себестоимость, руб",
        "action": "Решение", "status": "Статус", "reason": "Причина",
        "doc": "DOC, дн", "velocity": "Продажи, шт/дн", "trend": "Тренд",
        "conv_pct": "Конверсия, %", "backlog": "В корзине", "stock": "Остаток",
        "avg_price": "Ср. цена факт, руб", "eff": "База расчёта, руб",
        "floor_price": "Пол (break-even), руб", "max_discount_item": "Макс. скидка, %",
        "margin_pct_at_target": "Маржа при цели, %", "replenishable": "Докупаемый",
        "product_rating": "Рейтинг товара", "buyouts": "Выкупы, шт",
        "conv_buyout_percent": "Конверсия выкупа, %", "cancel_sum": "Отмены, руб",
        "add_to_wishlist": "В избранное, шт", "stock_wb": "Остаток WB, шт",
        "return_rate": "Возвраты, % от продаж",
        "margin_pct": "Маржа факт, % от выручки",
        "margin_per_one": "Маржа/шт факт, руб",
        "revenue_per_one": "Ср. чек факт, руб",
        "income_per_one": "К перечислению/шт, руб",
        "commission_per_one": "Комиссия/шт, руб",
        "logistics_per_one": "Логистика/шт, руб",
        "storage_per_one": "Хранение/шт, руб",
        "detail_sells": "Продано в детализации, шт",
        "detail_returns_qty": "Возвращено в детализации, шт",
        "promo_count": "Акций WB (кол-во)",
        "promo_names": "Акции WB",
        "promo_part_pct": "Участие в акциях, % (агрегат WB)",
        "promo_tier_pct": "Доля участия след. буста, %",
        "promo_tier_boost": "Буст след. ступени, ×",
        "promo_need_rows": "Надо в акцию для буста, шт",
        "promo_cap_pct": "Потолок промо-скидки, %",
        "promo_push_applied": "Разгружен акцией",
        "promo_delta_discount": "Вклад разгрузки в скидку, п.п.",
        "promo_score": "Оценка жертвенности",
        "promo_score_confidence": "Полнота оценки",
        "last_sale_days_ago": "Дней без продаж",
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


@router.get("/replenish")
def api_replenish(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    target_days: int = 30,
    window_days: int = 30,
    marketplace: Optional[str] = None,
    sort: str = "urgency",
    article_like: Optional[str] = None,
    show_inactive: int = 0,
    view: str = "article",
    db: Session = Depends(get_db),
):
    """Потребность в товаре: спрос (детализации) + остатки (наш склад и МП).

    Спрос = продажи − возвраты за окно (шт/день). target_days — целевой запас
    в днях продаж; window_days — число дней по умолчанию, когда не указаны
    даты окна. sort: urgency | margin | name. view: article | sizes —
    размерный разрез (WB) по той же логике.
    """
    from_, to_ = _parse_window400(date_from, date_to)
    if date_from is None and date_to is None:
        # окно задано не фильтрами, а window_days: двигаем назад от даты to_.
        back = max(1, int(window_days or settings.sync_days_default))
        from_ = to_ - timedelta(days=back - 1)
    span = None
    try:
        span = (to_ - from_).days + 1
    except (TypeError, ValueError):
        span = max(1, int(window_days or settings.sync_days_default))

    result = replenish_service.replenish_rows(
        db, from_, to_, target_days=target_days, span_days=span,
        marketplace=marketplace, sort=sort, article_like=article_like,
        show_inactive=bool(show_inactive), view=view,
    )
    result["date_from"] = from_.isoformat()
    result["date_to"] = to_.isoformat()
    return result


_REPLENISH_EXPORT = {
    "article": "Артикул", "name": "Наименование", "barcode": "Баркод",
    "actual_mp": "Карточки", "status_label": "Статус",
    "demand": "Спрос, шт/день", "demand_wb": "Спрос WB, шт/день",
    "demand_oz": "Спрос Ozon, шт/день",
    "sells": "Продано, шт", "returns_qty": "Возвраты, шт", "return_rate": "Возвраты, %",
    "wb_sells": "Продано WB, шт",
    "our_stock": "У нас, шт", "our_cost": "Себестоимость, руб",
    "wb_qty": "WB склад, шт", "wb_avail": "WB доступно, шт", "wb_in_way": "WB в пути, шт",
    "wb_doc": "WB, дней запаса", "wb_def": "WB дефицит, шт",
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
    "wb_doc": "WB, дней запаса", "wb_def": "WB дефицит, шт",
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


@router.get("/export/replenish")
def export_replenish(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    target_days: int = 30,
    window_days: int = 30,
    marketplace: Optional[str] = None,
    sort: str = "urgency",
    article_like: Optional[str] = None,
    show_inactive: int = 0,
    view: str = "article",
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)
    if date_from is None and date_to is None:
        back = max(1, int(window_days or settings.sync_days_default))
        from_ = to_ - timedelta(days=back - 1)
    span = (to_ - from_).days + 1
    result = replenish_service.replenish_rows(
        db, from_, to_, target_days=target_days, span_days=span,
        marketplace=marketplace, sort=sort, article_like=article_like,
        show_inactive=bool(show_inactive), view=view,
    )
    df = pd.DataFrame(result["rows"])
    if df.empty:
        df = pd.DataFrame(columns=list(_REPLENISH_EXPORT))
    key_map = _REPLENISH_EXPORT if view != "sizes" else _REPLENISH_EXPORT_SIZES
    df, ru = excel_io.project_export(df, key_map, ",".join(_replenish_cols(cols, key_map)))
    df = df.rename(columns=ru)
    sheet = "Потребность по размерам" if view == "sizes" else "Потребность"
    buf = excel_io.df_to_excel_stream(df, sheet_name=sheet)
    tpl = "replenish_{0}_{1}_{2}.xlsx"
    fname = tpl.format(view, from_, to_)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.post("/replenish/import-excel")
async def replenish_import_excel(file: UploadFile = File(...)):
    """Разбор Excel-файла с правками для PDF (дропзона в меню PDF).

    Файл — обычная выгрузка «Потребность» (кнопка «Excel»), отредактированная
    в Excel. Возвращает ``{rows, meta}``: строки с ключами выгрузки для POST
    ``/export/replenish/pdf`` и сводку предпросмотра — сколько карточек
    распознано, какие колонки неизвестны, сколько строк без артикула
    отброшено.
    """
    name = (file.filename or "").strip().lower()
    if not name.endswith(".xlsx"):
        raise HTTPException(400, "Нужен файл .xlsx (выгрузка «Потребность»)")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Файл пустой")
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "Файл больше 20 МБ — похоже, это не выгрузка")
    try:
        rows, meta = excel_import.parse_replenish_excel(data, _REPLENISH_EXPORT)
    except excel_import.ExcelImportError as e:
        raise HTTPException(400, str(e))
    if not rows:
        raise HTTPException(400, "В файле нет строк с артикулом")
    meta["file"] = file.filename or ""
    return {"rows": rows, "meta": meta}


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


@router.get("/export/replenish/pdf")
def export_replenish_pdf(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    target_days: int = 30,
    window_days: int = 30,
    marketplace: Optional[str] = None,
    sort: str = "urgency",
    article_like: Optional[str] = None,
    show_inactive: int = 0,
    cols: Optional[str] = None,
    with_photos: int = 1,
    photo_count: int = 6,
    vel_days: int = replenish_service.WB_SORT_VELOCITY_DAYS,
    profit: int = 1,
    limit: int = PDF_DEFAULT_LIMIT,
    db: Session = Depends(get_db),
):
    """Карточки потребности в PDF из строк таблицы (базовый режим).

    ``cols`` — те же ключи, что и в Excel-выгрузке. Фото подтягиваются из
    индекса фотографий (рекурсивный обход диска, кэш в памяти), миниатюры
    кэшируются на диске, поэтому повторные выгрузки быстрые.

    ``vel_days`` — окно скорости продаж размера: 180, 365 или 0 (всё время).

    ``profit`` — учитывать прибыльность: покрытие = период/4 дней продаж, а
    целевой уровень домножается на коэффициент по рентабельности (0…2).
    При ``profit=0`` работает как раньше: покрытие = «Запас», без коэффициента.

    PDF из Excel-файла с правками — отдельный ``POST /export/replenish/pdf``.
    """
    from_, to_ = _parse_window400(date_from, date_to)
    if date_from is None and date_to is None:
        back = max(1, int(window_days or settings.sync_days_default))
        from_ = to_ - timedelta(days=back - 1)
    span = (to_ - from_).days + 1

    common = dict(
        target_days=target_days, span_days=span, marketplace=marketplace,
        sort=sort, article_like=article_like, show_inactive=bool(show_inactive),
    )
    main = replenish_service.replenish_rows(db, from_, to_, view="article", **common)

    rows = main["rows"]
    cap = max(1, min(int(limit or PDF_DEFAULT_LIMIT), PDF_MAX_LIMIT))
    truncated = max(0, len(rows) - cap)
    rows = rows[:cap]
    return _replenish_pdf_build(
        db, rows,
        from_=from_, to_=to_, span=span, truncated=truncated,
        target_days=target_days, vel_days=vel_days, profit=profit,
        cols=cols, with_photos=with_photos, photo_count=photo_count,
        article_like=article_like,
    )


@router.post("/export/replenish/pdf")
def export_replenish_pdf_from_excel(
    payload: dict = Body(...),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    target_days: int = 30,
    window_days: int = 30,
    cols: Optional[str] = None,
    with_photos: int = 1,
    photo_count: int = 6,
    vel_days: int = replenish_service.WB_SORT_VELOCITY_DAYS,
    profit: int = 1,
    limit: int = PDF_DEFAULT_LIMIT,
    db: Session = Depends(get_db),
):
    """PDF из строк Excel-файла (дропзона в меню PDF).

    ``payload`` = ``{"rows": [...], "source": "имя.xlsx"}`` — строки из
    ``POST /replenish/import-excel``, ключи как в выгрузке Excel.

    ``wb_def`` («WB дефицит, шт») каждой строки задаёт бюджет «Итого
    дослать» по артикулу (см. ``replenish.apply_sort_budget``): пустая
    ячейка — бюджет не задан, размеры считаются по складу как обычно.
    Отбраковка убыточных/полных карточек не применяется: файл есть истина,
    ненужное владелец удалил сам. Фильтры таблицы (marketplace, поиск,
    сортировка) игнорируются — состав задаёт файл; окно/скорость/прибыльность
    и фото действуют как в базовом режиме.
    """
    from_, to_ = _parse_window400(date_from, date_to)
    if date_from is None and date_to is None:
        back = max(1, int(window_days or settings.sync_days_default))
        from_ = to_ - timedelta(days=back - 1)
    span = (to_ - from_).days + 1

    raw = payload.get("rows")
    if not isinstance(raw, list) or not raw:
        raise HTTPException(400, "В файле нет строк для карточек")
    rows: list = []
    budgets: dict = {}
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise HTTPException(
                400, f"Строка {i + 1}: ожидался объект, получен {type(item).__name__}"
            )
        r = dict(item)
        art = str(r.get("article") or "").strip()
        if not art:
            raise HTTPException(400, f"Строка {i + 1}: нет артикула")
        r["article"] = art
        rows.append(r)
        v = r.get("wb_def")
        if v is None or v == "":
            continue  # пустая ячейка = бюджет не задан, план как обычно
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f != f:  # NaN
            continue
        budgets[art.upper()] = max(0, int(round(f)))

    cap = max(1, min(int(limit or PDF_DEFAULT_LIMIT), PDF_MAX_LIMIT))
    truncated = max(0, len(rows) - cap)
    rows = rows[:cap]

    source = str(payload.get("source") or "").strip()[:60]
    return _replenish_pdf_build(
        db, rows,
        from_=from_, to_=to_, span=span, truncated=truncated,
        target_days=target_days, vel_days=vel_days, profit=profit,
        cols=cols, with_photos=with_photos, photo_count=photo_count,
        budgets=budgets,
        subtitle_extra=(" · из Excel: " + source) if source else " · из Excel",
    )

def _replenish_pdf_build(
    db: Session,
    rows: list,
    *,
    from_: date,
    to_: date,
    span: int,
    truncated: int,
    target_days: int,
    vel_days: int,
    profit,
    cols: Optional[str],
    with_photos: int,
    photo_count: int,
    article_like: Optional[str] = None,
    budgets: Optional[dict] = None,
    subtitle_extra: str = "",
) -> Response:
    """Общая сборка PDF-потребности: план подсортировки, фото, карточки.

    ``rows`` — строки с ключами выгрузки, уже обрезанные по ``limit``.
    ``budgets`` не None = режим файла (PDF из Excel): «Итого дослать»
    берёт бюджет из колонки «WB дефицит» по артикулу, а отбраковка
    убыточных/полных карточек не применяется — файл есть истина
    (см. ``replenish.apply_sort_budget``).
    ``subtitle_extra`` — приписка в шапке (имя Excel-файла).
    """
    vel_days = int(vel_days or 0)
    if vel_days not in replenish_service.WB_SORT_VELOCITY_WINDOWS:
        vel_days = replenish_service.WB_SORT_VELOCITY_DAYS
    vel_label = "всё время" if vel_days == 0 else f"{vel_days} дн"

    # План подсортировки: полный список размеров берём из карточки WB, а
    # скорость — за длинное окно. По окну спроса (30 дн) размеры без продаж
    # получают цель 0 и выпадают, хотя остатков на них может не быть вовсе.
    use_profit = bool(int(profit or 0))
    coverage = None
    factors = None
    if use_profit:
        # покрытие = четверть выбранного периода: недельный цикл пополнения
        coverage = max(0.25, span / 4.0)
        factors = replenish_service.wb_profit_factors(
            db, to_, velocity_days=vel_days,
            articles=[r.get("article") for r in rows],
            article_like=article_like,
        )
    plan = replenish_service.wb_sorting_plan(
        db, to_, target_days=target_days, velocity_days=vel_days,
        articles=[r.get("article") for r in rows],
        coverage_days=coverage, profit_factor=factors,
    )

    if budgets is not None:
        # Режим Excel: «Итого дослать» и распределение по размерам идут по
        # колонке «WB дефицит» из файла (пустые ячейки не задают бюджет).
        replenish_service.apply_sort_budget(plan, budgets)
    elif use_profit:
        # убыточные и уже полные товары в шопинг-листе не нужны: иначе PDF
        # наполовину состоит из нулей. Считаем отбракованные, чтобы X-Truncated
        # остался честным. В режиме Excel не применяется: файл есть истина.
        alive = [r for r in rows if plan.get(
            str(r.get("article") or "").strip().upper(), {"total": 0}
        )["total"] > 0]
        truncated += len(rows) - len(alive)
        rows = alive
        plan = {k: v for k, v in plan.items() if v["total"] > 0}

    # Дальше идёт только CPU-работа (обход фото, ReportLab) — она занимает
    # секунды-двадцать. Соединение из пула (5 + overflow 10) держим ровно
    # столько, сколько нужно БД: иначе несколько открытых PDF занимают весь
    # пул, и обычные запросы вроде «Применить» встают в 30-секундную очередь
    # pool_timeout. Дальше db не используется, close() в get_db идемпотентен.
    db.close()

    # Размеры по артикулу: в карточке это блок «размер / наличие / дослать».
    sizes_by_article: dict[str, list[dict]] = {
        art: v["sizes"] for art, v in plan.items() if v["sizes"]
    }

    want_photos = bool(with_photos) and photo_count > 0
    n_photos = max(0, min(int(photo_count or 0), PDF_MAX_PHOTOS)) if want_photos else 0

    index = photos_service.get_index()
    if want_photos and rows and not index.root.is_dir():
        raise HTTPException(
            400,
            f"Папка с фотографиями не найдена: {index.root}. "
            "Укажите путь в настройке PHOTOS_ROOT.",
        )
    jobs: list = []
    if want_photos and rows:
        tasks = []
        for i, r in enumerate(rows):
            paths = index.find(r.get("article") or "", n_photos)
            for p in paths:
                tasks.append((i, p))
        results: dict[tuple[int, str], bytes] = {}
        if tasks:
            with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 4))) as pool:
                futures = {
                    pool.submit(thumbs_service.thumb_bytes, p): (i, p) for i, p in tasks
                }
                for fut in as_completed(futures):
                    idx, path = futures[fut]
                    try:
                        data = fut.result()
                    except Exception:
                        data = None
                    if data:
                        results[(idx, str(path))] = data
        buckets: dict[int, list[bytes]] = {i: [] for i in range(len(rows))}
        for (i, path), data in sorted(results.items(), key=lambda kv: kv[0]):
            buckets[i].append(data)
        for i, r in enumerate(rows):
            jobs.append((r, sizes_by_article.get(str(r.get("article") or "").strip().upper(), []),
                         buckets.get(i, [])))
    else:
        jobs = [
            (r, sizes_by_article.get(str(r.get("article") or "").strip().upper(), []), [])
            for r in rows
        ]

    # Только колонки из карты экспорта — иначе в подписи останется сырой ключ.
    wanted = _replenish_cols(cols, _REPLENISH_EXPORT)
    if not wanted:
        wanted = ["name", "need_buy", "demand", "status_label"]

    pdf = pdf_demand.build_demand_pdf(
        jobs,
        cols=wanted,
        labels=_REPLENISH_EXPORT,
        with_photos=want_photos,
        photo_count=n_photos or PDF_MAX_PHOTOS,
        subtitle=(
            f"{from_:%d.%m.%Y} — {to_:%d.%m.%Y}"
            f" · {'покрытие ' + _fmt_days(coverage) + ' дн (период/4)' if use_profit else 'запас ' + str(int(target_days or 0)) + ' дн'}"
            f" · скорость по {vel_label}"
            + (f" · прибыльность 0…{replenish_service.PROFIT_FACTOR_MAX:g}×"
               if use_profit else "")
            + (f" · пустой размер ≥ {replenish_service.WB_SORT_MIN_SIZE_STOCK} шт"
               if use_profit else "")
            + " · WB"
            + subtitle_extra
        ),
    )
    hits = sum(1 for _, _, t in jobs if t)
    fname = f"potrebnost_{from_:%Y-%m-%d}_{to_:%Y-%m-%d}.pdf"
    return Response(
        content=pdf,
        media_type=PDF_MEDIA,
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "X-Count": str(len(jobs)),
            "X-Photo-Hits": str(hits),
            "X-Truncated": str(truncated),
        },
    )


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
        "article": "Артикул", "nm_id": "Артикул WB", "name": "Наименование", "sells": "Продано, шт",
        "returns_qty": "Возвращено, шт",
        "stock_qty": "Остаток, шт", "stock_total": "Остаток всего, шт",
        "stock_in_way": "В пути, шт",
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


@router.get("/export/margin/ozon-detail")
def export_margin_ozon_detail(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    missing_only: int = 0,
    compare: int = 0,
    by_size: int = 0,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)

    df = margin_service.ozon_margin_detail_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like,
        default_net_cost=settings.default_net_cost, by_size=bool(by_size),
    )
    if compare and date_from and date_to:
        try:
            f = date.fromisoformat(date_from)
            t = date.fromisoformat(date_to)
            delta = (t - f).days
            prev_df = margin_service.ozon_margin_detail_dataframe(
                db, date_from=f - timedelta(days=delta), date_to=f - timedelta(days=1),
                article_like=article_like, default_net_cost=settings.default_net_cost,
                by_size=bool(by_size),
            )
            df = margin_service.compare_margin_periods(df, prev_df)
        except (ValueError, TypeError):
            pass
    if missing_only:
        df = df[df["net_cost_est"] == True].drop(columns=["net_cost_est"])
    df, ru = excel_io.project_export(df, {
        "article": "Артикул", "nm_id": "Артикул WB", "name": "Наименование",
        "size": "Размер", "sizes_count": "Размеров", "offers_count": "Артикулов",
        "sells": "Продано, шт", "returns_qty": "Возвращено, шт",
        "postings": "Постинги",
        "revenue": "Выручка, руб", "commission": "Комиссия, руб",
        "services": "Услуги, руб", "income": "К перечислению, руб",
        "cashflow_est": "На р/с (оценка), руб",
        "storage": "Хранение, руб",
        "net_cost": "Себестоимость, руб", "margin": "Прибыль, руб",
        "margin_gross": "Маржа, до себестоимости, руб",
        "net_cost_est": "Себестоимость оценка",
        "margin_per_one": "Прибыль на ед., руб", "margin_pct": "Прибыль, %",
        "net_cost_est": "Себестоимость оценка",
        "amount": "Сумма продажи, руб",
        "sells_pp": "Пред. период: Продано, шт", "margin_pp": "Пред. период: Прибыль, руб",
        "delta_ru": "Δ прибыли, руб", "delta_pct": "Δ прибыли, %",
        "commission_per_one": "Комиссия на ед., руб",
        "services_per_one": "Услуги на ед., руб",
        "storage_per_one": "Хранение на ед., руб",
        "income_per_one": "К перечисл. на ед., руб",
        "revenue_per_one": "Средняя цена, руб",
        "return_rate": "Доля возвратов, %",
        "accrued_sale": "Начислено: продажа, руб",
        "accrued_commission": "Начислено: комиссия, руб",
        "accrued_logistics": "Начислено: логистика, руб",
        "accrued_services": "Начислено: услуги, руб",
        "accrued_other": "Начислено: прочее, руб",
        "accrued_net": "На р/с (по начислениям), руб",
        "accrued_diff": "Δ нач. vs детал., руб",
        "accrued_coverage": "Есть начисления",
        "has_detail": "Есть детализация",
        "margin_accrued": "Прибыль (по начислениям), руб",
    }, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Маржа")
    fname = (("detail_missing_cost" if missing_only else "margin_ozon_detail")) + f"_{from_}_{to_}.xlsx"
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
        "cancelled": "Отмены", "buyouts": "Выкупы", "avg_price": "Ср. цена, руб",
        "revenue": "Выручка (оценка), руб",
        "cart_pct": "В корзину, %", "order_pct": "Заказы, %",
        "net_cost": "Себестоимость, руб", "margin": "Маржа (оценка), руб",
        "margin_pct": "Маржа, %", "storage_est": "Хранение (оц.), руб",
        "buyout_sum": "Выкуп, руб", "subject_name": "Предмет",
        "brand_name": "Бренд", "product_rating": "Рейтинг товара",
        "feedback_rating": "Рейтинг отзывов", "stock_wb": "Остаток WB, шт",
        "stock_mp": "Остаток МП, шт", "stock_balance_sum": "Остаток (баланс)",
        "cancel_sum": "Отмены, руб", "avg_orders_per_day": "Заказов в день",
        "share_order_percent": "Доля заказов, %", "add_to_wishlist": "В избранное",
        "time_to_ready_min": "До готовности, мин", "localization_percent": "Локализация, %",
        "conv_to_cart_percent": "В корзину (воронка), %",
        "conv_cart_to_order_percent": "Корзина→Заказ, %",
        "conv_buyout_percent": "Выкуп, %",
        "wb_club_order_count": "WB Клуб: заказы", "wb_club_order_sum": "WB Клуб: заказы, руб",
        "wb_club_buyout_count": "WB Клуб: выкупы", "wb_club_buyout_sum": "WB Клуб: выкупы, руб",
        "wb_club_cancel_count": "WB Клуб: отмены", "wb_club_cancel_sum": "WB Клуб: отмены, руб",
        "wb_club_avg_price": "WB Клуб: ср. цена", "wb_club_buyout_percent": "WB Клуб: выкуп, %",
        "wb_club_avg_orders_per_day": "WB Клуб: заказов в день",
        "title": "Название", "subject_id": "ID предмета", "tags": "Теги",
        "past_views": "Пред. период: просмотры", "past_adds": "Пред. период: в корзину",
        "past_orders": "Пред. период: заказы", "past_cancelled": "Пред. период: отмены",
        "past_buyouts": "Пред. период: выкупы", "past_revenue": "Пред. период: выручка",
        "past_buyout_sum": "Пред. период: выкуп, руб", "past_cancel_sum": "Пред. период: отмены, руб",
        "past_avg_price": "Пред. период: ср. цена",
        "dy_views": "Динамика просмотров, %", "dy_adds": "Динамика корзины, %",
        "dy_orders": "Динамика заказов, %", "dy_cancelled": "Динамика отмен, %",
        "dy_buyouts": "Динамика выкупов, %", "dy_revenue": "Динамика выручки, %",
        "dy_avg_price": "Динамика ср. цены, %",
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
        keep = df["article"].str.lower().str.contains(
            common_service.like_to_regex(article_like), regex=True, na=False
        )
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
        q = q.where(common_service.like_col(models.WbDetailRow.article, article_like))
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
        q = q.where(common_service.like_col(models.WbDetailRow.article, article_like))
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
def export_wb_prices(marketplace: str = "wb", article_like: Optional[str] = None,
                     cols: Optional[str] = None, db: Session = Depends(get_db)):
    """Экспорт текущих цен/скидок (price_snapshots) в Excel."""
    payload = api_prices(marketplace=marketplace, article_like=article_like, db=db)
    df = pd.DataFrame(payload["rows"], columns=[
        "article", "nm_id", "size", "name", "price", "discounted_price", "discount",
    ])
    df, ru = excel_io.project_export(df, {
        "article": "Артикул", "nm_id": "Артикул WB", "size": "Размер",
        "name": "Наименование", "price": "Цена без скидки",
        "discounted_price": "Цена со скидкой", "discount": "Скидка, %",
    }, cols)
    df = df.rename(columns=ru)
    fname = f"{marketplace}_prices.xlsx"
    return _xlsx_response(df, fname, payload["count"])


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
    df = pd.DataFrame(payload["rows"])
    df, ru = excel_io.project_export(df, {
        "date_from": "С", "date_to": "По", "nm_id": "Артикул WB", "article": "Артикул",
        "name": "Наименование", "subject_name": "Предмет", "brand_name": "Бренд",
        "product_rating": "Рейтинг карточки", "feedback_rating": "Рейтинг по отзывам",
        "views": "Просмотры", "opens": "Открытия", "adds": "В корзину",
        "orders": "Заказы", "cancelled": "Отмены", "cancel_sum": "Сумма отмен",
        "buyouts": "Выкупы", "avg_price": "Ср. цена", "revenue": "Выручка",
        "buyout_sum": "Сумма выкупа", "avg_orders_per_day": "Ср. заказов в день",
        "share_order_percent": "Доля в выручке, %", "add_to_wishlist": "В отложенные",
        "time_to_ready_min": "Время доставки, мин", "localization_percent": "Локальные заказы, %",
        "conv_to_cart_percent": "В корзину, %", "conv_cart_to_order_percent": "К заказу, %",
        "conv_buyout_percent": "К выкупу, %",
        "wb_club_order_count": "WB Клуб: заказы", "wb_club_order_sum": "WB Клуб: заказы, ₽",
        "wb_club_buyout_count": "WB Клуб: выкупы", "wb_club_buyout_sum": "WB Клуб: выкупы, ₽",
        "wb_club_cancel_count": "WB Клуб: отмены", "wb_club_cancel_sum": "WB Клуб: отмены, ₽",
        "wb_club_avg_price": "WB Клуб: ср. цена", "wb_club_buyout_percent": "WB Клуб: % выкупа",
        "wb_club_avg_orders_per_day": "WB Клуб: заказов/день",
        "stock_wb": "Остатки WB", "stock_mp": "Остатки свой склад",
        "stock_balance_sum": "Сумма остатков",
    }, cols)
    df = df.rename(columns=ru)
    return _xlsx_response(df, f"wb_funnel_{payload['date_from']}_{payload['date_to']}.xlsx",
                          payload["count"])


@router.post("/ozon/cards")
def ozon_cards(write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_oz_cards(db, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    if not int(excel):
        return _pull_json(res)
    return _xlsx_response(res["df"], "ozon_cards.xlsx", res["count"])


@router.post("/ozon/stock")
def ozon_stock(write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_oz_stock(db, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    if not int(excel):
        return _pull_json(res)
    return _xlsx_response(res["df"], "ozon_stock.xlsx", res["count"])


@router.post("/ozon/prices")
def ozon_prices(write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_oz_prices(db, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    if not int(excel):
        return _pull_json(res)
    return _xlsx_response(res["df"], "ozon_prices.xlsx", res["count"])


@router.post("/ozon/realization")
def ozon_realization(date_from: Optional[str] = None, date_to: Optional[str] = None,
                     month: Optional[int] = None, year: Optional[int] = None,
                     write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    """Реализация за месяцы, покрывающие окно из шапки (или точный месяц).

    Приоритет: явные month/year → месяцы интервала date_from..date_to →
    прошлый месяц по умолчанию.
    """
    if month is not None and year is not None:
        res = refresh_service.pull_oz_realization(db, month, year, write_db=bool(write_db))
        label = f"{year:04d}-{month:02d}"
    elif date_from and date_to:
        from_, to_ = _parse_window400(date_from, date_to)
        res = refresh_service.pull_oz_realizations(db, from_, to_, write_db=bool(write_db))
        label = f"{from_}_{to_}"
    else:
        today = date.today().replace(day=1) - timedelta(days=1)
        res = refresh_service.pull_oz_realization(db, today.month, today.year,
                                                  write_db=bool(write_db))
        label = f"{today.year:04d}-{today.month:02d}"
    if not int(excel):
        return _pull_json(res)
    df = res["df"]
    export = df.rename(columns=OZON_RU_COLUMNS) if not df.empty else df
    return _xlsx_response(export, f"ozon_realization_{label}.xlsx", res["count"])


@router.post("/ozon/cashflow")
def ozon_cashflow(date_from: Optional[str] = None, date_to: Optional[str] = None,
                  excel: int = 1, write_db: int = 1, db: Session = Depends(get_db)):
    from_, to_ = _parse_window400(date_from, date_to)
    try:
        res = refresh_service.pull_oz_cashflow(db, from_, to_, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    if not int(excel):
        return _pull_json(res)
    return _xlsx_response(res["df"], f"ozon_cashflow_{from_}_{to_}.xlsx", res["count"])


@router.get("/ozon/cashflow-rows")
def api_ozon_cashflow_rows(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Периоды «Движения средств» Ozon (ozon_cash_flows) за окно + итоги.

    payments_amount хранится отрицательным (деньги ушли на расчётный счёт),
    поэтому «фактически получено» = -(сумма payments_amount)."""
    from_, to_ = _parse_window400(date_from, date_to)
    q = select(models.OzonCashFlow).where(models.OzonCashFlow.period_begin.isnot(None))
    if from_:
        q = q.where(models.OzonCashFlow.period_end >= from_)
    if to_:
        q = q.where(models.OzonCashFlow.period_begin <= to_)
    rows = db.execute(
        q.order_by(models.OzonCashFlow.period_begin.asc())
    ).scalars().all()
    out = [{
        "period_begin": r.period_begin.isoformat() if r.period_begin else "",
        "period_end": r.period_end.isoformat() if r.period_end else "",
        "begin_balance": float(r.begin_balance or 0),
        "payments_amount": float(r.payments_amount or 0),
        "delivery_total": float(r.delivery_total or 0),
        "return_total": float(r.return_total or 0),
        "services_total": float(r.services_total or 0),
        "others_total": float(r.others_total or 0),
        "end_balance": float(r.end_balance or 0),
    } for r in rows]
    df = pd.DataFrame(out) if out else pd.DataFrame()
    totals = _df_totals(df, extra_skip=("begin_balance", "end_balance")) if len(df) else {}
    received = round(-(totals.get("payments_amount") or 0), 2) if totals else 0.0
    return {"rows": out, "count": len(out), "totals": totals,
            "received": received,
            "window": req_window(date_from, date_to),
            "detail_range": ozon_date_range(db, models.OzonCashFlow,
                                            models.OzonCashFlow.period_begin)}


@router.post("/ozon/accrual")
def ozon_accrual(date_from: Optional[str] = None, date_to: Optional[str] = None,
                 excel: int = 1, write_db: int = 1, db: Session = Depends(get_db)):
    """Начисления по товарам (аккруалы) Ozon за окно: /v1/finance/accrual/by-day.

    Побуквенно: продажа (sale), комиссия (commission), логистика (logistics) —
    из POSTING; услуги (services) — из ITEM; прочее (other) — NON_ITEM.
    """
    from_, to_ = _parse_window400(date_from, date_to)
    try:
        res = refresh_service.pull_oz_accrual(db, from_, to_, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    if not int(excel):
        return _pull_json(res)
    df = res["df"]
    export = df.rename(columns=OZON_ACCRUAL_RU_COLUMNS) if not df.empty else df
    return _xlsx_response(export, f"ozon_accrual_{from_}_{to_}.xlsx", res["count"])


@router.get("/ozon/accrual-rows")
def api_ozon_accrual_rows(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    bucket: Optional[str] = None,
    article_like: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Строки начислений Ozon (ozon_accruals) за окно + итоги по корзинам.

    amount отрицательный — расход (комиссия/логистика/услуги), положительный —
    продажа. bucket: sale | commission | logistics | services | other.
    """
    from_, to_ = _parse_window400(date_from, date_to)
    q = select(models.OzonAccrual)
    if from_:
        q = q.where(models.OzonAccrual.date >= from_)
    if to_:
        q = q.where(models.OzonAccrual.date <= to_)
    if bucket:
        q = q.where(models.OzonAccrual.bucket == bucket)
    if article_like:
        q = q.where(common_service.like_col(models.OzonAccrual.offer_id, article_like)
                    | common_service.like_col(models.OzonAccrual.base_article, article_like))
    rows = db.execute(
        q.order_by(models.OzonAccrual.date.asc())
    ).scalars().all()
    out = [{
        "date": r.date.isoformat() if r.date else "",
        "accrual_id": r.accrual_id or "",
        "bucket": r.bucket or "",
        "type_id": int(r.type_id or 0),
        "sku": r.sku or "",
        "offer_id": r.offer_id or "",
        "unit_number": r.unit_number or "",
        "quantity": int(r.quantity or 0),
        "amount": float(r.amount or 0),
        "seller_price": float(r.seller_price or 0),
        "sale_price": float(r.sale_price or 0),
    } for r in rows]
    df = pd.DataFrame(out) if out else pd.DataFrame()
    totals = _df_totals(df) if len(df) else {}
    return {"rows": out, "count": len(out), "totals": totals,
            "window": req_window(date_from, date_to),
            "detail_range": ozon_date_range(db, models.OzonAccrual)}


@router.get("/export/ozon/accrual-rows")
def export_ozon_accrual_rows(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    bucket: Optional[str] = None,
    article_like: Optional[str] = None,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)
    q = select(models.OzonAccrual)
    if from_:
        q = q.where(models.OzonAccrual.date >= from_)
    if to_:
        q = q.where(models.OzonAccrual.date <= to_)
    if bucket:
        q = q.where(models.OzonAccrual.bucket == bucket)
    if article_like:
        q = q.where(common_service.like_col(models.OzonAccrual.offer_id, article_like)
                    | common_service.like_col(models.OzonAccrual.base_article, article_like))
    rows = db.execute(
        q.order_by(models.OzonAccrual.date.asc())
    ).scalars().all()
    out = [{
        "date": r.date.isoformat() if r.date else "",
        "accrual_id": r.accrual_id or "",
        "bucket": r.bucket or "",
        "type_id": int(r.type_id or 0),
        "sku": r.sku or "",
        "offer_id": r.offer_id or "",
        "unit_number": r.unit_number or "",
        "quantity": int(r.quantity or 0),
        "amount": float(r.amount or 0),
        "seller_price": float(r.seller_price or 0),
        "sale_price": float(r.sale_price or 0),
    } for r in rows]
    df = pd.DataFrame(out) if out else pd.DataFrame()
    df, ru = excel_io.project_export(df, OZON_ACCRUAL_RU_COLUMNS, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Начисления")
    fname = f"ozon_accrual_{from_}_{to_}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export/ozon/cashflow-rows")
def export_ozon_cashflow_rows(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)
    q = select(models.OzonCashFlow).where(models.OzonCashFlow.period_begin.isnot(None))
    if from_:
        q = q.where(models.OzonCashFlow.period_end >= from_)
    if to_:
        q = q.where(models.OzonCashFlow.period_begin <= to_)
    rows = db.execute(
        q.order_by(models.OzonCashFlow.period_begin.asc())
    ).scalars().all()
    out = [{
        "period_begin": r.period_begin.isoformat() if r.period_begin else "",
        "period_end": r.period_end.isoformat() if r.period_end else "",
        "begin_balance": float(r.begin_balance or 0),
        "payments_amount": float(r.payments_amount or 0),
        "delivery_total": float(r.delivery_total or 0),
        "return_total": float(r.return_total or 0),
        "services_total": float(r.services_total or 0),
        "others_total": float(r.others_total or 0),
        "end_balance": float(r.end_balance or 0),
    } for r in rows]
    df = pd.DataFrame(out) if out else pd.DataFrame()
    df, ru = excel_io.project_export(df, OZON_CASHFLOW_RU_COLUMNS, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Движение средств")
    fname = f"ozon_cashflow_{from_}_{to_}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


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

OZON_DETAIL_SUMMARY_RU_COLUMNS = {
    "article": "Артикул", "name": "Наименование", "size": "Размер",
    "sizes_count": "Размеров", "offers_count": "Артикулов",
    "sells": "Продано, шт",
    "returns_qty": "Возвращено, шт", "postings": "Постингов",
    "seller_total": "Продажи (цена×кол-во), руб", "amount": "Реализовано, руб",
    "commission": "Комиссия, руб", "services": "Услуги, руб",
    "income": "К перечислению, руб", "ops_count": "Операций",
    "buyout_sum": "Сумма выкупов, руб", "buyout_percent": "Выкуп, %",
    "storage": "Хранение, руб",
}

OZON_BUYOUT_RU_COLUMNS = {
    "posting_number": "Постинг", "offer_id": "Артикул", "name": "Наименование",
    "sku": "SKU", "quantity": "Кол-во", "seller_price": "Цена, руб",
    "buyout_price": "Цена выкупа, руб", "amount": "Сумма выкупа, руб",
    "deduction_by_category_percent": "Дед., %", "vat_percent": "НДС, %",
}


@router.post("/ozon/detail")
def ozon_detail(date_from: Optional[str] = None, date_to: Optional[str] = None,
                write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    from_, to_ = _parse_window400(date_from, date_to)
    try:
        res = refresh_service.pull_oz_detail(db, from_, to_, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    if not int(excel):
        return _pull_json(res)
    df = res["df"]
    export = df.rename(columns=OZON_DETAIL_RU_COLUMNS) if not df.empty else df
    return _xlsx_response(export, f"ozon_detail_{from_}_{to_}.xlsx", res["count"])


@router.post("/ozon/buyout")
def ozon_buyout(date_from: Optional[str] = None, date_to: Optional[str] = None,
                write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    from_, to_ = _parse_window400(date_from, date_to)
    try:
        res = refresh_service.pull_oz_buyout(db, from_, to_, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    if not int(excel):
        return _pull_json(res)
    df = res["df"]
    export = df.rename(columns=OZON_BUYOUT_RU_COLUMNS) if not df.empty else df
    return _xlsx_response(export, f"ozon_buyout_{from_}_{to_}.xlsx", res["count"])


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


@router.post("/ozon/placement")
def ozon_placement(date_from: Optional[str] = None, date_to: Optional[str] = None,
                   write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    from_, to_ = _parse_window400(date_from, date_to)
    try:
        res = refresh_service.pull_oz_placements(db, from_, to_, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    if not int(excel):
        return _pull_json(res)
    df = res["df"]
    export = df.rename(columns=OZON_PLACEMENT_RU_COLUMNS) if not df.empty else df
    return _xlsx_response(export, f"ozon_placement_{from_}_{to_}.xlsx", res["count"])


@router.get("/ozon/placement-rows")
def api_ozon_placement_rows(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Сырые строки «Размещения (хранения)» Ozon (ozon_placements) с фильтрами."""
    from_, to_ = _parse_window400(date_from, date_to)
    q = select(models.OzonPlacement).where(models.OzonPlacement.date.isnot(None))
    if from_:
        q = q.where(models.OzonPlacement.date >= from_)
    if to_:
        q = q.where(models.OzonPlacement.date <= to_)
    if article_like:
        q = q.where(common_service.like_col(models.OzonPlacement.offer_id, article_like)
                    | common_service.like_col(models.OzonPlacement.base_article, article_like))
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = db.execute(
        q.order_by(models.OzonPlacement.date.asc(), models.OzonPlacement.offer_id.asc())
        .offset(offset).limit(limit)
    ).scalars().all()
    out = [{
        "date": r.date.isoformat() if r.date else "",
        "sku": r.sku, "offer_id": r.offer_id, "name": r.offer_id,
        "warehouse": r.warehouse, "paid_quantity": r.paid_quantity,
        "paid_volume": float(r.paid_volume or 0),
        "storage": float(r.storage or 0),
    } for r in rows]
    df = pd.DataFrame(out) if out else pd.DataFrame()
    return {"rows": out, "total": int(total),
            "totals": _df_totals(df) if len(df) else {},
            "window": {"date_from": date_from or "", "date_to": date_to or ""},
            "detail_range": ozon_date_range(db, models.OzonPlacement)}


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


@router.get("/ozon/placement-summary")
def api_ozon_placement_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    by_size: int = 0,
    db: Session = Depends(get_db),
):
    """Свод «Размещения (хранения)» Ozon по артикулам: дни, кол-во, объём, сумма.

    Показывает ВСЕ SKU из ozon_placements за окно (включая те, у которых
    в окне не было продаж) — в отличие от свода «Детализация продаж».

    by_size=0 (по умолчанию) — строка это товар (артикулы размеров свёрнуты),
    by_size=1 — строка это артикул конкретного размера.
    """
    from_, to_ = _parse_window400(date_from, date_to)
    out = _ozon_placement_agg(db, from_, to_, article_like, by_size=bool(by_size))
    df = pd.DataFrame(out) if out else pd.DataFrame()
    return {"rows": out, "count": len(out),
            "totals": _df_totals(df, extra_skip=(
                "days", "ops_count", "sizes_count", "offers_count")) if len(df) else {},
            "window": {"date_from": date_from or "", "date_to": date_to or ""},
            "detail_range": ozon_date_range(db, models.OzonPlacement)}


@router.get("/export/ozon/placement-summary")
def export_ozon_placement_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    by_size: int = 0,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)
    out = _ozon_placement_agg(db, from_, to_, article_like, by_size=bool(by_size))
    df = pd.DataFrame(out) if out else pd.DataFrame()
    df, ru = excel_io.project_export(df, OZON_PLACEMENT_SUMMARY_RU_COLUMNS, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Размещение по артикулам")
    fname = f"ozon_placement_{from_}_{to_}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export/ozon/placement-rows")
def export_ozon_placement_rows(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)
    q = select(models.OzonPlacement).where(models.OzonPlacement.date.isnot(None))
    if from_:
        q = q.where(models.OzonPlacement.date >= from_)
    if to_:
        q = q.where(models.OzonPlacement.date <= to_)
    if article_like:
        q = q.where(common_service.like_col(models.OzonPlacement.offer_id, article_like)
                    | common_service.like_col(models.OzonPlacement.base_article, article_like))
    rows = db.execute(
        q.order_by(models.OzonPlacement.date.asc(), models.OzonPlacement.offer_id.asc())
    ).scalars().all()
    out = [{
        "date": r.date.isoformat() if r.date else "",
        "sku": r.sku, "offer_id": r.offer_id, "name": r.offer_id,
        "warehouse": r.warehouse, "paid_quantity": r.paid_quantity,
        "paid_volume": float(r.paid_volume or 0),
        "storage": float(r.storage or 0),
    } for r in rows]
    df = pd.DataFrame(out) if out else pd.DataFrame()
    df, ru = excel_io.project_export(df, OZON_PLACEMENT_RU_COLUMNS, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Размещение по дням")
    fname = f"ozon_placement_rows_{from_}_{to_}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/ozon/detail-rows")
def api_ozon_detail_rows(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Сырые строки «Детализации реализаций» Ozon (ozon_detail_rows) с фильтрами."""
    from_, to_ = _parse_window400(date_from, date_to)
    q = select(models.OzonDetailRow).where(models.OzonDetailRow.date.isnot(None))
    if from_:
        q = q.where(models.OzonDetailRow.date >= from_)
    if to_:
        q = q.where(models.OzonDetailRow.date <= to_)
    if article_like:
        q = q.where(common_service.like_col(models.OzonDetailRow.offer_id, article_like)
                    | common_service.like_col(models.OzonDetailRow.base_article, article_like))
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = db.execute(
        q.order_by(models.OzonDetailRow.date.desc(), models.OzonDetailRow.id.desc())
        .offset(offset).limit(limit)
    ).scalars().all()
    out = [{
        "date": r.date.isoformat() if r.date else "",
        "posting_number": r.posting_number,
        "offer_id": r.offer_id,
        "name": r.name,
        "sku": r.sku,
        "barcode": r.barcode,
        "quantity": r.quantity,
        "seller_price": float(r.seller_price or 0),
        "amount": float(r.amount or 0),
        "commission_ratio": float(r.commission_ratio or 0),
        "commission": float(r.commission or 0),
        "standard_fee": float(r.standard_fee or 0),
        "income": float(r.income or 0),
        "return_qty": r.return_qty,
        "return_total": float(r.return_total or 0),
        "source": r.source,
    } for r in rows]
    df = pd.DataFrame(out) if out else pd.DataFrame()
    return {
        "rows": out,
        "total": int(total),
        "totals": _df_totals(df) if len(df) else {},
        "window": {"date_from": date_from or "", "date_to": date_to or ""},
        "detail_range": ozon_detail_range(db),
    }


@router.get("/ozon/detail-summary")
def api_ozon_detail_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    by_size: int = 0,
    db: Session = Depends(get_db),
):
    """Свод «Детализации реализаций» Ozon по артикулам (+ выкупы).

    by_size=0 (по умолчанию) — строка это товар (артикулы размеров свёрнуты),
    by_size=1 — строка это артикул конкретного размера.
    """
    from_, to_ = _parse_window400(date_from, date_to)
    df = sync_service.oz_detail_summary_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like,
        by_size=bool(by_size),
    )
    resp = {
        "rows": df.replace({None: ""}).to_dict("records"),
        "count": len(df),
        "totals": _df_totals(df, extra_skip=("sizes_count", "offers_count")),
    }
    # «Фактически получено на р/с» по периодам движения средств,
    # пересекающимся с окном (payments_amount хранится в минусе).
    received, periods = _cashflow_received(db, from_, to_)
    resp["cashflow_received"] = received
    resp["cashflow_periods"] = periods
    resp["window"] = {"date_from": date_from or "", "date_to": date_to or ""}
    resp["detail_range"] = ozon_detail_range(db)
    return resp


@router.get("/ozon/buyout-rows")
def api_ozon_buyout_rows(
    article_like: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Сырые строки «Выкупов» Ozon (ozon_buyouts) с фильтром по артикулу."""
    q = select(models.OzonBuyout)
    if article_like:
        q = q.where(common_service.like_col(models.OzonBuyout.offer_id, article_like)
                    | common_service.like_col(models.OzonBuyout.base_article, article_like))
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = db.execute(q.order_by(models.OzonBuyout.id.desc())
                      .offset(offset).limit(limit)).scalars().all()
    out = [{
        "posting_number": r.posting_number,
        "offer_id": r.offer_id,
        "name": r.name,
        "sku": r.sku,
        "quantity": r.quantity,
        "seller_price": float(r.seller_price or 0),
        "buyout_price": float(r.buyout_price or 0),
        "amount": float(r.amount or 0),
        "deduction_by_category_percent": float(r.deduction_by_category_percent or 0),
        "vat_percent": r.vat_percent,
    } for r in rows]
    return {"rows": out, "total": int(total)}


@router.get("/export/ozon/detail-summary")
def export_ozon_detail_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    by_size: int = 0,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)
    df = sync_service.oz_detail_summary_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like,
        by_size=bool(by_size),
    )
    df, ru = excel_io.project_export(df, OZON_DETAIL_SUMMARY_RU_COLUMNS, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Детализация по артикулам")
    fname = f"ozon_detail_summary_{from_}_{to_}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export/ozon/detail-rows")
def export_ozon_detail_rows(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    limit: int = 5000,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Экспорт сырых строк «Детализации реализаций» Ozon в Excel."""
    from_, to_ = _parse_window400(date_from, date_to)
    q = (
        select(models.OzonDetailRow)
        .where(models.OzonDetailRow.date.isnot(None))
        .order_by(models.OzonDetailRow.date, models.OzonDetailRow.id)
        .limit(limit)
    )
    if from_:
        q = q.where(models.OzonDetailRow.date >= from_)
    if to_:
        q = q.where(models.OzonDetailRow.date <= to_)
    if article_like:
        q = q.where(common_service.like_col(models.OzonDetailRow.offer_id, article_like)
                    | common_service.like_col(models.OzonDetailRow.base_article, article_like))
    rows = db.execute(q).scalars().all()
    recs = [{
        "date": r.date.isoformat() if r.date else "",
        "posting_number": r.posting_number, "offer_id": r.offer_id,
        "name": r.name, "sku": r.sku, "barcode": r.barcode,
        "quantity": r.quantity, "seller_price": float(r.seller_price or 0),
        "amount": float(r.amount or 0),
        "commission_ratio": float(r.commission_ratio or 0),
        "commission": float(r.commission or 0),
        "standard_fee": float(r.standard_fee or 0),
        "income": float(r.income or 0),
        "return_qty": r.return_qty, "return_total": float(r.return_total or 0),
        "source": r.source,
    } for r in rows]
    df = pd.DataFrame(recs, columns=list(OZON_DETAIL_RU_COLUMNS.keys()))
    df, ru = excel_io.project_export(df, OZON_DETAIL_RU_COLUMNS, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Строки детализации")
    fname = f"ozon_detail_rows_{from_}_{to_}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export/ozon/buyout-rows")
def export_ozon_buyout_rows(
    article_like: Optional[str] = None,
    limit: int = 5000,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Экспорт сырых строк «Выкупов» Ozon в Excel."""
    q = select(models.OzonBuyout).order_by(models.OzonBuyout.id).limit(limit)
    if article_like:
        q = q.where(common_service.like_col(models.OzonBuyout.offer_id, article_like)
                    | common_service.like_col(models.OzonBuyout.base_article, article_like))
    rows = db.execute(q).scalars().all()
    recs = [{
        "posting_number": r.posting_number, "offer_id": r.offer_id,
        "name": r.name, "sku": r.sku, "quantity": r.quantity,
        "seller_price": float(r.seller_price or 0),
        "buyout_price": float(r.buyout_price or 0),
        "amount": float(r.amount or 0),
        "deduction_by_category_percent": float(r.deduction_by_category_percent or 0),
        "vat_percent": r.vat_percent,
    } for r in rows]
    df = pd.DataFrame(recs, columns=list(OZON_BUYOUT_RU_COLUMNS.keys()))
    df, ru = excel_io.project_export(df, OZON_BUYOUT_RU_COLUMNS, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Выкупы")
    fname = "ozon_buyout_rows.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


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


@router.post("/promo/refresh")
def promo_refresh(db: Session = Depends(get_db)):
    """Одноразовое обновление акций WB (Календарь акций) в wb_promotions."""
    try:
        res = refresh_service.pull_wb_promotions(db, write_db=True)
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    return {
        "rows": res["rows"], "db_rows": res["db_rows"],
        "count": res["count"], "window": res["window"],
    }


@router.get("/promo/list")
def promo_list(db: Session = Depends(get_db)):
    """Сохранённые акции WB: актуальные и ближайшие (±90 дней), свежие сверху."""
    rows = db.scalars(
        select(models.Promotion)
        .order_by(models.Promotion.starts_at.desc())
        .limit(200)
    ).all()
    return {"promotions": [_promo_row(p) for p in rows]}