"""Автопилот скидок Wildberries.

По каждому товару собираются сигналы (скорость продаж и тренд/сезонность,
остатки/DOC, unit-экономика из истории продаж, воронка продаж, текущая цена
и скидка WB, докупаемость) и по правилам R1-R10 формируется целевая скидка.
Применение — через WbProvider.update_prices (POST upload/task), меняется только
скидка (базовая цена price не трогается).

Цена никогда не опускается ниже floor_price (break-even по unit-экономике).
"""
import math
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.providers import factory as provider_factory

PRICING_DEFAULTS = {
    "window_days": 14,
    "target_doc": 30,
    "doc_low": 14,
    "doc_high": 60,
    "floor_margin_pct": 10.0,
    "max_discount_pct": 50.0,
    "max_raise_pct": 15.0,
    "max_drop_pct": 15.0,
    "min_delta_pp": 1.0,
    "cooldown_days": 3,
    "season_adj": True,
    "season_damp": 0.5,
    "min_days_with_sales": 5,
    "hot_conv_pct": 1.5,
    "hot_backlog_factor": 2.0,
    "return_penalty": 0.3,
    "dead_stock_days": 7,
    "low_conv_pct": 0.7,
    "fallback_window_days": 90,
    # «Рейтинг по отзывам» из воронки продаж (1..5): чем выше рейтинг — тем
    # меньше скидку мы даём при снижении (ценный товар). От 0 = нет данных
    # ограничений нет.
    "min_rating_reviews": 4.0,
    "raise_pct_replenishable": 10.0,
    # T-14: quality gates for RAISE
    "min_rating_for_raise": 4.2,
    "min_conv_buyout_for_raise": 40.0,
    "max_cancel_ratio_for_raise": 0.2,
    "max_return_rate_for_raise": 15.0,
    # T-14: strong signals → bolder RAISE
    "strong_rating": 4.5,
    "strong_buyout_conv": 60.0,
    "strong_return_rate": 5.0,
    "strong_margin_pct": 30.0,
    "raise_boost_pct": 25.0,
    # T-21: нулевые/мёртвые товары + противовес автоскидкам WB
    "show_zero": False,
    "dead_min_discount": 1.0,
    "prefer_raise": True,
    "prefer_raise_bias": 0.15,
    # T-24: какие параметры влияют на решение (панель-чекбоксы в UI);
    # выключенный фактор не меняет цену на основании своего сигнала.
    "use_inventory": True,
    "use_sales": True,
    "use_orders": True,
    "use_margin": True,
    "use_replenishable": True,
    "use_season": True,
    "use_reviews": True,
    "use_quality": False,
    "use_returns": False,
    # Версия расчёта: "new" — правила R1-R10 (рекомендуется), "old" — порт
    # старой эвристики из finance/price_module (НЕ рекомендуется, только для
    # сравнения результатов; общие стражи безопасности действуют в обеих).
    "mode": "new",
}


def merge_settings(payload=None) -> dict:
    """Склеивает параметры из запроса с дефолтами (только известные ключи).

    date_from/date_to — строки ISO (период анализа из шапки), пропускаются
    как есть; применяются в recommendations() поверх window_days.
    """
    out = dict(PRICING_DEFAULTS)
    for key, value in (payload or {}).items():
        if key in ("date_from", "date_to") and value:
            try:
                date.fromisoformat(str(value)[:10])
            except (TypeError, ValueError):
                continue
            out[key] = str(value)[:10]
            continue
        if key not in out:
            continue
        if isinstance(out[key], bool):
            out[key] = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "on", "yes")
        elif isinstance(out[key], str):
            out[key] = value if isinstance(value, str) else out[key]
        else:
            try:
                out[key] = float(value)
            except (TypeError, ValueError):
                continue
    return out


def project_velocity(v_now: float, v_prev: float, season_adj: bool = True, damp: float = 0.5) -> float:
    """Скорость с учётом текущего тренда (momentum-сезонность).

    Единая точка проекции скорости: позже сюда же можно подставить
    недельный индекс или сравнение год-к-году без изменения правил.
    """
    if not season_adj or v_now <= 0:
        return float(v_now)
    if v_prev <= 0:
        return float(v_now)
    g = max(0.5, min(2.0, v_now / v_prev))
    return float(v_now * (g ** damp))


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _wb_id(db: Session) -> int:
    return db.execute(
        select(models.Marketplace.id).where(models.Marketplace.code == "wb")
    ).scalar_one()


def _normalize_prices(prices_df: Optional[pd.DataFrame]) -> dict:
    """nmID -> {price, discount}. Терпим к разным наборам колонок провайдера."""
    out: dict = {}
    if prices_df is None or prices_df.empty:
        return out
    for row in prices_df.to_dict("records"):
        nm = str(row.get("nmID", row.get("nm_id", ""))).strip()
        if not nm:
            continue
        price = _num(row.get("price"))
        if price <= 0:
            continue
        discount = _num(row.get("discount"))
        if "discountedPrice" in row and discount == 0:
            disc_price = _num(row.get("discountedPrice"))
            if price > disc_price > 0:
                discount = (price - disc_price) / price * 100
        out[nm] = {"price": price, "discount": max(0.0, min(discount, 100.0))}
    return out


def _funnel_slice(db: Session, date_from: date, date_to: date) -> dict:
    """Последний срез воронки, пересекающий окно [date_from, date_to].

    Воронка WB — снимок за период (не по дням), поэтому берём одну (максимально
    свежую) запись среза и не суммируем срезы. Возвращает расширенный набор
    метрик (рейтинги, выкупы, конверсии, остатки WB, WB Клуб, вишлисты).
    Ключи — и как в БД, и в верхнем регистре (артикулы продуктов могут
    отличаться регистром от воронки/детализации).
    """
    snap = db.execute(
        select(models.FunnelMetric.date_from, models.FunnelMetric.date_to)
        .where(
            models.FunnelMetric.date_from <= date_to,
            models.FunnelMetric.date_to >= date_from,
        )
        .order_by(models.FunnelMetric.date_to.desc())
    ).first()
    out: dict = {}
    if snap is None:
        return out
    from_d, to_d = snap.date_from, snap.date_to
    q = (
        select(
            models.FunnelMetric.article,
            func.sum(models.FunnelMetric.views).label("views"),
            func.sum(models.FunnelMetric.adds).label("adds"),
            func.sum(models.FunnelMetric.orders).label("orders"),
            func.sum(models.FunnelMetric.cancelled).label("cancelled"),
            func.max(models.FunnelMetric.avg_price).label("avg_price"),
            func.sum(models.FunnelMetric.buyouts).label("buyouts"),
            func.max(models.FunnelMetric.product_rating).label("product_rating"),
            func.max(models.FunnelMetric.feedback_rating).label("feedback_rating"),
            func.max(models.FunnelMetric.conv_to_cart_percent).label("conv_to_cart_percent"),
            func.max(models.FunnelMetric.conv_cart_to_order_percent).label("conv_cart_to_order_percent"),
            func.max(models.FunnelMetric.conv_buyout_percent).label("conv_buyout_percent"),
            func.max(models.FunnelMetric.add_to_wishlist).label("add_to_wishlist"),
            func.max(models.FunnelMetric.share_order_percent).label("share_order_percent"),
            func.max(models.FunnelMetric.avg_orders_per_day).label("avg_orders_per_day"),
            func.max(models.FunnelMetric.stock_wb).label("stock_wb"),
            func.max(models.FunnelMetric.cancel_sum).label("cancel_sum"),
            func.max(models.FunnelMetric.wb_club_buyout_percent).label("wb_club_buyout_percent"),
            func.max(models.FunnelMetric.subject_name).label("subject_name"),
            func.max(models.FunnelMetric.brand_name).label("brand_name"),
        )
        .where(models.FunnelMetric.date_from == from_d, models.FunnelMetric.date_to == to_d)
        .group_by(models.FunnelMetric.article)
    )
    for r in db.execute(q):
        rec = {
            "views": int(r.views or 0),
            "adds": int(r.adds or 0),
            "orders": int(r.orders or 0),
            "cancelled": int(r.cancelled or 0),
            "avg_price": _num(r.avg_price),
            "buyouts": int(r.buyouts or 0),
            "product_rating": _num(r.product_rating),
            "feedback_rating": _num(r.feedback_rating),
            "conv_buyout_percent": _num(r.conv_buyout_percent),
            "conv_to_cart_percent": _num(r.conv_to_cart_percent),
            "conv_cart_to_order_percent": _num(r.conv_cart_to_order_percent),
            "add_to_wishlist": int(r.add_to_wishlist or 0),
            "share_order_percent": _num(r.share_order_percent),
            "avg_orders_per_day": _num(r.avg_orders_per_day),
            "stock_wb": int(r.stock_wb or 0),
            "cancel_sum": _num(r.cancel_sum),
            "wb_club_buyout_percent": _num(r.wb_club_buyout_percent),
            "subject_name": str(r.subject_name or ""),
            "brand_name": str(r.brand_name or ""),
        }
        art = str(r.article).strip()
        out[art] = rec
        out[art.upper()] = rec
    return out


def _detail_metrics(db: Session, date_from: date, date_to: date) -> dict:
    """Фактические деньги из детализации продаж WB за окно (по артикулам).

    Использует margin_detail_dataframe: возвраты, маржа/шт, комиссия, логистика,
    хранение, средний чек, выручка/шт. При отсутствии детализации возвращает {}.
    """
    from app.services.margin import margin_detail_dataframe

    try:
        dframe = margin_detail_dataframe(db, date_from=date_from, date_to=date_to)
    except Exception:  # noqa: BLE001
        # необязательный источник: откат обязателен, иначе оборванная транзакция
        # уронит все последующие запросы расчёта (InFailedSqlTransaction)
        db.rollback()
        return {}
    if dframe is None or dframe.empty:
        return {}
    out: dict = {}
    for row in dframe.to_dict("records"):
        art = str(row.get("article", "")).strip()
        if not art:
            continue
        rec = {
            "return_rate": _num(row.get("return_rate")),
            "margin_pct": _num(row.get("margin_pct")),
            "margin_per_one": _num(row.get("margin_per_one")),
            "income_per_one": _num(row.get("income_per_one")),
            "revenue_per_one": _num(row.get("revenue_per_one")),
            "commission_per_one": _num(row.get("commission_per_one")),
            "logistics_per_one": _num(row.get("logistics_per_one")),
            "storage_per_one": _num(row.get("storage_per_one")),
            "detail_sells": int(row.get("sells") or 0),
            "detail_returns_qty": int(row.get("returns_qty") or 0),
        }
        out[art] = rec
        out[art.upper()] = rec
    return out


def _latest_stock(db: Session, wb_mp: int) -> dict:
    latest = db.scalar(
        select(func.max(models.Stock.date))
        .where(models.Stock.marketplace_id == wb_mp)
    )
    if latest is None:
        return {}
    q = (
        select(
            models.Stock.article,
            func.sum(models.Stock.quantity + models.Stock.in_way).label("qty"),
        )
        .where(models.Stock.date == latest, models.Stock.marketplace_id == wb_mp)
        .group_by(models.Stock.article)
    )
    out: dict = {}
    for art, qty in db.execute(q):
        val = int(qty or 0)
        out[str(art)] = val
        out[str(art).upper()] = val
    return out


def _unit_economics(rows: pd.DataFrame) -> Optional[dict]:
    """Приводит unit-экономику по суммам sales (колонки: revenue, commission, ...)."""
    if rows is None or rows.empty:
        return None
    revenue = _num(rows["revenue"].sum())
    qty = max(_num(rows["qty"].sum()), 0)
    if revenue <= 0 or qty <= 0:
        return None
    comm = abs(_num(rows["commission"].sum()))
    comm_rate = min(0.5, comm / revenue)
    logistics_unit = abs(_num(rows["logistics"].sum())) / qty
    storage_unit = abs(_num(rows["storage"].sum())) / qty
    other_unit = abs(_num(rows["services"].sum())) / qty
    return {
        "comm_rate": comm_rate,
        "logistics_unit": logistics_unit,
        "storage_unit": storage_unit,
        "other_unit": other_unit,
        "qty": qty,
    }


def recommendations(
    db: Session,
    settings=None,
    prices_df: Optional[pd.DataFrame] = None,
    provider=None,
    today: Optional[date] = None,
) -> dict:
    """Read-only расчёт рекомендаций по правилам R1-R10."""
    s = merge_settings(settings)
    mode = "old" if str(s.get("mode") or "").strip().lower() in ("old", "legacy") else "new"
    s["mode"] = mode
    today = today or date.today()
    wb_mp = _wb_id(db)
    W = int(s["window_days"])
    fallback_days = int(s["fallback_window_days"])

    # Период анализа: явные date_from/date_to из шапки (или дефолт window_days).
    req_from = req_to = None
    for key in ("date_from", "date_to"):
        raw = s.get(key)
        if isinstance(raw, str) and raw:
            try:
                parsed = date.fromisoformat(raw[:10])
            except ValueError:
                continue
            if key == "date_from":
                req_from = parsed
            else:
                req_to = min(parsed, today)
    if req_from is None or req_to is None:
        req_from = req_from if req_from is not None else today - timedelta(days=W - 1)
        req_to = req_to if req_to is not None else today
    if req_to < req_from:
        req_to = req_from

    min_date = db.scalar(
        select(func.min(models.Sale.date)).where(models.Sale.marketplace_id == wb_mp)
    )
    if min_date is None:
        return {"rows": [], "settings": s, "note": "Нет данных о продажах WB."}
    span = (today - min_date).days + 1
    cover = min((req_to - req_from).days + 1, span)
    if cover < int(s["min_days_with_sales"]):
        return {
            "rows": [], "settings": s,
            "note": f"Мало истории продаж WB (покрыто {cover} дн. из {W}). "
                    f"Нужно ≥ {int(s['min_days_with_sales'])} дн. — сначала тяните данные (Обновить WB).",
        }

    # Окно анализа якорим на самые свежие данные запрошенного периода.
    now_to = req_to
    now_from = now_to - timedelta(days=cover - 1)
    prev_to = now_from - timedelta(days=1)
    prev_from = now_to - timedelta(days=2 * cover - 1)

    sales_range_from = today - timedelta(days=fallback_days - 1)
    q = (
        select(
            models.Sale.date,
            models.Sale.article,
            func.sum(models.Sale.quantity - models.Sale.returns_qty).label("qty"),
            func.sum(models.Sale.quantity).label("qty_sold"),
            func.sum(models.Sale.returns_qty).label("ret"),
            func.sum(models.Sale.revenue).label("revenue"),
            func.sum(models.Sale.commission).label("commission"),
            func.sum(models.Sale.logistics).label("logistics"),
            func.sum(models.Sale.storage).label("storage"),
            func.sum(models.Sale.services).label("services"),
        )
        .where(
            models.Sale.marketplace_id == wb_mp,
            models.Sale.source != "detail",
            models.Sale.date >= sales_range_from,
            models.Sale.date <= today,
        )
        .group_by(models.Sale.date, models.Sale.article)
    )
    rows_df = pd.DataFrame([{
        "date": r.date, "article": str(r.article).strip().upper(),
        "qty": int(r.qty or 0),
        "qty_sold": int(r.qty_sold or 0), "ret": int(r.ret or 0),
        "revenue": _num(r.revenue), "commission": _num(r.commission),
        "logistics": _num(r.logistics), "storage": _num(r.storage),
        "services": _num(r.services),
    } for r in db.execute(q)])

    last_sale = db.execute(
        select(models.Sale.article, func.max(models.Sale.date).label("d"))
        .where(models.Sale.marketplace_id == wb_mp)
        .group_by(models.Sale.article)
    )
    last_sale_ago = {str(art).upper(): (today - d).days for art, d in last_sale}

    funnel = _funnel_slice(db, now_from, now_to)
    detail = _detail_metrics(db, now_from, now_to)
    stock = _latest_stock(db, wb_mp)
    nm_map = {
        str(r.nm_id).strip(): str(r.article).strip().upper()
        for r in db.execute(select(models.NmArticle.nm_id, models.NmArticle.article))
    }
    prices = _normalize_prices(prices_df)

    # Базас автопилота — только WB-карточки: article -> nmID. Товары без
    # nm-карты (каталог Ozon/не-WB) из расчёта исключаются полностью.
    art_to_nm: dict = {}
    for nm, art in nm_map.items():
        if art not in art_to_nm:
            art_to_nm[art] = nm

    products = {}
    for r in db.execute(select(
            models.Product.article, models.Product.name,
            models.Product.net_cost, models.Product.replenishable,
    ).order_by(models.Product.article)):
        products[str(r.article).strip().upper()] = {
            "name": r.name or "",
            "net_cost": _num(r.net_cost),
            "replenishable": bool(r.replenishable),
        }

    now_df = rows_df[(rows_df["date"] >= now_from)] if not rows_df.empty else rows_df
    prev_df = rows_df[(rows_df["date"] >= prev_from) & (rows_df["date"] <= prev_to)] if not rows_df.empty else rows_df
    fallback_df = rows_df

    # Старая модель: глобальный k_norma_revenue как в count_norma_revenue()
    # (clear_sells / (clear_sells − expenses) × 1.5; при нулевой выручке — 3).
    legacy_k_norma = None
    if mode == "old" and not rows_df.empty:
        clear = _num(rows_df["revenue"].sum())
        expenses = (_num(rows_df["commission"].sum()) + _num(rows_df["logistics"].sum())
                    + _num(rows_df["storage"].sum()) + _num(rows_df["services"].sum()))
        revenue = clear - expenses
        if clear > 0 and revenue > 0:
            legacy_k_norma = (clear / revenue) * 1.5

    # Товары с хоть каким-то «сигналом жизни»: продажи в окне, остаток > 0,
    # просмотры/заказы в воронке, продажи/возвраты в детализации.
    # Всё остальное — «мёртвые»: скрываются по умолчанию (show_zero=False).
    has_life: set = set()
    if not now_df.empty:
        has_life |= {str(a) for a in now_df[now_df["qty"] > 0]["article"]}
    has_life |= {
        a for a, v in funnel.items()
        if int(v.get("views") or 0) > 0 or int(v.get("orders") or 0) > 0
    }
    has_life |= {
        a for a, v in detail.items()
        if int(v.get("detail_sells") or 0) > 0 or int(v.get("detail_returns_qty") or 0) > 0
    }
    has_life |= {a for a, v in stock.items() if _num(v) > 0}

    show_zero = bool(s["show_zero"])
    use_season = bool(s.get("use_season", True))
    use_sales = bool(s.get("use_sales", True))
    hidden_dead = 0
    non_wb = 0
    out_rows = []
    for art, prod in products.items():
        nm_id = art_to_nm.get(art)
        if nm_id is None:
            non_wb += 1
            continue
        if art not in has_life and not show_zero:
            hidden_dead += 1
            continue
        if nm_id not in prices:
            out_rows.append(_row_skip(art, prod, nm_id, "нет карточки/цен WB (нет nmID в списке цен)"))
            continue
        pr = prices[nm_id]
        price = pr["price"]
        cur_disc = pr["discount"]
        cur_vis = price * (1 - cur_disc / 100)

        art_now = now_df[now_df["article"] == art] if not now_df.empty else now_df
        art_prev = prev_df[prev_df["article"] == art] if not prev_df.empty else prev_df
        art_fb = fallback_df[fallback_df["article"] == art] if not fallback_df.empty else fallback_df

        s_now = _num(art_now["qty"].sum()) if not art_now.empty else 0.0
        s_prev = _num(art_prev["qty"].sum()) if not art_prev.empty else 0.0
        v_now = s_now / cover
        v_prev = s_prev / cover
        v_proj = project_velocity(v_now, v_prev,
                                  use_season and bool(s["season_adj"]),
                                  _num(s["season_damp"]))
        if not use_sales:
            v_proj = 0.0

        ue = _unit_economics(art_fb)
        fl = funnel.get(art, {})
        dl = detail.get(art, {})
        f = {
            "article": art, "name": prod["name"], "replenishable": prod["replenishable"],
            "nm_id": nm_id, "price": price, "current_discount": cur_disc,
            "current_vis": cur_vis, "stock": stock.get(art),
            "velocity": v_now, "v_proj": v_proj, "trend": (v_now / v_prev) if v_prev > 0 else 1.0,
            "last_sale_days_ago": last_sale_ago.get(art, 9999),
            "avg_price": _num(fl.get("avg_price")),
            "adds": int(fl.get("adds", 0)),
            "orders": int(fl.get("orders", 0)),
            "cancelled": int(fl.get("cancelled", 0)),
            "views": int(fl.get("views", 0)),
            "buyouts": int(fl.get("buyouts", 0)),
            "product_rating": _num(fl.get("product_rating")),
            "feedback_rating": _num(fl.get("feedback_rating")),
            "conv_buyout_percent": _num(fl.get("conv_buyout_percent")),
            "conv_to_cart_percent": _num(fl.get("conv_to_cart_percent")),
            "conv_cart_to_order_percent": _num(fl.get("conv_cart_to_order_percent")),
            "add_to_wishlist": int(fl.get("add_to_wishlist", 0)),
            "share_order_percent": _num(fl.get("share_order_percent")),
            "avg_orders_per_day": _num(fl.get("avg_orders_per_day")),
            "stock_wb": int(fl.get("stock_wb", 0)),
            "cancel_sum": _num(fl.get("cancel_sum")),
            "wb_club_buyout_percent": _num(fl.get("wb_club_buyout_percent")),
            "return_rate": _num(dl.get("return_rate")),
            "margin_pct": _num(dl.get("margin_pct")),
            "margin_per_one": _num(dl.get("margin_per_one")),
            "income_per_one": _num(dl.get("income_per_one")),
            "revenue_per_one": _num(dl.get("revenue_per_one")),
            "commission_per_one": _num(dl.get("commission_per_one")),
            "logistics_per_one": _num(dl.get("logistics_per_one")),
            "storage_per_one": _num(dl.get("storage_per_one")),
            "detail_sells": int(dl.get("detail_sells", 0)),
            "detail_returns_qty": int(dl.get("detail_returns_qty", 0)),
            "detail_known": bool(dl),
            "funnel_known": bool(fl),
            "net_cost": prod["net_cost"],
            "sells_net": s_now, "cover_days": cover,
            "comm_rate": None, "logistics_unit": 0.0, "storage_unit": 0.0,
            "other_unit": 0.0, "floor_price": None,
        }
        qty_sold = int(art_now["qty_sold"].sum()) if not art_now.empty else 0
        ret_now = int(art_now["ret"].sum()) if not art_now.empty else 0
        pena = ret_now + f["cancelled"]
        f["returns_ratio"] = pena / qty_sold if qty_sold > 0 else 0.0

        if ue is not None:
            f["comm_rate"] = ue["comm_rate"]
            f["logistics_unit"] = ue["logistics_unit"]
            f["storage_unit"] = ue["storage_unit"]
            f["other_unit"] = ue["other_unit"]
            denom = 1 - ue["comm_rate"] - _num(s["floor_margin_pct"]) / 100
            if denom > 0.05:
                floor = (ue["logistics_unit"] + ue["storage_unit"]
                         + ue["other_unit"] + f["net_cost"]) / denom
                f["floor_price"] = max(0.0, floor)

        if mode == "old":
            out_rows.append(_decide_legacy(f, s, legacy_k_norma))
        else:
            out_rows.append(_decide_dead(f, s) if art not in has_life else _decide(f, s))

    return {
        "rows": out_rows,
        "settings": s,
        "note": "",
        "total": len(out_rows),
        "actionable": sum(1 for r in out_rows if r["action"] in ("RAISE", "LOWER", "HALVE")),
        "hidden_dead": hidden_dead,
        "non_wb": non_wb,
        "as_of": str(today),
        "date_from": str(now_from),
        "date_to": str(now_to),
        "window_days": cover,
    }


ENRICHED_KEYS = {
    "buyouts", "product_rating", "feedback_rating", "conv_buyout_percent",
    "conv_to_cart_percent", "conv_cart_to_order_percent", "add_to_wishlist",
    "share_order_percent", "avg_orders_per_day", "stock_wb", "cancel_sum",
    "wb_club_buyout_percent", "return_rate", "margin_pct", "margin_per_one",
    "income_per_one", "revenue_per_one", "commission_per_one",
    "logistics_per_one", "storage_per_one", "detail_sells", "detail_returns_qty",
}


def _row_skip(art, prod, nm_id, reason):
    return {
        "article": art, "name": prod["name"],
        "replenishable": prod["replenishable"], "nm_id": nm_id or "",
        "price": 0.0, "current_discount": 0.0, "current_vis": 0.0,
        "avg_price": 0.0, "stock": None, "doc": None,
        "velocity": 0.0, "v_proj": 0.0, "conv_pct": None, "backlog": 0,
        "net_cost": _num(prod["net_cost"]), "floor_price": None,
        "max_discount_item": None, "action": "SKIP", "status": "skipped_no_data",
        "reason": reason, "target_discount": None, "target_vis": None,
        "margin_pct_at_target": None,
        **_enriched({}),
    }


def _opt(value) -> Optional[float]:
    """float-значение или None (для Nullable-метрик вроде рейтинга)."""
    try:
        f_val = float(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(f_val):
        return None
    return f_val


def _enriched(f: dict) -> dict:
    """Новые колонки T-13: из воронки (funnel) и детализации (margin_detail)."""
    return {
        "buyouts": int(f.get("buyouts") or 0),
        "product_rating": _opt(f.get("product_rating", float("nan"))),
        "feedback_rating": _opt(f.get("feedback_rating", float("nan"))),
        "conv_buyout_percent": _num(f.get("conv_buyout_percent")),
        "conv_to_cart_percent": _num(f.get("conv_to_cart_percent")),
        "conv_cart_to_order_percent": _num(f.get("conv_cart_to_order_percent")),
        "add_to_wishlist": int(f.get("add_to_wishlist") or 0),
        "share_order_percent": _num(f.get("share_order_percent")),
        "avg_orders_per_day": _num(f.get("avg_orders_per_day")),
        "stock_wb": int(f.get("stock_wb") or 0),
        "cancel_sum": _num(f.get("cancel_sum")),
        "wb_club_buyout_percent": _num(f.get("wb_club_buyout_percent")),
        "return_rate": _num(f.get("return_rate")),
        "margin_pct": _num(f.get("margin_pct")),
        "margin_per_one": _num(f.get("margin_per_one")),
        "income_per_one": _num(f.get("income_per_one")),
        "revenue_per_one": _num(f.get("revenue_per_one")),
        "commission_per_one": _num(f.get("commission_per_one")),
        "logistics_per_one": _num(f.get("logistics_per_one")),
        "storage_per_one": _num(f.get("storage_per_one")),
        "detail_sells": int(f.get("detail_sells") or 0),
        "detail_returns_qty": int(f.get("detail_returns_qty") or 0),
    }


def _base_row(f: dict) -> dict:
    return {
        "article": f["article"], "name": f["name"], "replenishable": f["replenishable"],
        "nm_id": f["nm_id"], "price": f["price"], "current_discount": f["current_discount"],
        "current_vis": f["current_vis"], "avg_price": f["avg_price"], "eff": None,
        "stock": f["stock"], "velocity": f["velocity"], "v_proj": f["v_proj"],
        "doc": None, "conv_pct": None, "backlog": 0, "net_cost": f["net_cost"],
        "comm_rate": f["comm_rate"], "floor_price": f["floor_price"],
        "max_discount_item": None, "trend": round(f["trend"], 2),
        "action": "HOLD", "status": "hold", "reason": "",
        "target_discount": None, "target_vis": None, "margin_pct_at_target": None,
        **_enriched(f),
    }


def _raise_quality_gate(f: dict, s: dict) -> Optional[str]:
    """Блокировка RAISE, если сигналы качества запрещают повышение.

    Возвращает текст причины или None (повышать можно).
    Сигналы с нулевым значением трактуются как «нет данных» и не блокируют.
    """
    rating = f.get("product_rating", 0)
    if rating > 0 and rating < _num(s["min_rating_for_raise"]):
        return f"рейтинг {rating:.1f} — качество не позволяет поднимать цену"
    buyout = f.get("conv_buyout_percent", 0)
    if buyout > 0 and buyout < _num(s["min_conv_buyout_for_raise"]):
        return f"конверсия выкупа {buyout:.1f}% — низкая выкупаемость"
    orders = f.get("orders", 0)
    if orders > 0:
        cancel_ratio = f.get("cancelled", 0) / orders
        if cancel_ratio > _num(s["max_cancel_ratio_for_raise"]):
            return f"отмены {cancel_ratio:.0%} от заказов — спрос мыльный"
    if f.get("detail_known") and f.get("return_rate", 0) > _num(s["max_return_rate_for_raise"]):
        return f"возвраты {f['return_rate']:.1f}% — брак/неликвид"
    return None


def _raise_boost(f: dict, s: dict) -> float:
    """Множитель uplift при сильных сигналах качества (≥2 из 4).

    Возвращает 1.0 при недостатке данных или слабых сигналах.
    """
    strong = 0
    if f.get("product_rating", 0) >= _num(s["strong_rating"]):
        strong += 1
    if f.get("conv_buyout_percent", 0) >= _num(s["strong_buyout_conv"]):
        strong += 1
    if f.get("detail_known") and f.get("return_rate", 0) <= _num(s["strong_return_rate"]):
        strong += 1
    if f.get("detail_known") and f.get("margin_pct", 0) >= _num(s["strong_margin_pct"]):
        strong += 1
    return (1 + _num(s["raise_boost_pct"]) / 100) if strong >= 2 else 1.0


def _decide_dead(f: dict, s: dict) -> dict:
    """Мёртвый товар (нет ни одного сигнала) с живой карточкой WB: скидка пополам.

    Каждый прогон «Применить» делит текущую скидку на 2 (50→25→12→6→3→1), пока
    шаг не станет меньше min_delta_pp или не упрётся в dead_min_discount.
    Это мягкий «противовес» автоскидкам WB: цена постепенно восстанавливается.
    """
    base = _base_row(f)
    base["eff"] = min(f["current_vis"], f["avg_price"]) if f["avg_price"] > 0 else f["current_vis"]
    cur = f["current_discount"]
    price = f["price"]
    floor_min = _num(s["dead_min_discount"])
    half = math.floor(cur / 2) if cur > 0 else 0
    target = max(min(half, cur), floor_min)
    if target >= cur or cur - target < _num(s["min_delta_pp"]):
        base.update(action="HOLD", status="hold",
                    reason=f"мёртвый товар: скидка уже минимальна ({cur:.1f}%)")
        return base
    target_vis = price * (1 - target / 100) if price > 0 else 0.0
    base.update(action="HALVE", status="suggested",
                reason=f"мёртвый товар (нет продаж, остатков и активности): скидка ÷2 "
                       f"{cur:.0f}%→{target:.0f}%, до мин. {floor_min:.0f}%",
                target_discount=round(_clamp(target, 0, 100), 1),
                target_vis=round(target_vis, 2),
                margin_pct_at_target=_margin_pct(target_vis, f))
    return base


def _rating_discount_scale(fb_rating, threshold) -> float:
    """Множитель снижения скидки по рейтингу по отзывам (воронка, 1..5).

    Чем выше рейтинг — тем меньше скидка при LOWER (товар ценный).
    0/нет данных или рейтинг ≤ порога → 1.0 (без ограничений),
    5.0 → 0.0 (скидку не увеличиваем вовсе), между порогом и 5 — линейно.
    """
    r = _num(fb_rating)
    th = _num(threshold)
    if r <= 0 or r <= th:
        return 1.0
    if r >= 5.0:
        return 0.0
    denom = 5.0 - th
    return (5.0 - r) / denom if denom > 0 else 0.0


def _half_discount_guard(cur_disc: float, new_disc: float, factual: float, net_cost: float) -> float:
    """R: при RAISE (скидку уменьшаем) никогда не режем её более чем пополам.

    Исключение — фактическая цена продажи опустилась ниже себестоимости: тогда
    повышаем свободно (иначе продолжаем отдавать товар в убыток).
    """
    if factual >= net_cost:
        return max(new_disc, cur_disc / 2.0)
    return new_disc


def _mandatory_step(base: dict, f: dict, s: dict, direction: str, reason: str) -> dict:
    """R: если есть остаток — изменение скидки обязательно, HOLD не выдаём.

    Ставим минимальный шаг в заданном направлении (max(min_delta_pp, 1 п.п.))
    с уважением к капсам (floor, max_discount_item, минимальная цена WB) и к правилу
    «скидку при RAISE не режем более чем пополам». Если шаг в заданном направлении
    невозможен — пробуем противоположное. Если не выходит вовсе (цена <= 0 или оба
    направления упираются в капсы) — оставляем HOLD как есть.
    """
    step = max(_num(s["min_delta_pp"], 1.0), 1.0)
    price = f["price"]
    if price <= 0:
        return base
    cur_disc = f["current_discount"]
    cur_vis = f["current_vis"]
    eff = min(cur_vis, f["avg_price"]) if f["avg_price"] > 0 else cur_vis
    net_cost = _num(f.get("net_cost"))
    factual = f["avg_price"] if f["avg_price"] > 0 else eff
    max_disc_item = base.get("max_discount_item")
    if max_disc_item is None:
        max_disc_item = _num(s["max_discount_pct"])
    suffix = f" — коррекция скидки обязательна (есть остаток {f['stock']} шт)"

    def try_lower():
        target_vis = max(price * (1 - (cur_disc + step) / 100),
                         _num(f["floor_price"]),
                         price * (1 - max_disc_item / 100))
        target_vis = min(target_vis, cur_vis)
        new_disc = (1 - target_vis / price) * 100 if price > 0 else cur_disc
        if new_disc > cur_disc + 1e-9:
            return new_disc, target_vis
        return None, None

    def try_raise():
        new_disc = max(cur_disc - step, 0.0)
        new_disc = _half_discount_guard(cur_disc, new_disc, factual, net_cost)
        if new_disc < cur_disc - 1e-9:
            return new_disc, price * (1 - new_disc / 100)
        return None, None

    def emit(action, nd, tv):
        base.update(action=action, status="suggested", reason=reason + suffix,
                    target_discount=round(_clamp(nd, 0, 100), 1),
                    target_vis=round(tv, 2),
                    margin_pct_at_target=_margin_pct(tv, f))

    if direction == "RAISE":
        nd, tv = try_raise()
        if nd is not None:
            emit("RAISE", nd, tv)
            return base
        nd, tv = try_lower()
        if nd is not None:
            emit("LOWER", nd, tv)
        return base
    nd, tv = try_lower()
    if nd is not None:
        emit("LOWER", nd, tv)
        return base
    nd, tv = try_raise()
    if nd is not None:
        emit("RAISE", nd, tv)
    return base


def _decide(f: dict, s: dict) -> dict:
    price = f["price"]
    cur_disc = f["current_discount"]
    cur_vis = f["current_vis"]
    stock = f["stock"]
    v_proj = f["v_proj"]
    eff = min(cur_vis, f["avg_price"]) if f["avg_price"] > 0 else cur_vis
    # Фактическая цена продажи (есть данные о продаже — avg_price, иначе витрина).
    factual = f["avg_price"] if f["avg_price"] > 0 else eff
    floor = f["floor_price"]
    base = _base_row(f)
    base["eff"] = eff

    # T-24: включаемые пользователем факторы решения (панель-чекбоксы в UI).
    # Выключенный фактор не участвует в правилах; его данные остаются в колонках.
    use_inv = bool(s.get("use_inventory", True))
    use_sales = bool(s.get("use_sales", True))
    use_orders = bool(s.get("use_orders", True))
    use_margin = bool(s.get("use_margin", True))
    use_repl = bool(s.get("use_replenishable", True))
    use_season = bool(s.get("use_season", True))
    use_reviews = bool(s.get("use_reviews", True))
    use_quality = bool(s.get("use_quality", False))
    use_returns = bool(s.get("use_returns", False))

    # Предпочтение «поднимать, а не опускать» (противовес автоскидкам WB):
    # зона дефицита шире (док_ло выше), зона перезапаса уже (док_хай выше),
    # шаг снижения мягче.
    bias = _num(s["prefer_raise_bias"]) if s.get("prefer_raise") else 0.0
    doc_low = _num(s["doc_low"]) * (1 + bias)
    doc_high = _num(s["doc_high"]) * (1 + bias)
    max_drop = _num(s["max_drop_pct"]) * (1 - bias)
    # Рейтинг по отзывам (ценный товар): ограничивает скидку при LOWER.
    rating_scale = (_rating_discount_scale(f.get("feedback_rating"), s.get("min_rating_reviews", 4.0))
                    if use_reviews else 1.0)

    if use_inv and stock is None:
        base.update(action="SKIP", status="skipped_no_stock",
                    reason="нет данных об остатках (вытяните WB Остатки)")
        return base
    if use_inv and stock <= 0:
        base.update(action="HOLD", status="hold", reason="распродан (остаток 0)")
        return base

    if use_margin:
        if f["comm_rate"] is None or floor is None:
            base.update(action="SKIP", status="skipped_no_ratio",
                        reason="нет unit-экономики (нет продаж/доходов — не можем гарантировать break-even)")
            return base
        max_disc_item = min(_num(s["max_discount_pct"]), (1 - floor / price) * 100 if price > 0 else 0)
    else:
        # без поля безубыточности ниже опускаться нельзя лишь до потолка max_discount_pct
        max_disc_item = _num(s["max_discount_pct"])
    base["max_discount_item"] = round(max_disc_item, 1)

    doc = stock / v_proj if v_proj > 0 and stock is not None else float("inf")
    base["doc"] = round(doc, 1) if doc != float("inf") else None
    conv = (f["orders"] / f["views"] * 100) if f["views"] > 0 else 0.0
    base["conv_pct"] = round(conv, 2)
    backlog = max(f["adds"] - f["orders"], 0)
    base["backlog"] = backlog

    # R3: пена возвратов/отмен (фактор «Возвраты/отмены», по умолчанию выкл.)
    if use_returns and f["returns_ratio"] > _num(s["return_penalty"]):
        base.update(action="SKIP", status="skipped_returns",
                    reason=f"высокая доля возвратов/отмен ({f['returns_ratio']:.0%}) — спрос мыльный, не трогаем")
        return base

    hot_backlog = use_orders and f["adds"] > 0 and f["adds"] >= _num(s["hot_backlog_factor"]) * max(f["orders"], 0)

    # R9: много в корзинах, но не покупают (фактор «Заказы и конверсия»)
    if use_orders and hot_backlog and conv < _num(s["low_conv_pct"]):
        base.update(action="SKIP", status="skipped_carts",
                    reason="много в корзинах, но конверсия низкая — цена тормозит сделку, решить вне автопилота")
        return base

    hot_demand = use_orders and (conv >= _num(s["hot_conv_pct"]) or hot_backlog)
    # докупаемость участвует, только пока включён фактор
    repl = use_repl and f["replenishable"]

    # R4: мёртвый запас (фактор «Продажи»)
    if use_sales and v_proj <= 0:
        if f["last_sale_days_ago"] >= int(s["dead_stock_days"]):
            if rating_scale <= 0.01:
                return _mandatory_step(
                    base, f, s, "LOWER",
                    f"ценный товар (рейтинг по отзывам {f.get('feedback_rating', 0):.1f}) "
                    f"— скидку не увеличиваем")
            target_vis = max(_num(floor), eff * (1 - max_drop * rating_scale / 100))
            target_vis = min(target_vis, cur_vis)
            target_vis = max(target_vis, price * (1 - max_disc_item / 100))
            new_disc = (1 - target_vis / price) * 100 if price > 0 else cur_disc
            if new_disc - cur_disc >= _num(s["min_delta_pp"]):
                base.update(action="LOWER", status="suggested",
                            reason=f"мёртвый запас: продаж нет {f['last_sale_days_ago']} дн., остаток {stock} шт",
                            target_discount=round(_clamp(new_disc, 0, 100), 1),
                            target_vis=round(target_vis, 2),
                            margin_pct_at_target=_margin_pct(target_vis, f))
                return base
            else:
                return _mandatory_step(
                    base, f, s, "LOWER",
                    f"мёртвый запас: продаж нет {f['last_sale_days_ago']} дн., остаток {stock} шт")
        return _mandatory_step(base, f, s, "LOWER", "продаж нет (менее порога)")

    if not use_sales:
        if stock is None or stock <= 0:
            base.update(action="HOLD", status="hold",
                        reason="продажи не влияют (фактор выключен) — нет сигнала для изменения цены")
        else:
            return _mandatory_step(
                base, f, s,
                "LOWER" if doc >= _num(s["target_doc"]) else "RAISE",
                "продажи не влияют (фактор выключен)")
        return base
    if not use_inv:
        if stock is None or stock <= 0:
            base.update(action="HOLD", status="hold",
                        reason="остаток не влияет (фактор выключен) — нет сигнала для изменения цены")
        else:
            return _mandatory_step(
                base, f, s,
                "LOWER" if doc >= _num(s["target_doc"]) else "RAISE",
                "остаток не влияет (фактор выключен)")
        return base

    if doc < doc_low:
        if hot_demand:
            gate = _raise_quality_gate(f, s) if use_quality else None
            if gate:
                base.update(action="SKIP", status="skipped_quality", reason=gate)
                return base
            boost = _raise_boost(f, s) if use_quality else 1.0
            raise_pct = (_num(s["raise_pct_replenishable"]) if repl else _num(s["max_raise_pct"])) * boost
            target_vis = min(price, cur_vis * (1 + raise_pct / 100))
            new_disc = (1 - target_vis / price) * 100 if price > 0 else cur_disc
            new_disc = _half_discount_guard(cur_disc, new_disc, factual, _num(f["net_cost"]))
            target_vis = price * (1 - new_disc / 100) if price > 0 else target_vis
            if cur_disc - new_disc >= _num(s["min_delta_pp"]):
                base.update(action="RAISE", status="suggested",
                            reason=f"дефицит (DOC={doc:.0f} дн.) и горячий спрос "
                                   f"(conv={conv:.1f}% / в корзинах {backlog})"
                                   + (" · сильные сигналы" if boost > 1 else ""),
                            target_discount=round(_clamp(new_disc, 0, 100), 1),
                            target_vis=round(target_vis, 2),
                            margin_pct_at_target=_margin_pct(target_vis, f))
            else:
                return _mandatory_step(
                    base, f, s, "RAISE", f"дефицит (DOC={doc:.0f} дн.) и горячий спрос")
            return base
        if not repl:
            gate = _raise_quality_gate(f, s) if use_quality else None
            if gate:
                base.update(action="SKIP", status="skipped_quality", reason=gate)
                return base
            boost = _raise_boost(f, s) if use_quality else 1.0
            target_vis = min(price, cur_vis * (1 + _num(s["max_raise_pct"]) * boost / 100))
            new_disc = (1 - target_vis / price) * 100 if price > 0 else cur_disc
            new_disc = _half_discount_guard(cur_disc, new_disc, factual, _num(f["net_cost"]))
            target_vis = price * (1 - new_disc / 100) if price > 0 else target_vis
            if cur_disc - new_disc >= _num(s["min_delta_pp"]):
                base.update(action="RAISE", status="suggested",
                            reason=f"дефицит (DOC={doc:.0f} дн.), товар не докупается — последние единицы"
                                   + (" · сильные сигналы" if boost > 1 else ""),
                            target_discount=round(_clamp(new_disc, 0, 100), 1),
                            target_vis=round(target_vis, 2),
                            margin_pct_at_target=_margin_pct(target_vis, f))
            else:
                return _mandatory_step(
                    base, f, s, "RAISE", f"дефицит (DOC={doc:.0f} дн.), товар не докупается")
            return base
        return _mandatory_step(
            base, f, s, "RAISE", f"дефицит (DOC={doc:.0f} дн., товар докупаемый) — темп важнее")

    if doc >= doc_high:
        if rating_scale <= 0.01:
            return _mandatory_step(
                base, f, s, "LOWER",
                f"ценный товар (рейтинг по отзывам {f.get('feedback_rating', 0):.1f}) "
                f"— скидку не увеличиваем")
        k = 0.5
        if use_orders and conv < 1.0:
            k *= 0.8
        if use_season:
            if f["trend"] < 1.0:
                k *= 0.8
            elif f["trend"] > 1.0:
                k *= 1.2
        k = _clamp(k, 0.2, 1.2)
        factor = max(1 - max_drop * rating_scale / 100, (_num(s["target_doc"]) / doc) ** k)
        target_vis = max(_num(floor), eff * factor, price * (1 - max_disc_item / 100))
        target_vis = min(target_vis, cur_vis)
        new_disc = (1 - target_vis / price) * 100 if price > 0 else cur_disc
        if new_disc - cur_disc >= _num(s["min_delta_pp"]):
            trend_note = ""
            if use_season and bool(s["season_adj"]) and f["trend"] != 1.0:
                trend_note = " · тренд " + ("растёт ↓" if f["trend"] > 1 else "падает ↑")
            base.update(action="LOWER", status="suggested",
                        reason=f"перезапас (DOC={doc:.0f} дн.), целевые {_num(s['target_doc']):.0f} дн." + trend_note,
                        target_discount=round(_clamp(new_disc, 0, 100), 1),
                        target_vis=round(target_vis, 2),
                        margin_pct_at_target=_margin_pct(target_vis, f))
        else:
            return _mandatory_step(
                base, f, s, "LOWER",
                f"перезапас (DOC={doc:.0f} дн.), целевые {_num(s['target_doc']):.0f} дн.")
        return base

    return _mandatory_step(
        base, f, s,
        "LOWER" if doc >= _num(s["target_doc"]) else "RAISE",
        f"нормальные остатки (DOC={doc:.0f} дн.), целевые {_num(s['target_doc']):.0f} дн.")


# ----------------------------------------------------------------------
# «Старая версия» коррекции скидки — порт finance/price_module.discount().
# Режим НЕ рекомендуется: эвристика на взвешенных k-коэффициентах вместо
# правил R1-R10, переносится для сравнения результатов. Общие стражи этого
# автопилота (пол безубыточности floor_price, кламп 0..100, потолок
# max_discount_pct) действуют и здесь, чтобы Apply не увёл витринную цену
# ниже точки убытка.

DEFAULT_NET_COST = 500.0
DEFAULT_PURE_VALUE = DEFAULT_NET_COST * 1.2  # 600


def _legacy_k_is_sell(pure_sells_qt: float, net_cost: float, pure_value: float) -> float:
    """price_module.k_is_sell: чем больше чистых продаж — тем больше скидка.

    Пороги масштабируются на k_net_cost = sqrt(550 / ((pure_value + net_cost) / 2)).
    """
    if not net_cost:
        net_cost = DEFAULT_NET_COST
    if not pure_value:
        pure_value = DEFAULT_PURE_VALUE
    k_nc = (((DEFAULT_NET_COST + DEFAULT_PURE_VALUE) * 0.5) / ((pure_value + net_cost) * 0.5)) ** 0.5
    if pure_sells_qt > 50 * k_nc:
        return 0.60
    if pure_sells_qt > 20 * k_nc:
        return 0.70
    if pure_sells_qt > 10 * k_nc:
        return 0.80
    if pure_sells_qt > 5 * k_nc:
        return 0.85
    if pure_sells_qt > 3 * k_nc:
        return 0.90
    if pure_sells_qt > 2 * k_nc:
        return 0.95
    if pure_sells_qt > 1 * k_nc:
        return 0.98
    if pure_sells_qt >= 1:
        return 0.99
    return 1.01


def _legacy_k_cost(cost: float, price_disc: float, k_norma_revenue: float) -> float:
    """price_module.k_cost: лестница по отношению скидочной цены к затратам.

    k = sqrt(DEFAULT_NET_COST / cost), снизу клампится в 1 (как в старой
    системе): у цены у самой себестоимости k < 1 — модель поднимает цену
    (защита от убытка); у дорогого товара k = 1 и скидка растёт.
    """
    if not cost:
        cost = DEFAULT_NET_COST
    k = (DEFAULT_NET_COST / cost) ** 0.5
    if k < 1:
        k = 1
    if price_disc <= cost / 4:
        return 0.50
    if price_disc <= cost / 2:
        return 0.60
    if price_disc <= cost:
        return 0.70
    if price_disc <= cost * k:
        return 0.80
    if price_disc <= cost * ((0.50 * k_norma_revenue) * k):
        return 0.85
    if price_disc <= cost * ((0.60 * k_norma_revenue) * k):
        return 0.89
    if price_disc <= cost * ((0.70 * k_norma_revenue) * k):
        return 0.91
    if price_disc <= cost * ((0.80 * k_norma_revenue) * k):
        return 0.93
    if price_disc <= cost * ((0.90 * k_norma_revenue) * k):
        return 0.95
    if price_disc <= cost * ((0.92 * k_norma_revenue) * k):
        return 0.97
    if price_disc <= cost * ((0.96 * k_norma_revenue) * k):
        return 0.98
    if price_disc <= cost * ((0.97 * k_norma_revenue) * k):
        return 0.985
    if price_disc <= cost * ((0.98 * k_norma_revenue) * k):
        return 0.99
    if price_disc <= cost * ((0.99 * k_norma_revenue) * k):
        return 0.995
    if price_disc >= cost * ((5 * k_norma_revenue) * k):
        return 1.20
    if price_disc >= cost * ((2.5 * k_norma_revenue) * k):
        return 1.10
    if price_disc >= cost * ((2 * k_norma_revenue) * k):
        return 1.06
    if price_disc >= cost * ((1.5 * k_norma_revenue) * k):
        return 1.05
    if price_disc >= cost * ((1.25 * k_norma_revenue) * k):
        return 1.04
    if price_disc >= cost * ((1.15 * k_norma_revenue) * k):
        return 1.03
    if price_disc >= cost * ((1.1 * k_norma_revenue) * k):
        return 1.02
    if price_disc > cost * ((1.05 * k_norma_revenue) * k):
        return 1.01
    if price_disc > cost * ((k_norma_revenue) * k):
        return 1.0
    return 1.0


def _legacy_k_qt_full(qt: float, volume: float = 1.0) -> float:
    """price_module.k_qt_full: объём остатка шт × объём единицы.

    В новой системе объём единицы отсутствует — proxy volume = 1 (фактически
    отрабатывает «объём остатка в штуках»).
    """
    k_volume = 2.0
    v_all = qt * volume
    if v_all < 1:
        return 0.96
    if v_all < 1 * k_volume:
        return 0.95
    if v_all <= 1 * k_volume:
        return 0.96
    if v_all <= 2 * k_volume:
        return 0.97
    if v_all <= 3 * k_volume:
        return 0.98
    if v_all <= 5 * k_volume:
        return 0.99
    if v_all <= 10 * k_volume:
        return 1.0
    if 10 * k_volume < v_all <= 20 * k_volume:
        return 1.01
    if 20 * k_volume < v_all <= 50 * k_volume:
        return 1.03
    if 50 * k_volume < v_all <= 70 * k_volume:
        return 1.06
    if 70 * k_volume < v_all <= 100 * k_volume:
        return 1.09
    if v_all > 100 * k_volume:
        return 1.12
    return 1.0


def _legacy_k_logistic(log_rub: float, net_cost: float) -> float:
    """price_module.k_logistic (ветка to_rub == 0 — раздельной логистики продажи нет).

    Высокая логистика/шт — повод поднять цену (защита от «покатушек»).
    """
    if not log_rub:
        return 1.0
    if not net_cost:
        net_cost = DEFAULT_NET_COST
    if log_rub >= net_cost * 2:
        return 0.90
    if log_rub >= net_cost:
        return 0.95
    if log_rub >= net_cost / 2:
        return 0.97
    if log_rub >= net_cost / 4:
        return 0.98
    return 1.0


def _decide_legacy(f: dict, s: dict, k_norma_revenue: Optional[float] = None) -> dict:
    """«Старая версия» корректировки скидки (порт finance price_module.discount()).

    Режим НЕ рекомендуется: работает по взвешенным k-коэффициентам старой
    системы (k_is_sell×2, k_logistic×1, k_net_cost×3, k_pure_value×1,
    k_qt_full×1) вместо правил R1-R10. Выход тот же контракт (action/status/
    target_discount/target_vis/reason), что и у новой версии, поэтому Apply,
    кулдаун и журнал меняться не должны.
    """
    base = _base_row(f)

    price = _num(f["price"])
    cur_disc = _num(f["current_discount"])
    cur_vis = _num(f["current_vis"])
    net_cost = _num(f.get("net_cost"))
    if net_cost <= 0:
        net_cost = DEFAULT_NET_COST
    pure_value = net_cost * 1.2
    price_disc = cur_vis  # старая система брала скидочную цену из отчёта WB
    pure_sells_qt = _num(f.get("sells_net"))
    cover = max(int(_num(f.get("cover_days"), 0)), 1)
    smooth_days = (cover / 7.0) ** 0.25
    if k_norma_revenue is None:
        k_norma_revenue = 3.0

    k_is_sell = _legacy_k_is_sell(pure_sells_qt, net_cost, pure_value)
    k_logistic = _legacy_k_logistic(_num(f.get("logistics_unit")), net_cost)
    k_net_cost = _legacy_k_cost(net_cost, price_disc, k_norma_revenue)
    k_pure_value = _legacy_k_cost(pure_value, price_disc, k_norma_revenue)
    stock = f.get("stock")
    k_qt_full = _legacy_k_qt_full(_num(stock)) if stock is not None else 1.0

    k_discount = (k_is_sell * 2 + k_logistic * 1 + k_net_cost * 3
                  + k_pure_value * 1 + k_qt_full * 1) / 8.0

    # n_discount(price_disc, k_discount, price, k_delta=1)
    if price > 0:
        raw = (1 - price_disc / (price * k_discount ** 1)) * 100
        if raw < 0:
            raw = 0.0
    else:
        raw = 0.0
    default_changing = cur_disc / 4.0 if cur_disc > 0 else 0.0
    n_discount = raw
    if n_discount <= 0:
        n_discount = default_changing
    n_delta = round((cur_disc - n_discount) / smooth_days, 2)
    n_discount = round(cur_disc - n_delta, 2)
    if n_discount <= 0:
        n_discount = 0.0
    # reset_if_null: остаток распродан — скидку почти снимаем (цена восстанавливается).
    if stock is not None and stock <= 0:
        n_discount = default_changing

    target_discount = _clamp(n_discount, 0.0, _num(s["max_discount_pct"], 100.0))
    target_vis = price * (1 - target_discount / 100) if price > 0 else 0.0

    # Общий страж: пол безубыточности (всегда).
    if target_vis and price > 0:
        lo = _num(f.get("floor_price"))
        if lo and cur_vis > lo:
            target_vis = max(target_vis, lo)
            target_discount = (1 - target_vis / price) * 100
    target_discount = _clamp(target_discount, 0.0, 100.0)
    if price > 0:
        target_vis = price * (1 - target_discount / 100)
    else:
        target_vis = 0.0

    v_proj = _num(f["v_proj"])
    if v_proj > 0 and stock is not None:
        base["doc"] = round(stock / v_proj, 1)
    base["conv_pct"] = round((f["orders"] / f["views"] * 100), 2) if f["views"] > 0 else 0.0
    base["backlog"] = max(f["adds"] - f["orders"], 0)
    base["eff"] = min(cur_vis, f["avg_price"]) if f["avg_price"] > 0 else cur_vis
    base["max_discount_item"] = round(min(_num(s["max_discount_pct"]), 100.0), 1)

    if target_discount < cur_disc - 1e-9:
        action, status = "RAISE", "suggested"
    elif target_discount > cur_disc + 1e-9:
        action, status = "LOWER", "suggested"
    else:
        base.update(action="HOLD", status="hold",
                    reason=f"старая модель: без изменений (k={k_discount:.3f})")
        return base
    base.update(
        action=action, status=status,
        reason=(f"старая модель коррекции скидки (k={k_discount:.3f}, "
                f"k_is_sell={k_is_sell:.2f}, k_logistic={k_logistic:.2f}, "
                f"k_net_cost={k_net_cost:.2f}, k_pure_value={k_pure_value:.2f}, "
                f"k_qt_full={k_qt_full:.2f}, n_disc={n_discount:.1f}%, "
                f"сглаживание {smooth_days:.2f} дн)"),
        target_discount=round(target_discount, 1),
        target_vis=round(target_vis, 2),
        margin_pct_at_target=_margin_pct(target_vis, f),
    )
    return base


def _margin_pct(target_vis: float, f: dict) -> Optional[float]:
    """Ожидаемая маржа при целевой витринной цене, % от цены."""
    if f["comm_rate"] is None or target_vis <= 0:
        return None
    unit = (target_vis * (1 - f["comm_rate"])
            - f["logistics_unit"] - f["storage_unit"] - f["other_unit"] - f["net_cost"])
    return round(unit / target_vis * 100, 1)


def _pushed_items(rows: list, s: dict, applied_past: set) -> list:
    items = []
    for r in rows:
        if r["action"] not in ("RAISE", "LOWER", "HALVE"):
            continue
        # HALVE (мёртвые товары) не тонет в кулдауне: делим скидку на каждом
        # прогоне «Применить», пока есть шаг.
        if r["action"] != "HALVE" and r["article"] in applied_past:
            r["status"] = "skipped_cooldown"
            r["reason"] += f" — кулдаун {int(s['cooldown_days'])} дн. после предыдущего изменения"
            continue
        try:
            nm = int(r["nm_id"])
        except (ValueError, TypeError):
            nm = r["nm_id"]
        if not isinstance(nm, int) or nm <= 0:
            # upload/task требует числовой nmID: без WB-карты из задачи исключаем
            r["status"] = "skipped_no_nm"
            r["reason"] += " — нет nm-карты WB, в задачу изменения цен не включён"
            continue
        # upload/task принимает только целые price и discount (0..99): иначе 400.
        items.append({
            "nmID": nm,
            "price": max(100, int(float(r["price"] or 0))),
            "discount": max(0, min(99, int(float(r["target_discount"] or 0)))),
        })
    return items


def _record(db: Session, rows: list, applied_at: Optional[datetime], error: Optional[str] = None):
    for r in rows:
        is_applied = (r["action"] in ("RAISE", "LOWER", "HALVE")
                      and r.get("status") == "applied"
                      and error is None)
        db.add(models.PriceChange(
            article=r["article"], nm_id=str(r.get("nm_id") or ""),
            calculated_at=datetime.now(), applied_at=applied_at if is_applied else None,
            before_discount=float(r.get("current_discount") or 0),
            after_discount=float(r.get("target_discount")) if is_applied else None,
            action=r.get("action") or "",
            status=(r.get("status") or "") if not error else "error",
            reason=str(error) if error else (r.get("reason", "") or ""),
        ))
    db.commit()


def apply_recommendations(
    db: Session,
    settings=None,
    prices_df: Optional[pd.DataFrame] = None,
    provider=None,
    today: Optional[date] = None,
) -> dict:
    """Расчёт + применение скидок через WbProvider.update_prices + журнал PriceChange."""
    s = merge_settings(settings)
    # Мёртвые товары скрыты от UI, но применение обязано их обработать
    # (HALVE — шаг «скидка ÷2» идёт каждый прогон).
    rec_settings = merge_settings(s)
    rec_settings["show_zero"] = True
    rec = recommendations(db, rec_settings, prices_df=prices_df, provider=provider,
                          today=today)
    rows = rec["rows"]
    today = today or date.today()
    cooldown_from = today - timedelta(days=int(s["cooldown_days"]))

    applied_past = set(db.scalars(
        select(models.PriceChange.article).where(
            models.PriceChange.status == "applied",
            models.PriceChange.applied_at >= cooldown_from,
        )
    ).all())

    items = _pushed_items(rows, s, applied_past)
    now = datetime.now()
    prov = provider or provider_factory.get_wb_provider()

    if not items:
        _record(db, rows, applied_at=now)
        return {"applied": [], "rows": rows, "pushed": 0,
                "note": "Нет кандидатов на изменение (все hold/skip или в кулдауне)."}

    try:
        result = prov.update_prices(items)
    except Exception as exc:  # noqa: BLE001
        _record(db, rows, applied_at=now, error=str(exc))
        raise

    for r in rows:
        if r["action"] in ("RAISE", "LOWER", "HALVE") and r["status"] == "suggested":
            r["status"] = "applied"

    _record(db, rows, applied_at=now)
    applied_ids = [r["article"] for r in rows if r["status"] == "applied"]
    note = f"Применено изменений: {len(applied_ids)}."
    n_halve = sum(1 for r in rows
                  if r["action"] == "HALVE" and r["status"] == "applied")
    if n_halve:
        note += f" Из них деление скидки пополам у мёртвых: {n_halve}."
    return {"applied": applied_ids, "rows": rows, "pushed": len(items),
            "task_id": result.get("task_id") if isinstance(result, dict) else None,
            "note": note}


def apply_rows(
    db: Session,
    rows: list,
    settings=None,
    provider=None,
    today: Optional[date] = None,
) -> dict:
    """Применяет скидки ровно для переданных строк — видимых в таблице автопилота.

    Используется кнопкой «Применить в WB»: сервер НЕ пересчитывает рекомендации,
    а отправляет на API WB то, что пользователь видит в таблице (с учётом фильтров
    поиска и скрытых колонок). Строки — row-объекты из recommendations():
    article, nm_id, price, current_discount, target_discount, action, status, reason.
    Кулдаун работает как обычно.
    """
    s = merge_settings(settings or {})
    today = today or date.today()
    rows = [dict(r) for r in rows]
    now = datetime.now()
    cooldown_from = today - timedelta(days=int(s["cooldown_days"]))

    applied_past = set(db.scalars(
        select(models.PriceChange.article).where(
            models.PriceChange.status == "applied",
            models.PriceChange.applied_at >= cooldown_from,
        )
    ).all())
    items = _pushed_items(rows, s, applied_past)
    prov = provider or provider_factory.get_wb_provider()

    if not items:
        _record(db, rows, applied_at=now)
        return {"applied": [], "rows": rows, "pushed": 0,
                "note": "Нет кандидатов на изменение (все hold/skip или в кулдауне)."}

    try:
        result = prov.update_prices(items)
    except Exception as exc:  # noqa: BLE001
        _record(db, rows, applied_at=now, error=str(exc))
        raise

    for r in rows:
        if r["action"] in ("RAISE", "LOWER", "HALVE") and r["status"] == "suggested":
            r["status"] = "applied"

    _record(db, rows, applied_at=now)
    applied_ids = [r["article"] for r in rows if r["status"] == "applied"]
    note = f"Применено изменений: {len(applied_ids)} (из видимых в таблице: {len(rows)})."
    return {"applied": applied_ids, "rows": rows, "pushed": len(items),
            "task_id": result.get("task_id") if isinstance(result, dict) else None,
            "note": note}