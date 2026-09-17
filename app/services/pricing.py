"""Автопилот скидок Wildberries.

По каждому товару собираются сигналы (скорость продаж и тренд/сезонность,
остатки/DOC, unit-экономика из истории продаж, воронка продаж, текущая цена
и скидка WB, докупаемость) и по правилам R1-R10 формируется целевая скидка.
Применение — через WbProvider.update_prices (POST upload/task), меняется только
скидка (базовая цена price не трогается).

Цена никогда не опускается ниже floor_price (break-even по unit-экономике).
"""
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
    "raise_pct_replenishable": 10.0,
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
        if key == "season_adj":
            out[key] = value if isinstance(value, bool) else str(value).lower() in ("1", "true", "on", "yes")
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
    latest = db.scalar(select(func.max(models.Stock.date)))
    if latest is None:
        return {}
    q = (
        select(models.Stock.article, func.sum(models.Stock.quantity).label("qty"))
        .where(models.Stock.date == latest, models.Stock.marketplace_id == wb_mp)
        .group_by(models.Stock.article)
    )
    return {str(art): int(qty or 0) for art, qty in db.execute(q)}


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
        "date": r.date, "article": str(r.article), "qty": int(r.qty or 0),
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
    last_sale_ago = {str(art): (today - d).days for art, d in last_sale}

    funnel = _funnel_slice(db, now_from, now_to)
    detail = _detail_metrics(db, now_from, now_to)
    stock = _latest_stock(db, wb_mp)
    nm_map = {
        str(r.nm_id).strip(): str(r.article).strip()
        for r in db.execute(select(models.NmArticle.nm_id, models.NmArticle.article))
    }
    prices = _normalize_prices(prices_df)

    products = {}
    for r in db.execute(select(
            models.Product.article, models.Product.name,
            models.Product.net_cost, models.Product.replenishable,
    ).order_by(models.Product.article)):
        products[str(r.article)] = {
            "name": r.name or "",
            "net_cost": _num(r.net_cost),
            "replenishable": bool(r.replenishable),
        }

    now_df = rows_df[(rows_df["date"] >= now_from)] if not rows_df.empty else rows_df
    prev_df = rows_df[(rows_df["date"] >= prev_from) & (rows_df["date"] <= prev_to)] if not rows_df.empty else rows_df
    fallback_df = rows_df

    out_rows = []
    for art, prod in products.items():
        nm_id = next((nm for nm, a in nm_map.items() if a == art), None)
        if nm_id is None or nm_id not in prices:
            out_rows.append(_row_skip(art, prod, None, "нет карточки/цен WB (нет nmID в списке цен)"))
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
        v_proj = project_velocity(v_now, v_prev, bool(s["season_adj"]), _num(s["season_damp"]))

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
            "net_cost": prod["net_cost"],
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

        out_rows.append(_decide(f, s))

    return {
        "rows": out_rows,
        "settings": s,
        "note": "",
        "total": len(out_rows),
        "actionable": sum(1 for r in out_rows if r["action"] in ("RAISE", "LOWER")),
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


def _decide(f: dict, s: dict) -> dict:
    price = f["price"]
    cur_disc = f["current_discount"]
    cur_vis = f["current_vis"]
    stock = f["stock"]
    v_proj = f["v_proj"]
    eff = min(cur_vis, f["avg_price"]) if f["avg_price"] > 0 else cur_vis
    floor = f["floor_price"]
    base = _base_row(f)
    base["eff"] = eff

    if stock is None:
        base.update(action="SKIP", status="skipped_no_stock",
                    reason="нет данных об остатках (вытяните WB Остатки)")
        return base
    if stock <= 0:
        base.update(action="HOLD", status="hold", reason="распродан (остаток 0)")
        return base
    if f["comm_rate"] is None or floor is None:
        base.update(action="SKIP", status="skipped_no_ratio",
                    reason="нет unit-экономики (нет продаж/доходов — не можем гарантировать break-even)")
        return base

    max_disc_item = min(_num(s["max_discount_pct"]), (1 - floor / price) * 100 if price > 0 else 0)
    base["max_discount_item"] = round(max_disc_item, 1)

    doc = stock / v_proj if v_proj > 0 else float("inf")
    base["doc"] = round(doc, 1) if doc != float("inf") else None
    conv = (f["orders"] / f["views"] * 100) if f["views"] > 0 else 0.0
    base["conv_pct"] = round(conv, 2)
    backlog = max(f["adds"] - f["orders"], 0)
    base["backlog"] = backlog

    # R3: пена возвратов/отмен
    if f["returns_ratio"] > _num(s["return_penalty"]):
        base.update(action="SKIP", status="skipped_returns",
                    reason=f"высокая доля возвратов/отмен ({f['returns_ratio']:.0%}) — спрос мыльный, не трогаем")
        return base

    hot_backlog = f["adds"] > 0 and f["adds"] >= _num(s["hot_backlog_factor"]) * max(f["orders"], 0)

    # R9: много в корзинах, но не покупают
    if hot_backlog and conv < _num(s["low_conv_pct"]):
        base.update(action="SKIP", status="skipped_carts",
                    reason="много в корзинах, но конверсия низкая — цена тормозит сделку, решить вне автопилота")
        return base

    hot_demand = conv >= _num(s["hot_conv_pct"]) or hot_backlog

    # R4: мёртвый запас
    if v_proj <= 0:
        if f["last_sale_days_ago"] >= int(s["dead_stock_days"]):
            target_vis = max(floor, eff * (1 - _num(s["max_drop_pct"]) / 100))
            target_vis = min(target_vis, cur_vis)
            target_vis = max(target_vis, price * (1 - max_disc_item / 100))
            new_disc = (1 - target_vis / price) * 100 if price > 0 else cur_disc
            if new_disc - cur_disc >= _num(s["min_delta_pp"]):
                base.update(action="LOWER", status="suggested",
                            reason=f"мёртвый запас: продаж нет {f['last_sale_days_ago']} дн., остаток {stock} шт",
                            target_discount=round(_clamp(new_disc, 0, 100), 1),
                            target_vis=round(target_vis, 2),
                            margin_pct_at_target=_margin_pct(target_vis, f))
            else:
                base.update(action="HOLD", status="hold", reason="дельта скидки меньше порога")
            return base
        base.update(action="HOLD", status="hold", reason="продаж нет (менее порога) — не трогаем")
        return base

    if doc < _num(s["doc_low"]):
        if hot_demand:
            raise_pct = _num(s["raise_pct_replenishable"]) if f["replenishable"] else _num(s["max_raise_pct"])
            target_vis = min(price, cur_vis * (1 + raise_pct / 100))
            new_disc = (1 - target_vis / price) * 100 if price > 0 else cur_disc
            if cur_disc - new_disc >= _num(s["min_delta_pp"]):
                base.update(action="RAISE", status="suggested",
                            reason=f"дефицит (DOC={doc:.0f} дн.) и горячий спрос "
                                   f"(conv={conv:.1f}% / в корзинах {backlog})",
                            target_discount=round(_clamp(new_disc, 0, 100), 1),
                            target_vis=round(target_vis, 2),
                            margin_pct_at_target=_margin_pct(target_vis, f))
            else:
                base.update(action="HOLD", status="hold", reason="дельта скидки меньше порога")
            return base
        if not f["replenishable"]:
            target_vis = min(price, cur_vis * (1 + _num(s["max_raise_pct"]) / 100))
            new_disc = (1 - target_vis / price) * 100 if price > 0 else cur_disc
            if cur_disc - new_disc >= _num(s["min_delta_pp"]):
                base.update(action="RAISE", status="suggested",
                            reason=f"дефицит (DOC={doc:.0f} дн.), товар не докупается — последние единицы",
                            target_discount=round(_clamp(new_disc, 0, 100), 1),
                            target_vis=round(target_vis, 2),
                            margin_pct_at_target=_margin_pct(target_vis, f))
            else:
                base.update(action="HOLD", status="hold", reason="дельта скидки меньше порога")
            return base
        base.update(action="HOLD", status="hold",
                    reason=f"дефицит, но товар докупаемый — темп важнее")
        return base

    if doc >= _num(s["doc_high"]):
        k = 0.5
        if conv < 1.0:
            k *= 0.8
        if f["trend"] < 1.0:
            k *= 0.8
        elif f["trend"] > 1.0:
            k *= 1.2
        k = _clamp(k, 0.2, 1.2)
        factor = max(1 - _num(s["max_drop_pct"]) / 100, (_num(s["target_doc"]) / doc) ** k)
        target_vis = max(floor, eff * factor, price * (1 - max_disc_item / 100))
        target_vis = min(target_vis, cur_vis)
        new_disc = (1 - target_vis / price) * 100 if price > 0 else cur_disc
        if new_disc - cur_disc >= _num(s["min_delta_pp"]):
            trend_note = ""
            if bool(s["season_adj"]) and f["trend"] != 1.0:
                trend_note = " · тренд " + ("растёт ↓" if f["trend"] > 1 else "падает ↑")
            base.update(action="LOWER", status="suggested",
                        reason=f"перезапас (DOC={doc:.0f} дн.), целевые {_num(s['target_doc']):.0f} дн." + trend_note,
                        target_discount=round(_clamp(new_disc, 0, 100), 1),
                        target_vis=round(target_vis, 2),
                        margin_pct_at_target=_margin_pct(target_vis, f))
        else:
            base.update(action="HOLD", status="hold", reason="дельта скидки меньше порога")
        return base

    base.update(action="HOLD", status="hold", reason=f"нормальные остатки (DOC={doc:.0f} дн.)")
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
        if r["action"] not in ("RAISE", "LOWER"):
            continue
        if r["article"] in applied_past:
            r["status"] = "skipped_cooldown"
            r["reason"] += f" — кулдаун {int(s['cooldown_days'])} дн. после предыдущего изменения"
            continue
        try:
            nm = int(r["nm_id"])
        except (ValueError, TypeError):
            nm = r["nm_id"]
        items.append({
            "nmID": nm,
            "price": float(r["price"]),
            "discount": float(r["target_discount"]),
        })
    return items


def _record(db: Session, rows: list, applied_at: Optional[datetime], error: Optional[str] = None):
    for r in rows:
        is_applied = (r["action"] in ("RAISE", "LOWER")
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
    rec = recommendations(db, s, prices_df=prices_df, provider=provider, today=today)
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
        if r["action"] in ("RAISE", "LOWER") and r["status"] == "suggested":
            r["status"] = "applied"

    _record(db, rows, applied_at=now)
    applied_ids = [r["article"] for r in rows if r["status"] == "applied"]
    return {"applied": applied_ids, "rows": rows, "pushed": len(items),
            "task_id": result.get("task_id") if isinstance(result, dict) else None,
            "note": f"Применено изменений: {len(applied_ids)}."}