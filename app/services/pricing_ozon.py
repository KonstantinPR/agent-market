# -*- coding: utf-8 -*-
"""Автопилот цен Ozon.

Отдельный движок (WB-движок не трогаем): расчёт ведётся по базовому артикулу
товара (`base_article`), а модель цены — price-based: мы задаём ЦЕЛЕВУЮ
ВИТРИННУЮ цену напрямую (у Ozon нет разделения «базовая цена + скидка», как у
WB), оставляя old_price/min_price без изменений.

Применение — через OzonProvider.update_prices (POST /v1/product/import/prices).
Целевая цена базового артикула = витрина представителя (самый дорогой оффер).
В офферы базы изменение разносится с сохранением относительных различий:
каждый оффер умножается на тот же коэффициент (target/price − 1).

Данные Ozon отличаются от WB: нет воронки/просмотров/отзывов/акций, поэтому
тумблеры use_orders/use_reviews/use_quality/demand_raise/promo_enabled и
velocity_from_funnel для Ozon выключены, а «Возвраты/отмены» берутся из
детализации реализаций (ozon_detail_rows.return_qty).

Unit-экономика и метрики — из ozon_margin_detail_dataframe (та же, что на
вкладке «Детализация маржи OZON»), поэтому пол безубыточности (floor_price)
согласован с прибыльностью Ozon: комиссия — из детализации, логистика/услуги —
из начислений (ozon_accruals), хранение — из размещений (ozon_placements).
"""
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.providers import factory as provider_factory
from app.services import margin as margin_service
from app.services.cabinets import active_credentials, db_active_info, resolve_main, run_per_link
from app.services.pricing import (
    PRICING_DEFAULTS,
    _bias_shift,
    _canon_article,
    _cash_flow_bias,
    _clamp,
    _damp_raise_settings,
    _num,
    _reason_add,
    project_velocity,
)

# Настройки Ozon = настройки WB + Ozon-специфика. Факторы, опирающиеся на данные,
# которых у Ozon нет (воронка/заказы/отзывы/акции), по умолчанию выключены.
OZON_PRICING_DEFAULTS = {
    **PRICING_DEFAULTS,
    # минимальный шаг изменения цены: относительный (%, как min_delta_pp у WB)
    # и абсолютный (₽) — берём максимум из двух.
    "min_delta_pct": 1.0,
    "oz_price_step": 1.0,
    # предохранитель автодействий Ozon: не трогаем auto_action_enabled у товара.
    "ozon_auto_action_guard": True,
    "return_penalty": 0.3,
    # тумблеры, недоступные для Ozon (нет сигнала) — глушим по умолчанию.
    "use_orders": False,
    "use_reviews": False,
    "use_quality": False,
    "demand_raise": False,
    "velocity_from_funnel": False,
    "promo_enabled": False,
    # Влияние главной связки Ozon→Ozon (кросс-маркетплейс — отдельная задача).
    "follow_main": False,
}


def merge_settings(payload=None) -> dict:
    """Склеивает параметры из запроса с Ozon-дефолтами (только известные ключи).

    date_from/date_to — строки ISO (период анализа), пропускаются как есть.
    """
    out = dict(OZON_PRICING_DEFAULTS)
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


def _oz_id(db: Session) -> int:
    return db.execute(
        select(models.Marketplace.id).where(models.Marketplace.code == "ozon")
    ).scalar_one()


def _oz_provider(db=None):
    """Провайдер Ozon с creds активного кабинета (иначе из .env).

    См. `pricing._cab_provider`: при пустом contextvar (эндпоинт в другом треде
    threadpool, чем get_db) читаем связку из сессии, чтобы не уйти на .env.
    """
    creds = active_credentials("ozon")
    if not creds and db is not None:
        info = db_active_info(db)
        creds = info.creds_for("ozon") if info else {}
    return provider_factory.get_oz_provider(credentials=creds or None)


# ---------------------------------------------------------------------------
# Цены: снимок из БД / живой API
# ---------------------------------------------------------------------------
def load_ozon_prices_from_db(db: Session) -> tuple[Optional[pd.DataFrame], Optional[datetime]]:
    """Снимок цен Ozon из price_snapshots (по строке на оффер/размер).

    Возвращает (DataFrame, updated_at) с колонками offer_id/base_article/
    product_id/size/price/old_price. Пустой — если снимка нет.
    """
    rows = db.execute(
        select(
            models.PriceSnapshot.article,
            models.PriceSnapshot.base_article,
            models.PriceSnapshot.nm_id,
            models.PriceSnapshot.size,
            models.PriceSnapshot.price,
            models.PriceSnapshot.discount,
        ).where(models.PriceSnapshot.marketplace == "ozon")
    ).all()
    if not rows:
        return None, None
    updated_at = db.execute(
        select(func.max(models.PriceSnapshot.updated_at)).where(
            models.PriceSnapshot.marketplace == "ozon"
        )
    ).scalar()
    data = []
    for art, base, nm, size, price, discount in rows:
        price = _num(price)
        if not str(art or "").strip() or price <= 0:
            continue
        disc = _num(discount)
        old = round(price / (1 - disc / 100), 2) if 0 < disc < 100 else 0.0
        data.append({
            "offer_id": str(art or "").strip(),
            "base_article": str(base or "").strip(),
            "product_id": str(nm or "").strip(),
            "size": str(size or "").strip(),
            "price": price,
            "old_price": old,
        })
    if not data:
        return None, updated_at
    return pd.DataFrame(data), updated_at


def resolve_ozon_prices(
    db: Session, provider=None, allow_network: bool = True
) -> tuple[Optional[pd.DataFrame], Optional[datetime], str]:
    """Цены Ozon: снимок из БД, а если его нет — живой API (get_prices)."""
    df, updated_at = load_ozon_prices_from_db(db)
    if df is not None and not df.empty:
        return df, updated_at, "db"
    if not allow_network:
        return None, None, "db"
    prov = provider or _oz_provider(db)
    return prov.get_prices(), None, "ozon_api"


def _normalize_ozon_prices(prices_df: Optional[pd.DataFrame]) -> dict:
    """Список офферов Ozon → {offer_id: {...}}. Терпим к колонкам провайдера/БД."""
    out: dict = {}
    if prices_df is None or prices_df.empty:
        return out
    for row in prices_df.to_dict("records"):
        offer = str(row.get("offer_id", row.get("article", "")) or "").strip()
        if not offer:
            continue
        price = _num(row.get("price_price", row.get("price")))
        if price <= 0:
            continue
        old = _num(row.get("price_old_price", row.get("old_price")))
        out[offer] = {
            "offer_id": offer,
            "base": _canon_article(row.get("base_article", "")),
            "price": price,
            "old_price": old if old > 0 else None,
            "size": str(row.get("size", "") or "").strip(),
            "nm_id": str(row.get("product_id", row.get("nm_id", "")) or "").strip(),
        }
    return out


def _offer_base_map(db: Session) -> dict:
    """canon(offer_id) → canon(base_article) из снимка цен и детализации Ozon."""
    m: dict = {}
    for art, base in db.execute(
        select(models.PriceSnapshot.article, models.PriceSnapshot.base_article)
        .where(models.PriceSnapshot.marketplace == "ozon")
    ):
        a = _canon_article(art)
        if a:
            m[a] = _canon_article(base) or a
    for oid, base in db.execute(
        select(models.OzonDetailRow.offer_id, models.OzonDetailRow.base_article)
    ):
        a, b = _canon_article(oid), _canon_article(base)
        if a and b:
            m.setdefault(a, b)
    return m


def _group_offers(offers: dict, base_map: dict) -> dict:
    """{offer_id: {...}} → {base: {"offers": [...], "price": max, ...}}.

    price базового артикула = максимум цен его офферов (витрина представителя).
    """
    groups: dict = {}
    for offer, o in offers.items():
        base = o.get("base") or base_map.get(_canon_article(offer)) or _canon_article(offer)
        g = groups.setdefault(base, {"article": base, "offers": []})
        g["offers"].append(dict(o, offer_id=offer))
    for base, g in groups.items():
        g["offers"].sort(key=lambda x: x["price"])
        rep = max(g["offers"], key=lambda x: x["price"])
        g["price"] = rep["price"]
        g["old_price"] = rep.get("old_price")
        g["nm_id"] = rep.get("nm_id") or ""
        g["offers_count"] = len(g["offers"])
    return groups


# ---------------------------------------------------------------------------
# Метрики Ozon по базовому артикулу
# ---------------------------------------------------------------------------
def _detail_metrics_ozon(db: Session, date_from: date, date_to: date, base_map: dict) -> dict:
    """Скорость (текущее/предыдущее окно) и давность последней продажи по базе.

    Источник — ozon_detail_rows (та же детализация, что и маржа Ozon).
    """
    span = (date_to - date_from).days + 1
    prev_from = date_from - timedelta(days=span)
    rows = db.execute(
        select(
            models.OzonDetailRow.date,
            models.OzonDetailRow.offer_id,
            models.OzonDetailRow.base_article,
            models.OzonDetailRow.quantity,
            models.OzonDetailRow.return_qty,
        ).where(
            models.OzonDetailRow.date >= prev_from,
            models.OzonDetailRow.date <= date_to,
        )
    ).all()
    agg: dict = {}
    for dt, oid, base, qty, ret in rows:
        key = (_canon_article(base) or base_map.get(_canon_article(oid))
               or _canon_article(oid))
        if not key:
            continue
        net = int(qty or 0) - int(ret or 0)
        a = agg.setdefault(key, [0, 0, None])
        if dt is not None:
            if a[2] is None or dt > a[2]:
                a[2] = dt
            if dt >= date_from:
                a[0] += net
            else:
                a[1] += net
    out = {}
    for key, (cur, prev, last) in agg.items():
        out[key] = {"cur": max(cur, 0), "prev": max(prev, 0), "last": last}
    return out


def _unit_econ_from_margin(row: dict) -> Optional[dict]:
    """Удельная экономика Ozon из строки ozon_margin_detail_dataframe."""
    sells = _num(row.get("sells"))
    revenue = _num(row.get("revenue"))
    if sells <= 0 or revenue <= 0:
        return None
    return {
        "comm_rate": min(0.5, abs(_num(row.get("commission"))) / revenue),
        "logistics_unit": abs(_num(row.get("accrued_logistics"))) / sells,
        "storage_unit": abs(_num(row.get("storage"))) / sells,
        "services_unit": abs(_num(row.get("accrued_services"))) / sells,
        "other_unit": abs(_num(row.get("accrued_other"))) / sells,
        "net_cost": _num(row.get("net_cost")),
        "commission_per_one": _num(row.get("commission_per_one")),
        "services_per_one": _num(row.get("services_per_one")),
        "storage_per_one": _num(row.get("storage_per_one")),
        "income_per_one": _num(row.get("income_per_one")),
        "revenue_per_one": _num(row.get("revenue_per_one")),
        "margin_pct": _num(row.get("margin_pct")),
        "return_rate": _num(row.get("return_rate")),
    }


def _floor_price(f: dict, s: dict) -> Optional[float]:
    comm = f.get("comm_rate")
    if comm is None:
        return None
    denom = 1 - comm - _num(s["floor_margin_pct"]) / 100
    if denom <= 0:
        return None
    costs = (_num(f.get("logistics_unit")) + _num(f.get("storage_unit"))
             + _num(f.get("services_unit")) + _num(f.get("other_unit"))
             + _num(f.get("net_cost")))
    return round(costs / denom, 2)


def _latest_ozon_stock(db: Session, base_map: dict) -> dict:
    """Остаток Ozon на складе (on-hand) по базовому артикулу (последний срез stocks)."""
    mp = _oz_id(db)
    latest = db.scalar(
        select(func.max(models.Stock.date)).where(models.Stock.marketplace_id == mp)
    )
    if latest is None:
        return {}
    out: dict = {}
    for art, base, qty in db.execute(
        select(models.Stock.article, models.Stock.base_article, func.sum(models.Stock.quantity))
        .where(models.Stock.marketplace_id == mp, models.Stock.date == latest)
        .group_by(models.Stock.article, models.Stock.base_article)
    ):
        key = (_canon_article(base) or base_map.get(_canon_article(art))
               or _canon_article(art))
        if key:
            out[key] = out.get(key, 0) + int(qty or 0)
    return out


def _latest_ozon_in_way(db: Session, base_map: dict) -> dict:
    """«В пути» Ozon по базовому артикулу (последний срез stocks, promised_amount)."""
    mp = _oz_id(db)
    latest = db.scalar(
        select(func.max(models.Stock.date)).where(models.Stock.marketplace_id == mp)
    )
    if latest is None:
        return {}
    out: dict = {}
    for art, base, qty in db.execute(
        select(models.Stock.article, models.Stock.base_article, func.sum(models.Stock.in_way))
        .where(models.Stock.marketplace_id == mp, models.Stock.date == latest)
        .group_by(models.Stock.article, models.Stock.base_article)
    ):
        key = (_canon_article(base) or base_map.get(_canon_article(art))
               or _canon_article(art))
        if key:
            out[key] = out.get(key, 0) + int(qty or 0)
    return out


def _product_map(db: Session) -> dict:
    return {_canon_article(p.article): p for p in db.execute(select(models.Product)).scalars()}


# ---------------------------------------------------------------------------
# Решение (price-based порт правил R1-R10)
# ---------------------------------------------------------------------------
def soften_lower_ozon(rows: list, s: dict) -> int:
    """R18: снижение цены Ozon — в lower_soften раз мягче; повышения не трогаем.

    Дельта LOWER (price − target_price) делится на lower_soften, но никогда не
    ужимается ниже шага max(min_delta_pct %, oz_price_step ₽) — минимальный шаг
    (правило «коррекция обязательна») и защита от флапа целы. Вызывается после
    решений, до влияния главной связки. Возвращает число смягчённых строк.
    """
    soften = _num(s.get("lower_soften"), 1.0)
    if soften <= 1.0:
        return 0
    step_pct = _num(s.get("min_delta_pct"), 1.0)
    step_rub = _num(s.get("oz_price_step"), 1.0)
    n = 0
    for r in rows:
        if r.get("action") != "LOWER":
            continue
        price = _num(r.get("price"))
        tgt = _num(r.get("target_price"))
        floor = _num(r.get("floor_price"))
        if price <= 0 or tgt >= price:
            continue
        min_step = max(step_pct / 100.0 * price, step_rub)
        if price - tgt <= min_step + 1e-9:
            continue  # минимальный шаг не смягчаем
        # Смягчённый шаг, но не меньше обязательного минимума и не ниже пола.
        new_t = min(price - (price - tgt) / soften, price - min_step)
        new_t = max(new_t, floor, 1.0)
        new_t = round(new_t, 2)
        if abs(new_t - tgt) < 1e-9:
            continue
        r["target_price"] = new_t
        r["delta_price"] = round(new_t - price, 2)
        r["delta_pct"] = round((new_t - price) / price * 100, 2)
        r["margin_pct_at_target"] = _margin_pct_oz(new_t, {
            "comm_rate": r.get("comm_rate"),
            "logistics_unit": r.get("logistics_unit"),
            "storage_unit": r.get("storage_unit"),
            "services_unit": r.get("services_unit"),
            "other_unit": r.get("other_unit"),
            "net_cost": r.get("net_cost"),
        })
        n += 1
    return n


def _margin_pct_oz(target: float, f: dict) -> Optional[float]:
    comm = f.get("comm_rate")
    if comm is None or target <= 0:
        return None
    unit = (target * (1 - comm) - _num(f.get("logistics_unit"))
            - _num(f.get("storage_unit")) - _num(f.get("services_unit"))
            - _num(f.get("other_unit")) - _num(f.get("net_cost")))
    return round(unit / target * 100, 1)


def _base_row_ozon(f: dict) -> dict:
    return {
        "article": f["article"], "name": f.get("name", ""), "nm_id": f.get("nm_id", ""),
        "offers_count": f.get("offers_count", 1), "offers": f.get("offers", []),
        "price": f["price"], "old_price": f.get("old_price"),
        "ozon_discount_pct": f.get("ozon_discount_pct"),
        "current_vis": f["price"], "current_price": f["price"],
        "floor_price": f.get("floor_price"), "net_cost": f.get("net_cost"),
        "value": f.get("value"), "supplier": f.get("supplier", ""),
        "comm_rate": f.get("comm_rate"),
        "logistics_unit": f.get("logistics_unit"), "storage_unit": f.get("storage_unit"),
        "services_unit": f.get("services_unit"), "other_unit": f.get("other_unit"),
        "commission_per_one": f.get("commission_per_one"),
        "services_per_one": f.get("services_per_one"),
        "storage_per_one": f.get("storage_per_one"),
        "return_rate": f.get("return_rate", 0.0),
        "stock": f.get("stock"), "in_way": f.get("in_way"),
        "velocity": f.get("velocity", 0.0),
        "v_proj": f.get("v_proj", 0.0), "trend": f.get("trend", 1.0),
        "last_sale_days_ago": f.get("last_sale_days_ago"),
        "doc": None, "replenishable": bool(f.get("replenishable")),
        "stock_value": f.get("stock_value", 0.0),
        "action": "HOLD", "status": "hold", "reason": "",
        "target_price": f["price"], "delta_price": 0.0, "delta_pct": 0.0,
        "margin_pct": f.get("margin_pct"),
        "margin_pct_at_target": _margin_pct_oz(f["price"], f),
    }


def _apply_target(base: dict, f: dict, target: float, action: str, reason: str) -> dict:
    target = max(round(target, 2), 1.0)
    cur = f["price"]
    base.update(
        action=action, status="suggested", reason=reason,
        target_price=target,
        delta_price=round(target - cur, 2),
        delta_pct=round((target - cur) / cur * 100, 2) if cur > 0 else 0.0,
        margin_pct_at_target=_margin_pct_oz(target, f),
    )
    return base


def _mandatory_step_ozon(base: dict, f: dict, s: dict, direction: str, reason: str) -> dict:
    """Есть остаток — изменение цены обязательно: минимальный шаг в направлении.

    Шаг = max(min_delta_pct%, oz_price_step ₽). Для LOWER уважаем floor_price.
    Если шаг в заданном направлении невозможен — пробуем противоположный.
    """
    price = f["price"]
    if price <= 0:
        return base
    floor = _num(f.get("floor_price"))
    step_pct = max(_num(s["min_delta_pct"], 1.0), 1.0)
    step_rub = max(_num(s["oz_price_step"], 1.0), 1.0)
    if price > 0:
        step_pct = max(step_pct, step_rub / price * 100)
    suffix = f" — коррекция цены обязательна (остаток {f.get('stock')} шт)"

    def try_lower():
        t = max(price * (1 - step_pct / 100), floor)
        if t < price - 1e-9 and (price - t) >= step_rub - 1e-9:
            return t
        return None

    def try_raise():
        t = price * (1 + step_pct / 100)
        if t > price + 1e-9 and (t - price) >= step_rub - 1e-9:
            return t
        return None

    if direction == "RAISE":
        t = try_raise()
        if t is not None:
            return _apply_target(base, f, t, "RAISE", reason + suffix)
        t = try_lower()
        if t is not None:
            return _apply_target(base, f, t, "LOWER", reason + suffix)
        return base
    t = try_lower()
    if t is not None:
        return _apply_target(base, f, t, "LOWER", reason + suffix)
    t = try_raise()
    if t is not None:
        return _apply_target(base, f, t, "RAISE", reason + suffix)
    return base


def _decide_ozon(f: dict, s: dict) -> dict:
    price = f["price"]
    stock = f["stock"]
    v_proj = f["v_proj"]
    floor = f.get("floor_price")
    base = _base_row_ozon(f)

    use_inv = bool(s.get("use_inventory", True))
    use_sales = bool(s.get("use_sales", True))
    use_margin = bool(s.get("use_margin", True))
    use_repl = bool(s.get("use_replenishable", True))
    use_season = bool(s.get("use_season", True))
    use_returns = bool(s.get("use_returns", False))

    if use_inv and stock is None:
        base.update(action="SKIP", status="skipped_no_stock",
                    reason="нет данных об остатках (вытяните Ozon Остатки)")
        return base
    if use_inv and stock <= 0:
        base.update(action="HOLD", status="hold", reason="распродан (остаток 0)")
        return base

    if use_margin:
        if f.get("comm_rate") is None or floor is None:
            net_cost = _num(f.get("net_cost"))
            if bool(s.get("no_econ_cost_floor", True)) and net_cost > 0 and price > 0:
                floor = net_cost * (1 + _num(s["floor_margin_pct"]) / 100)
                f["floor_price"] = floor
            else:
                base.update(action="SKIP", status="skipped_no_ratio",
                            reason="нет unit-экономики (нет продаж/начислений — "
                                   "не можем гарантировать break-even)")
                return base
    base["floor_price"] = f.get("floor_price")

    doc = stock / v_proj if (v_proj > 0 and stock is not None) else float("inf")
    base["doc"] = round(doc, 1) if doc != float("inf") else None

    if use_returns and _num(f.get("return_rate")) > _num(s["return_penalty"]) * 100:
        base.update(action="SKIP", status="skipped_returns",
                    reason=f"высокая доля возвратов ({f['return_rate']:.0f}%) — "
                           "спрос мыльный, не трогаем")
        return base

    # R4: мёртвый запас (нет продаж за окно)
    if use_sales and v_proj <= 0:
        if _num(f.get("last_sale_days_ago"), 999) >= int(s["dead_stock_days"]):
            return _mandatory_step_ozon(
                base, f, s, "LOWER",
                f"мёртвый запас: продаж нет {f.get('last_sale_days_ago')} дн., "
                f"остаток {stock} шт")
        return _mandatory_step_ozon(base, f, s, "LOWER", "продаж нет (менее порога)")

    if not use_sales:
        return _mandatory_step_ozon(
            base, f, s, "LOWER" if doc >= _num(s["target_doc"]) else "RAISE",
            "продажи не влияют (фактор выключен)")

    # Режим кассы (cash_flow_bias) + противовес автоскидкам: сдвиг зон DOC.
    bias = _bias_shift(s)
    s = _damp_raise_settings(s)
    doc_low = _num(s["doc_low"]) * (1 + bias)
    doc_high = _num(s["doc_high"]) * (1 + bias)
    max_raise = _num(s["max_raise_pct"])
    max_drop = _num(s["max_drop_pct"]) * (1 - bias)
    repl = use_repl and bool(f.get("replenishable"))

    # дефицит: у Ozon нет воронки (hot_demand всегда False), поэтому либо
    # «не докупается — последние единицы» (RAISE на max_raise), либо обязательный шаг.
    if doc < doc_low:
        if not repl:
            target = price * (1 + max_raise / 100)
            if (target - price) / price * 100 >= _num(s["min_delta_pct"]):
                return _apply_target(
                    base, f, target, "RAISE",
                    f"дефицит (DOC={doc:.0f} дн.), товар не докупается — последние единицы")
            return _mandatory_step_ozon(
                base, f, s, "RAISE", f"дефицит (DOC={doc:.0f} дн.), товар не докупается")
        return _mandatory_step_ozon(
            base, f, s, "RAISE",
            f"дефицит (DOC={doc:.0f} дн., товар докупаемый) — темп важнее")

    if doc >= doc_high:
        k = 0.5
        if use_season and _num(f.get("trend"), 1.0) < 1.0:
            k *= 0.8
        elif use_season and _num(f.get("trend"), 1.0) > 1.0:
            k *= 1.2
        k = _clamp(k, 0.2, 1.2)
        factor = max(1 - max_drop / 100, (_num(s["target_doc"]) / doc) ** k)
        target = min(max(_num(floor), price * factor), price)
        if (price - target) / price * 100 >= _num(s["min_delta_pct"]):
            note = ""
            if use_season and bool(s["season_adj"]) and _num(f.get("trend"), 1.0) != 1.0:
                note = " · тренд " + ("растёт ↓" if _num(f.get("trend"), 1.0) > 1 else "падает ↑")
            return _apply_target(
                base, f, target, "LOWER",
                f"перезапас (DOC={doc:.0f} дн.), целевые {_num(s['target_doc']):.0f} дн." + note)
        return _mandatory_step_ozon(
            base, f, s, "LOWER",
            f"перезапас (DOC={doc:.0f} дн.), целевые {_num(s['target_doc']):.0f} дн.")

    return _mandatory_step_ozon(
        base, f, s,
        "LOWER" if doc >= _num(s["target_doc"]) else "RAISE",
        f"нормальные остатки (DOC={doc:.0f} дн.), целевые {_num(s['target_doc']):.0f} дн.")


# ---------------------------------------------------------------------------
# Влияние главной связки Ozon→Ozon (кросс-маркетплейс — отдельная задача)
# ---------------------------------------------------------------------------
def build_main_map_ozon(db: Session, settings=None, only_articles: Optional[set] = None) -> dict:
    """canon(base_article) → Δ% витринной цены главной связки Ozon (только RAISE).

    Работает, только если главная связка — Ozon: кросс-маркетплейс нормализация
    артикулов (WB↔Ozon) в этот проход не входит.
    """
    main = resolve_main(db)
    if main is None or main.marketplace != "ozon":
        return {}
    out: dict = {}
    only = {_canon_article(a) for a in only_articles} if only_articles else None

    def _fn(db2, info):  # noqa: ARG001
        try:
            rec = recommendations_ozon(db2, settings, only_bases=only)
        except Exception:  # noqa: BLE001 — влияние не критично для расчёта
            return
        for r in rec.get("rows") or []:
            if r.get("action") != "RAISE":
                continue
            cur = _num(r.get("price"))
            tgt = _num(r.get("target_price"))
            if cur > 0 and tgt > cur:
                art = _canon_article(r.get("article"))
                if art:
                    out[art] = (tgt - cur) / cur * 100.0

    run_per_link(db, [main], _fn)
    return out


def apply_main_influence_ozon(rows: list, main_map: dict, s: dict) -> int:
    """Поднимает локальную цену на половину относительного роста главной (RAISE).

    Только в сторону повышения, не против локального спроса: изменяем лишь те
    строки, что и так HOLD/suggested-шаг, и не поднимаем SKIP/HOLD-распродажу.
    """
    if not main_map or not s.get("follow_main"):
        return 0
    pct = _num(s.get("main_influence_pct"), 50.0) / 100.0
    n = 0
    for r in rows:
        art = _canon_article(r.get("article"))
        delta = main_map.get(art)
        if not delta:
            continue
        if r.get("status") == "skipped_no_stock" or r.get("action") == "SKIP":
            continue
        cur = _num(r.get("price"))
        if cur <= 0:
            continue
        local_delta = delta * pct
        new_target = cur * (1 + local_delta / 100)
        old_target = _num(r.get("target_price"))
        if new_target <= max(old_target, cur):
            continue
        r["action"] = "RAISE"
        r["status"] = "suggested"
        r["influenced_by_main"] = True
        r["main_delta_pct"] = round(local_delta, 2)
        r["target_price"] = round(new_target, 2)
        r["delta_price"] = round(new_target - cur, 2)
        r["delta_pct"] = round(local_delta, 2)
        r["margin_pct_at_target"] = _margin_pct_oz(new_target, {
            "comm_rate": r.get("comm_rate"),
            "logistics_unit": r.get("logistics_unit"),
            "storage_unit": r.get("storage_unit"),
            "services_unit": r.get("services_unit"),
            "other_unit": r.get("other_unit"),
            "net_cost": r.get("net_cost"),
        })
        _reason_add(r, f"подстройка под главную связку (+{local_delta:.1f}%)")
        n += 1
    return n


# ---------------------------------------------------------------------------
# Рекомендации
# ---------------------------------------------------------------------------
def recommendations_ozon(
    db: Session,
    settings=None,
    provider=None,
    prices_df: Optional[pd.DataFrame] = None,
    today: Optional[date] = None,
    main_map: Optional[dict] = None,
    only_bases: Optional[set] = None,
) -> dict:
    """Read-only рекомендации автопилота Ozon по базовым артикулам."""
    s = merge_settings(settings)
    today = today or date.today()

    if s.get("date_from") and s.get("date_to"):
        date_from = date.fromisoformat(s["date_from"])
        date_to = date.fromisoformat(s["date_to"])
    else:
        window = int(_num(s["window_days"], 14) or 14)
        date_to = today
        date_from = today - timedelta(days=window)

    updated_at, source = None, "provided"
    if prices_df is None:
        prices_df, updated_at, source = resolve_ozon_prices(db, provider=provider)
    offers = _normalize_ozon_prices(prices_df)
    base_map = _offer_base_map(db)
    groups = _group_offers(offers, base_map)

    mdf = margin_service.ozon_margin_detail_dataframe(
        db, date_from, date_to, by_size=False
    )
    mrows = ({_canon_article(r.get("article")): r for r in mdf.to_dict("records")}
             if mdf is not None and not mdf.empty else {})

    detail = _detail_metrics_ozon(db, date_from, date_to, base_map)
    stock = _latest_ozon_stock(db, base_map)
    in_way = _latest_ozon_in_way(db, base_map)
    prods = _product_map(db)
    span = max((date_to - date_from).days + 1, 1)

    only = {_canon_article(a) for a in only_bases} if only_bases else None

    rows = []
    for base, g in groups.items():
        if only is not None and base not in only:
            continue
        price = g["price"]
        mrow = mrows.get(base) or {}
        drow = detail.get(base) or {}
        prod = prods.get(base)
        net_cost = _num(mrow.get("net_cost")) or (_num(prod.net_cost) if prod else 0.0)
        v_now = drow.get("cur", 0) / span
        v_prev = drow.get("prev", 0) / span
        trend = (v_now / v_prev) if v_prev > 0 else 1.0
        trend = _clamp(trend, 0.5, 2.0)
        v_proj = project_velocity(v_now, v_prev, bool(s.get("season_adj")),
                                  _num(s["season_damp"], 0.5))
        last = drow.get("last")
        last_days = (today - last).days if last else 999
        st = stock.get(base)
        iw = in_way.get(base)
        f = {
            "article": base, "name": (prod.name if prod else "") or str(mrow.get("name") or ""),
            "nm_id": g.get("nm_id", ""), "offers": g["offers"],
            "offers_count": g.get("offers_count", len(g["offers"])),
            "price": price, "old_price": g.get("old_price"),
            "ozon_discount_pct": (round((g["old_price"] - price) / g["old_price"] * 100, 1)
                                  if g.get("old_price") else 0.0),
            "stock": st, "in_way": iw,
            "velocity": round(v_now, 2), "v_proj": v_proj, "trend": round(trend, 2),
            "last_sale_days_ago": last_days,
            "net_cost": net_cost,
            "value": _num(getattr(prod, "value", 0)) if prod else 0.0,
            "supplier": getattr(prod, "supplier", "") if prod else "",
            "replenishable": bool(getattr(prod, "replenishable", False)) if prod else False,
            "margin_pct": _num(mrow.get("margin_pct")),
        }
        ue = _unit_econ_from_margin(mrow)
        if ue:
            f.update(ue)
            f["net_cost"] = net_cost or ue.get("net_cost", 0.0)
        else:
            f.update({"comm_rate": None, "logistics_unit": 0.0, "storage_unit": 0.0,
                      "services_unit": 0.0, "other_unit": 0.0})
        f["return_rate"] = _num(mrow.get("return_rate"))
        f["floor_price"] = _floor_price(f, s)
        f["stock_value"] = round((st or 0) * net_cost, 2)
        rows.append(_decide_ozon(f, s))

    # Режим кассы: срочность ослабляет мягкость снижения, «деньги есть» — усиливает.
    _urgency = _cash_flow_bias(s)
    if _urgency > 0:
        s["lower_soften"] = max(1.0, _num(s.get("lower_soften"), 1.0) / (1 + _urgency))
    elif _urgency < 0:
        s["lower_soften"] = min(8.0, _num(s.get("lower_soften"), 1.0) * (1 - 0.5 * _urgency))

    # R18: мягкость снижения — LOWER делим на lower_soften, RAISE не трогаем.
    soften_lower_ozon(rows, s)

    main_map = main_map if main_map is not None else (
        build_main_map_ozon(db, s) if s.get("follow_main") else {})
    if main_map:
        apply_main_influence_ozon(rows, main_map, s)

    rows.sort(key=lambda r: (-_num(r.get("stock_value")), r["article"]))
    return {
        "rows": rows, "count": len(rows),
        "date_from": str(date_from), "date_to": str(date_to),
        "prices_source": source,
        "prices_updated_at": updated_at.isoformat() if updated_at else None,
        "main_influence": {"count": len(main_map or {})},
    }


# ---------------------------------------------------------------------------
# Применение
# ---------------------------------------------------------------------------
def _expand_offers(r: dict) -> list:
    """Офферы строки для import/prices: каждый оффер × (target/price − 1)."""
    offers = r.get("offers") or []
    price, target = _num(r.get("price")), _num(r.get("target_price"))
    if not offers or price <= 0 or target <= 0:
        return []
    delta = target / price - 1
    pushed = []
    for o in offers:
        op = _num(o.get("price"))
        offer_id = str(o.get("offer_id") or "").strip()
        if not offer_id or op <= 0:
            continue
        pushed.append({"offer_id": offer_id, "price": max(1.0, round(op * (1 + delta), 2))})
    return pushed


def _recent_applied(db: Session, cooldown_days: int) -> set:
    cooldown_from = date.today() - timedelta(days=int(cooldown_days))
    return set(db.scalars(
        select(models.PriceChange.article).where(
            models.PriceChange.status == "applied",
            models.PriceChange.action.in_(("RAISE", "LOWER", "HALVE")),
            models.PriceChange.applied_at >= cooldown_from,
        )
    ).all())


def _pushed_items_ozon(rows: list, s: dict, applied_past: set) -> list:
    """Строки → плоский список офферов для import/prices (со сдвигом по базе).

    Для каждой строки считаем коэффициент delta = target/price − 1 и применяем
    его к каждому офферу (сохраняем относительные различия размеров).
    """
    items: list = []
    for r in rows:
        if r["action"] not in ("RAISE", "LOWER"):
            continue
        if r["article"] in applied_past:
            r["status"] = "skipped_cooldown"
            _reason_add(r, f"кулдаун {int(s['cooldown_days'])} дн. после предыдущего изменения")
            continue
        pushed = _expand_offers(r)
        if not pushed:
            r["status"] = "skipped_no_offer"
            _reason_add(r, "нет валидных офферов Ozon для отправки")
            continue
        r["_pushed_offers"] = pushed
        items.extend(pushed)
    return items


def _record_ozon(db: Session, rows: list, applied_at: Optional[datetime],
                 error: Optional[str] = None):
    """Журнал решений Ozon в общую таблицу price_changes.

    before_discount/after_discount хранят цену до/после (не скидку) — у Ozon
    модель price-based.
    """
    for r in rows:
        is_applied = (r["action"] in ("RAISE", "LOWER", "MIN")
                      and r.get("status") == "applied" and error is None)
        db.add(models.PriceChange(
            article=r["article"], nm_id=str(r.get("nm_id") or ""),
            calculated_at=datetime.now(),
            applied_at=applied_at if is_applied else None,
            before_discount=float(r.get("price") or 0),
            after_discount=float(r.get("target_price")) if is_applied else None,
            action=r.get("action") or "",
            status=(r.get("status") or "") if not error else "error",
            reason=str(error) if error else (r.get("reason", "") or ""),
        ))
    db.commit()


def apply_recommendations_ozon(
    db: Session, settings=None, prices_df=None, provider=None,
    today: Optional[date] = None, main_map: Optional[dict] = None,
) -> dict:
    """Расчёт + применение цен Ozon через OzonProvider.update_prices + журнал."""
    s = merge_settings(settings)
    rec_settings = merge_settings(s)
    rec_settings["show_zero"] = True
    rec = recommendations_ozon(db, rec_settings, provider=provider,
                               prices_df=prices_df, today=today, main_map=main_map)
    rows = rec["rows"]
    now = datetime.now()
    applied_past = _recent_applied(db, int(s["cooldown_days"]))
    items = _pushed_items_ozon(rows, s, applied_past)
    prov = provider or _oz_provider(db)

    if not items:
        _record_ozon(db, rows, applied_at=now)
        return {"applied": [], "rows": rows, "pushed": 0,
                "note": "Нет кандидатов на изменение (все hold/skip или в кулдауне)."}

    try:
        result = prov.update_prices(items)
    except Exception as exc:  # noqa: BLE001
        _record_ozon(db, rows, applied_at=now, error=str(exc))
        raise

    for r in rows:
        if r["action"] in ("RAISE", "LOWER") and r["status"] == "suggested":
            r["status"] = "applied"
    _record_ozon(db, rows, applied_at=now)
    applied_ids = [r["article"] for r in rows if r["status"] == "applied"]
    return {"applied": applied_ids, "rows": rows, "pushed": len(items),
            "updated": result.get("updated") if isinstance(result, dict) else None,
            "note": f"Применено изменений: {len(applied_ids)} (офферов: {len(items)})."}


def apply_rows_ozon(
    db: Session, rows: list, settings=None, provider=None,
    today: Optional[date] = None,
) -> dict:
    """Применяет цены ровно для переданных строк (видимых в таблице)."""
    s = merge_settings(settings or {})
    rows = [dict(r) for r in rows]
    now = datetime.now()
    applied_past = _recent_applied(db, int(s["cooldown_days"]))
    items = _pushed_items_ozon(rows, s, applied_past)
    prov = provider or _oz_provider(db)
    if not items:
        _record_ozon(db, rows, applied_at=now)
        return {"applied": [], "rows": rows, "pushed": 0,
                "note": "Нет кандидатов на изменение (все hold/skip или в кулдауне)."}
    try:
        result = prov.update_prices(items)
    except Exception as exc:  # noqa: BLE001
        _record_ozon(db, rows, applied_at=now, error=str(exc))
        raise
    for r in rows:
        if r["action"] in ("RAISE", "LOWER") and r["status"] == "suggested":
            r["status"] = "applied"
    _record_ozon(db, rows, applied_at=now)
    applied_ids = [r["article"] for r in rows if r["status"] == "applied"]
    return {"applied": applied_ids, "rows": rows, "pushed": len(items),
            "updated": result.get("updated") if isinstance(result, dict) else None,
            "note": f"Применено изменений: {len(applied_ids)} (офферов: {len(items)})."}


def apply_min_prices_ozon(
    db: Session, rows: Optional[list] = None, settings=None,
    provider=None, today: Optional[date] = None,
) -> dict:
    """Минимальные (break-even) цены Ozon: где витрина ниже пола — поднимаем до пола.

    Офферы поднимаются пропорционально (target/price − 1), чтобы сохранить
    относительные различия размеров. Журнал — action=MIN (вне кулдауна).
    """
    s = merge_settings(settings or {})
    if rows is None:
        rec_settings = merge_settings(s)
        rec_settings["show_zero"] = True
        prices_df, _u, _src = resolve_ozon_prices(db, provider=provider)
        rows = recommendations_ozon(
            db, rec_settings, provider=provider, prices_df=prices_df, today=today,
        ).get("rows") or []
    rows = [dict(r) for r in rows]

    targets, no_offer = [], 0
    for r in rows:
        floor = _num(r.get("floor_price"))
        cur = _num(r.get("price"))
        if floor <= 0 or cur <= 0 or cur >= floor - 1e-9:
            continue
        if not (r.get("offers") or []):
            no_offer += 1
            continue
        r["target_price"] = round(max(1.0, floor), 2)
        targets.append(r)

    if not targets:
        return {"applied": [], "pushed": 0, "skipped": no_offer,
                "note": "Нет товаров с ценой ниже безубыточного минимума."}

    prov = provider or _oz_provider(db)
    items = []
    for r in targets:
        pushed = _expand_offers(r)
        if pushed:
            r["_pushed_offers"] = pushed
            items.extend(pushed)
    if not items:
        return {"applied": [], "pushed": 0, "skipped": no_offer,
                "note": "Нет офферов для отправки минимальных цен."}
    now = datetime.now()
    try:
        result = prov.update_prices(items)
    except Exception as exc:  # noqa: BLE001
        for r in targets:
            r["action"] = "MIN"
            r["status"] = "error"
            _reason_add(r, f"минимальная цена: {exc}")
        _record_ozon(db, targets, applied_at=None, error=str(exc))
        raise

    for r in targets:
        r["action"] = "MIN"
        r["status"] = "applied"
        _reason_add(r, f"минимальная цена (безубыток): {_num(r['target_price']):.0f} ₽ "
                       f"(пол {_num(r.get('floor_price')):.0f} ₽)")
    _record_ozon(db, targets, applied_at=now)
    applied_ids = [r["article"] for r in targets]
    note = f"Минимальные цены отправлено: {len(items)} офферов ({len(targets)} товаров)."
    if no_offer:
        note += f" Пропущено без офферов: {no_offer}."
    return {"applied": applied_ids, "pushed": len(items), "skipped": no_offer,
            "updated": result.get("updated") if isinstance(result, dict) else None,
            "note": note}
