"""Автопилот скидок Wildberries.

По каждому товару собираются сигналы (скорость продаж и тренд/сезонность,
остатки/DOC, unit-экономика из истории продаж, воронка продаж, текущая цена
и скидка WB, докупаемость) и по правилам R1-R10 формируется целевая скидка.
Применение — через WbProvider.update_prices (POST upload/task), меняется только
скидка (базовая цена price не трогается).

Цена никогда не опускается ниже floor_price (break-even по unit-экономике).
"""
import json
import math
import re
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import models
from app.providers import factory as provider_factory
from app.services.cabinets import active_credentials, run_per_link


def _cab_provider():
    """Провайдер WB с creds активного кабинета (иначе из .env)."""
    creds = active_credentials("wb")
    return provider_factory.get_wb_provider(credentials=creds or None)

PRICING_DEFAULTS = {
    "window_days": 14,
    "target_doc": 30,
    "doc_low": 14,
    "doc_high": 60,
    "floor_margin_pct": 10.0,
    "max_discount_pct": 50.0,
    "max_raise_pct": 15.0,
    "max_drop_pct": 15.0,
    # R18: мягкость снижения цены. Во сколько раз делить расчётную дельту
    # LOWER (1 — как раньше, 4 — снижение в 4 раза мягче). Повышения цены
    # от этого параметра не зависят; минимальный шаг (min_delta_pp) не
    # смягчается — правило «коррекция обязательна» и защита от флапа целы.
    "lower_soften": 4.0,
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
    # Ценность товара (value из каталога) относительно себестоимости: чем больше
    # value превышает net_cost (value/net_cost), тем ценнее товар — тем меньше
    # скидка при LOWER. Порог value_ratio_low = при значении ≤ себестоимости
    # ограничений нет (1.0), value_ratio_high = при таком отношении и выше скидку
    # не увеличиваем вовсе (0.0); между порогами — линейно.
    "value_ratio_low": 1.0,
    "value_ratio_high": 4.0,
    # T-14: качественные гейты для RAISE. Рейтинг здесь — «Рейтинг по отзывам»
    # из воронки продаж (1..5): низкий рейтинг не даёт поднимать цену.
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
    # T-31: акции WB (Календарь акций). promo_enabled = «учитывать акции» в
    # автопилоте: (1) при недоходе до следующего ranging-тира лучшей акции
    # «добираем» K товаров (наименее рентабельные → больший остаток → медленнее
    # оборот → объёмные), поднимая целевую скидку на promo_push_pct; вниз хуже
    # безубытка разрешено до promo_max_beyond_floor_pp п.п. (акция важнее пола
    # цены). (2) T-41 (R19): не уходить ниже скидки, рекомендованной акцией WB
    # (planDiscount из nomenclatures; только regular-акции) — финальный потолок.
    "promo_enabled": False,
    "promo_push_pct": 2.0,
    "promo_max_beyond_floor_pp": 5.0,
    # T-32 (R12): подтверждённый спрос поднимает цену вопреки зоне DOC.
    # Сигналы спроса (выкупы/корзина/рейтинг/конверсия) при наборе
    # demand_min_signals из 4 перекрывают «обычный» шаг по остатку и дают
    # RAISE на demand_raise_pct %, но только пока DOC ≤ demand_raise_max_doc
    # (в глубоком перезапасе не поднимаем — там товар не распродаётся).
    "demand_raise": True,
    "demand_min_buyouts": 3,
    "demand_min_backlog": 5,
    "demand_min_rating": 4.5,
    "demand_min_conv_pct": 1.0,
    "demand_min_signals": 2,
    "demand_raise_pct": 15.0,
    "demand_raise_max_doc": 120.0,
    # T-32 (R13): скорость продаж из воронки, если по строке Sales за окно
    # пусто (нет выручки), но в воронке есть активность. Считаем v_proj =
    # funnel_velocity_field / дней среза воронки (buyouts по умолчанию).
    "velocity_from_funnel": True,
    "funnel_velocity_field": "buyouts",
    # T-32 (R14): нет unit-экономики, но есть остаток и себестоимость.
    # Включено — пол цены = net_cost×(1+floor_margin_pct/100), решение
    # (мёртвый/перезапас → LOWER) принимается по нему вместо SKIP.
    # Выключено — прежний SKIP skipped_no_ratio. net_cost<=0 → SKIP всегда.
    "no_econ_cost_floor": True,
    # T-32 (R15): ценность остатка (qty × net_cost) — колонка и вес при
    # приоритизации (100×100 ₽ ≠ 1000×100 ₽).
    "stock_value_weight": True,
    # T-33 (R16): межсвязочное влияние. Если у пользователя выбрана «главная
    # связка» (users.main_cabinet_id), остальные связки подстраиваются под её
    # свежую рекомендацию: при RAISE главной локальная цена поднимается как
    # минимум на половину (main_influence_pct) от относительного изменения
    # витринной цены главной, но не против локального спроса. Влияют только
    # повышения главной (её снижения игнорируются). follow_main=0 — выключить.
    "follow_main": True,
    "main_influence_pct": 50.0,
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


def load_prices_from_db(
    db: Session, marketplace: str = "wb"
) -> tuple[Optional[pd.DataFrame], Optional[datetime]]:
    """Цены из локального снимка price_snapshots вместо живого API WB.

    Зачем: `WbProvider.get_prices()` тянет ~9 страниц discounts-prices-api
    (лимит 5 запросов/мин, ~8 с на ответ) — 80-145 с на каждый пересчёт, при том
    что CPU приложения в это время простаивает (замер: 0.1 ядра из 24). Те же
    значения уже лежат в БД: refresh.pull_wb_prices -> sync.upsert_price_snapshots.

    В снимке по строке на РАЗМЕР (83 608 строк на 8361 карточку), а расчёт
    оперирует nmID, поэтому размеры сворачиваем в одну цену на карточку.
    Сверено с живым API на 8361 карточке: цены совпали на 100 %, скидки на 99.6 %.

    Возвращает (DataFrame в формате get_prices(), updated_at снимка).
    DataFrame пустой, если снимка нет — вызывающий решает, идти ли в сеть.
    """
    rows = db.execute(
        select(
            models.PriceSnapshot.nm_id,
            models.PriceSnapshot.article,
            models.PriceSnapshot.price,
            models.PriceSnapshot.discounted_price,
            models.PriceSnapshot.discount,
        ).where(models.PriceSnapshot.marketplace == marketplace)
    ).all()
    if not rows:
        return None, None
    updated_at = db.execute(
        select(func.max(models.PriceSnapshot.updated_at)).where(
            models.PriceSnapshot.marketplace == marketplace
        )
    ).scalar()

    df = pd.DataFrame(
        rows, columns=["nm_id", "vendorCode", "price", "discountedPrice", "discount"]
    )
    for col in ("nm_id", "vendorCode"):
        df[col] = df[col].fillna("").astype(str).str.strip()
    for col in ("price", "discountedPrice", "discount"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
    df = df[df["nm_id"] != ""]
    if df.empty:
        return None, updated_at
    # По строке на размер — сворачиваем в одну цену на карточку (max), чтобы
    # результат не зависел от порядка строк, как это было у «последней строки».
    df = df.sort_values("price").drop_duplicates(subset="nm_id", keep="last")
    return df.rename(columns={"nm_id": "nmID"}), updated_at


def resolve_prices(
    db: Session,
    provider=None,
    allow_network: bool = True,
) -> tuple[Optional[pd.DataFrame], Optional[datetime], str]:
    """Цены для расчёта: снимок из БД, а если его нет — живой API WB.

    Снимок — основной источник: расчёт не должен ждать 80-145 с сети. Сеть
    остаётся запасным путём, чтобы не сломать расчёт на пустой БД (например,
    в тестах, где price_snapshots очищается, или до первого обновления цен).

    Возвращает (DataFrame, updated_at снимка, источник "db" | "wb_api").
    """
    df, updated_at = load_prices_from_db(db)
    if df is not None and not df.empty:
        return df, updated_at, "db"
    if not allow_network:
        return None, None, "db"
    prov = provider or _cab_provider()
    return prov.get_prices(), None, "wb_api"


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


def _funnel_slice(db: Session, date_from: date, date_to: date) -> tuple[dict, int]:
    """Последний срез воронки, пересекающий окно [date_from, date_to].

    Воронка WB — снимок за период (не по дням), поэтому берём одну (максимально
    свежую) запись среза и не суммируем срезы. Возвращает (метрики, дней_в_срезе).
    Метрики — расширенный набор (рейтинги, выкупы, конверсии, остатки WB, WB Клуб,
    вишлисты). Ключи — и как в БД, и в верхнем регистре (артикулы продуктов могут
    отличаться регистром от воронки/детализации). Дней_в_срезе нужен для R13
    (скорость из воронки: выкупы / дни среза).
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
        return out, 0
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
    return out, (to_d - from_d).days + 1


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
    """Остаток WB на складе (on-hand) по последнему срезу стоков.

    Без «в пути»: товары, едущие на склад, в остаток не входят (см. _latest_in_way).
    """
    latest = db.scalar(
        select(func.max(models.Stock.date))
        .where(models.Stock.marketplace_id == wb_mp)
    )
    if latest is None:
        return {}
    q = (
        select(
            models.Stock.article,
            func.sum(models.Stock.quantity).label("qty"),
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


def _latest_in_way(db: Session, wb_mp: int) -> dict:
    """«В пути» WB по последнему срезу стоков (inWayToClient + inWayFromClient)."""
    latest = db.scalar(
        select(func.max(models.Stock.date))
        .where(models.Stock.marketplace_id == wb_mp)
    )
    if latest is None:
        return {}
    q = (
        select(
            models.Stock.article,
            func.sum(models.Stock.in_way).label("qty"),
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


def _canon_article(value) -> str:
    """Канонический артикул для сопоставления строк связок (регистр/пробелы)."""
    return str(value or "").strip().upper()


def _reason_add(r: dict, note: str) -> None:
    """Дописывает пометку к причине, не теряя прежний текст."""
    old = (r.get("reason") or "").rstrip(". ")
    r["reason"] = (old + ". " if old else "") + note


def build_main_map(
    db: Session,
    main_info,
    settings=None,
    only_articles: Optional[set] = None,
) -> dict:
    """Карта влияния главной связки: canon(article) -> Δ% витринной цены.

    Считает свежую рекомендацию главной связки (read-only) под её собственной
    схемой и берёт только строки с action=RAISE: Δ = (target_vis−current_vis)/
    current_vis×100. Пустая карта при отсутствии главной, ошибке или её данных —
    влияние просто не применяется (расчёт зависимой связки не должен падать).

    only_articles — ограничить расчёт пересечением артикулов (дешевле).
    """
    if main_info is None:
        return {}
    out: dict = {}
    only = {_canon_article(a) for a in only_articles} if only_articles else None

    def _fn(db2, info):  # noqa: ARG001
        try:
            rec = recommendations(db2, settings, only_articles=only)
        except Exception:  # noqa: BLE001 — влияние не критично для расчёта
            return
        for r in rec.get("rows") or []:
            if r.get("action") != "RAISE":
                continue
            cur = _num(r.get("current_vis"))
            tgt = _num(r.get("target_vis"))
            if cur > 0 and tgt > cur:
                art = _canon_article(r.get("article"))
                if art:
                    out[art] = (tgt - cur) / cur * 100.0

    run_per_link(db, [main_info], _fn)
    return out


def main_influence_map(db: Session, settings=None,
                       only_articles: Optional[set] = None) -> dict:
    """Карта влияния главной связки для текущего пользователя/сессии.

    Пустая, если follow_main выключен, главная не задана, главная совпадает с
    активной связкой, или при любой ошибке — влияние не должно ломать расчёт
    зависимой связки. Настройки главной берутся её собственные (follow_main у
    неё принудительно снят, чтобы не было рекурсии).
    """
    s = merge_settings(settings)
    if not bool(s.get("follow_main", True)):
        return {}
    try:
        from app.services import cabinets as cabinet_service
        main = cabinet_service.resolve_main(db)
        if main is None:
            return {}
        active = cabinet_service.db_active_info(db)
        if active is not None and active.id == main.id:
            return {}
        ms = dict(cabinet_service.get_pricing_settings(db, main.id) or {})
        ms["follow_main"] = False
        return build_main_map(db, main, settings=ms, only_articles=only_articles)
    except Exception:  # noqa: BLE001
        return {}


def apply_main_influence(rows: list, main_map: dict, s: dict) -> int:
    """T-33 (R16): подтягивает строки зависимой связки к главной на половину.

    Если главная поднимает цену товара (Δ>0 против её же текущей), локальная
    витринная цена поднимается как минимум на main_influence_pct% от Δ. Правила:
      * влияют только ПОВЫШЕНИЯ главной (её снижения игнорируются);
      * локально цена не опускается: если локальный расчёт и так поднимает
        сильнее (например, горячий спрос) — не трогаем;
      * пол безубыточности (floor_price) соблюдается;
      * выше базовой цены не поднять — если скидка уже 0%, строка помечается
        skipped_no_headroom (автопилот базовую цену не меняет);
      * влияние применяется поверх min_delta_pp (иначе мелкие половинки глохнут).
    Возвращает число фактически изменённых строк.
    """
    if not main_map or not bool(s.get("follow_main", True)):
        return 0
    k = _num(s.get("main_influence_pct"), 50.0) / 100.0
    if k <= 0:
        return 0
    max_disc = _num(s.get("max_discount_pct"), 100.0)
    touched = 0
    for r in rows:
        d_pct = main_map.get(_canon_article(r.get("article")))
        if not d_pct or d_pct <= 0:
            continue
        price = _num(r.get("price"))
        cur_vis = _num(r.get("current_vis"))
        if price <= 0 or cur_vis <= 0:
            continue
        need_vis = cur_vis * (1 + d_pct * k / 100.0)
        tgt_vis = _num(r.get("target_vis"), cur_vis)
        if tgt_vis >= need_vis - 1e-9:
            continue  # локальный расчёт уже поднимает не ниже требуемого
        need_vis = min(need_vis, price)  # выше базовой цены скидка < 0 невозможна
        if need_vis <= cur_vis + 1e-9:
            # Скидка уже 0%: поднять цену нечем (базовая цена автопилотом не меняется).
            r["influenced_by_main"] = True
            r["main_delta_pct"] = round(d_pct, 2)
            r["action"] = "RAISE"
            r["status"] = "skipped_no_headroom"
            _reason_add(r, "влияние главной связки: скидка уже 0%, повышение невозможно")
            continue
        lo = _num(r.get("floor_price"))
        if lo and need_vis < lo:
            need_vis = lo  # пол только усиливает; на повышении не мешает
        new_disc = _clamp((1 - need_vis / price) * 100.0, 0.0, max_disc)
        cur_disc = _num(r.get("current_discount"))
        if new_disc >= cur_disc - 1e-9:
            continue  # лимит/пол съели шаг — фактического изменения нет
        new_vis = price * (1 - new_disc / 100.0)
        _reason_add(r, f"влияние главной связки: +{d_pct * k:.1f}% → {new_vis:.0f} ₽")
        r["action"] = "RAISE"
        r["status"] = "suggested"
        r["target_discount"] = round(new_disc, 1)
        r["target_vis"] = round(new_vis, 2)
        r["delta_discount"] = round(new_disc - cur_disc, 1)
        if all(key in r for key in ("comm_rate", "logistics_unit", "storage_unit",
                                    "other_unit", "net_cost")):
            r["margin_pct_at_target"] = _margin_pct(r["target_vis"], r)
        r["influenced_by_main"] = True
        r["main_delta_pct"] = round(d_pct, 2)
        touched += 1
    return touched


def recommendations(
    db: Session,
    settings=None,
    prices_df: Optional[pd.DataFrame] = None,
    provider=None,
    today: Optional[date] = None,
    only_articles: Optional[set] = None,
    main_map: Optional[dict] = None,
) -> dict:
    """Read-only расчёт рекомендаций по правилам R1-R10.

    only_articles — если задан, считать только эти товары (для main_map главной
    связки: нужны лишь пересекающиеся артикулы). main_map — заранее посчитанное
    влияние главной связки (article -> Δ% витринной цены, T-33/R16); при None
    влияние не применяется (вызывающий сам решает, строить ли его).
    """
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

    funnel, funnel_days = _funnel_slice(db, now_from, now_to)
    detail = _detail_metrics(db, now_from, now_to)
    stock = _latest_stock(db, wb_mp)
    in_way = _latest_in_way(db, wb_mp)
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
    volume_by_article: dict = {}
    for r in db.execute(select(
            models.Product.article, models.Product.name,
            models.Product.net_cost, models.Product.replenishable,
            models.Product.volume_l,
            models.Product.value, models.Product.supplier,
    ).order_by(models.Product.article)):
        art = str(r.article).strip().upper()
        products[art] = {
            "name": r.name or "",
            "net_cost": _num(r.net_cost),
            "replenishable": bool(r.replenishable),
            "value": _num(r.value),
            "supplier": (r.supplier or ""),
        }
        volume_by_article[art] = _num(r.volume_l)

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
    # Товар с нулевым складом, но идущий «в пути» — живой: не прячем как мёртвый.
    has_life |= {a for a, v in in_way.items() if _num(v) > 0}

    show_zero = bool(s["show_zero"])
    use_season = bool(s.get("use_season", True))
    use_sales = bool(s.get("use_sales", True))
    hidden_dead = 0
    non_wb = 0
    out_rows = []
    for art, prod in products.items():
        if only_articles is not None and art not in only_articles:
            continue
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
        # R13: нет строк продаж в окне, но воронка показывает активность —
        # берём скорость из среза воронки (по умолчанию выкупы), чтобы товар
        # не объявлялся «мёртвым запасом» (иначе живые товары дешевеют).
        if (use_sales and v_proj <= 0 and bool(s.get("velocity_from_funnel", True))
                and fl):
            field = str(s.get("funnel_velocity_field") or "buyouts")
            fv = _num(fl.get(field))
            if fv > 0 and funnel_days > 0:
                v_proj = fv / funnel_days
        f = {
            "article": art, "name": prod["name"], "replenishable": prod["replenishable"],
            "nm_id": nm_id, "price": price, "current_discount": cur_disc,
            "current_vis": cur_vis, "stock": stock.get(art),
            "in_way": in_way.get(art),
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
            "value": prod.get("value", 0.0),
            "supplier": prod.get("supplier", ""),
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

    for r in out_rows:
        if r.get("target_discount") is not None and r.get("current_discount") is not None:
            r["delta_discount"] = round(r["target_discount"] - r["current_discount"], 1)
        else:
            r["delta_discount"] = None

    # R18: мягкость снижения — LOWER делим на lower_soften, RAISE не трогаем.
    # Идёт ДО акций, чтобы добор участия (promo_push_pct) добавлялся к уже
    # смягчённой скидке и акция добирала свой уровень как задумано.
    soften_lower(out_rows, s)

    # T-31: акции WB — инфо-поля по каждой строке + R11 «добор участия».
    _promo_pass(out_rows, _active_promotions(db), s, volume_by_article)

    # T-33 (R16): межсвязочное влияние главной связки — ПОСЛЕ акций, чтобы быть
    # финальным словом (главная важнее локального добора в акцию).
    if main_map:
        apply_main_influence(out_rows, main_map, s)

    # T-41 (R19): потолок по рекомендации акции WB (regular, planDiscount).
    # Финальное правило: идёт после push (R11), смягчения (R18) и главной
    # связки (R16) — ничто не должно увести цену ниже, чем просит WB.
    promo_plan_apply(out_rows, _promo_plan_map(db), s)

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
        "avg_price": 0.0, "stock": None, "doc": None, "stock_value": 0.0,
        "velocity": 0.0, "v_proj": 0.0, "conv_pct": None, "backlog": 0,
        "net_cost": _num(prod["net_cost"]), "floor_price": None,
        "value": _num(prod.get("value")), "supplier": (prod.get("supplier") or ""),
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
        "views": int(f.get("views") or 0),
        "adds": int(f.get("adds") or 0),
        "orders": int(f.get("orders") or 0),
        "cancelled": int(f.get("cancelled") or 0),
        "add_to_wishlist": int(f.get("add_to_wishlist") or 0),
        "share_order_percent": _num(f.get("share_order_percent")),
        "avg_orders_per_day": _num(f.get("avg_orders_per_day")),
        "stock_wb": int(f.get("stock_wb") or 0),
        "in_way": int(f.get("in_way") or 0),
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
        "stock_value": round(_num(f.get("stock")) * _num(f.get("net_cost")), 2),
        "doc": None, "conv_pct": None, "backlog": 0, "net_cost": f["net_cost"],
        "value": _num(f.get("value")), "supplier": (f.get("supplier") or ""),
        "comm_rate": f["comm_rate"], "floor_price": f["floor_price"],
        "logistics_unit": _num(f.get("logistics_unit")),
        "storage_unit": _num(f.get("storage_unit")),
        "other_unit": _num(f.get("other_unit")),
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
    rating = f.get("feedback_rating", 0)
    if rating > 0 and rating < _num(s["min_rating_for_raise"]):
        return f"рейтинг по отзывам {rating:.1f} — качество не позволяет поднимать цену"
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
    if f.get("feedback_rating", 0) >= _num(s["strong_rating"]):
        strong += 1
    if f.get("conv_buyout_percent", 0) >= _num(s["strong_buyout_conv"]):
        strong += 1
    if f.get("detail_known") and f.get("return_rate", 0) <= _num(s["strong_return_rate"]):
        strong += 1
    if f.get("detail_known") and f.get("margin_pct", 0) >= _num(s["strong_margin_pct"]):
        strong += 1
    return (1 + _num(s["raise_boost_pct"]) / 100) if strong >= 2 else 1.0


def _demand_hot(f: dict, s: dict, doc: float) -> Optional[str]:
    """R12: подтверждённый спрос по сигналам воронки.

    Считает сигналы из 4 групп — выкупы, «в корзине» (adds−orders), рейтинг
    отзывов, конверсия заказов. Возвращает текст-причину, если набрано не меньше
    demand_min_signals групп И DOC не выше demand_raise_max_doc; иначе None.
    Нулевой порог отключает соответствующую группу (0 = «нет данных» не считаем).
    """
    doc_cap = _num(s.get("demand_raise_max_doc"), 0.0)
    if doc_cap > 0 and doc > doc_cap:
        return None
    signals: list = []

    def hit(threshold, value, text):
        thr = _num(threshold)
        if thr > 0 and value >= thr:
            signals.append(text)

    buyouts = int(f.get("buyouts") or 0)
    hit(s.get("demand_min_buyouts"), buyouts, f"выкупы {buyouts}")
    backlog = max(int(f.get("adds") or 0) - int(f.get("orders") or 0), 0)
    hit(s.get("demand_min_backlog"), backlog, f"в корзине {backlog}")
    rating = _num(f.get("feedback_rating"))
    hit(s.get("demand_min_rating"), rating, f"рейтинг {rating:.1f}")
    views = int(f.get("views") or 0)
    conv = (int(f.get("orders") or 0) / views * 100) if views > 0 else 0.0
    hit(s.get("demand_min_conv_pct"), conv, f"конверсия {conv:.1f}%")

    need = int(_num(s.get("demand_min_signals"), 2))
    if len(signals) < max(need, 1):
        return None
    return ", ".join(signals)


def _promo_cap_pct(description) -> Optional[float]:
    """Потолок промо-скидки WB из описания акции («промо-скидка не более N%») → N.

    Возвращает None, если потолок не указан. Это скидка WB поверх цели, а не
    наша собственная — используется как справочная цифра в UI.
    """
    desc = description or ""
    m = re.search(r"не более\s*(\d+(?:[.,]\d+)?)\s*%", desc, re.I)
    if m:
        return float(m.group(1).replace(",", "."))
    m = re.search(r"до\s*(\d+(?:[.,]\d+)?)\s*%", desc, re.I)
    if m:
        return float(m.group(1).replace(",", "."))
    return None


def _parse_ranging(raw) -> list:
    """ranging_json (строка JSON или list) → [{'rate': %, 'boost': %, 'condition': str}]."""
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    tiers = []
    if isinstance(data, list):
        for item in data:
            if not isinstance(item, dict):
                continue
            rate = _num(item.get("participationRate"))
            if rate <= 0:
                continue
            tiers.append({
                "rate": rate,
                "boost": _num(item.get("boost")),
                "condition": str(item.get("condition") or "").strip(),
            })
    return sorted(tiers, key=lambda t: t["rate"])


def _active_promotions(db: Session) -> list:
    """Текущие активные акции WB (доступные для участия = по умолчанию)."""
    now = datetime.utcnow()
    rows = db.execute(
        select(models.Promotion)
        .where(models.Promotion.starts_at <= now,
               models.Promotion.ends_at >= now)
        .order_by(models.Promotion.ends_at.asc())
    ).scalars().all()
    out = []
    for p in rows:
        cap = _promo_cap_pct(p.description)
        out.append({
            "id": p.promo_id,
            "name": p.name or "",
            "participation": _num(p.participation_percent),
            "cap": round(cap, 1) if cap is not None else None,
            "tiers": _parse_ranging(p.ranging_json),
        })
    return out


def _promo_pass(rows: list, promos: list, s: dict, volume_by_article: dict) -> None:
    """R11: акции WB в рекомендациях.

    Всегда добавляет инфо-поля строк (кол-во/названия акций, максимум участия,
    ближайший ranging-тир + его буст, потолок промо-скидки, добавленные п.п.,
    разрыв до тира, буст уже достигнутого уровня). При promo_enabled — «добор
    участия»: поднимает целевую скидку на promo_push_pct у первых K кандидатов
    (наименее рентабельные → больший остаток → медленнее оборот → объёмные),
    чтобы добирать участие лучшей акции до её следующего тира. Технически
    скидка может уйти ниже безубытка — до promo_max_beyond_floor_pp п.п.

    Модель тиров: WB отдаёт у каждой акции лестницу ranging
    (participationRate → boost) и агрегатный participationPercentage по своим
    товарам, но НЕ отдаёт требования по конкретному артикулу. Поэтому уровень
    участия конкретной строки здесь выводится из ЕЁ скидки: буст своего уровня
    — буст наивысшего тира, rate ≤ текущая скидка; разрыв до тира — насколько
    скидка ниже уровня следующего тира. Это наша интерпретация, не данные WB.
    """
    empty_info = {
        "promo_count": 0, "promo_names": "", "promo_part_pct": 0,
        "promo_tier_pct": None, "promo_tier_boost": None,
        "promo_cap_pct": None, "promo_push_applied": False,
        "promo_delta_discount": 0.0, "promo_gap_pp": None,
        "promo_boost_gain": None,
        "promo_plan_pct": None, "promo_cap_applied": False,
    }
    if not promos:
        for r in rows:
            r.update(empty_info)
        return

    # Названия: WB отдаёт несколько promo_id на одно действие («Осенние скидки»
    # по карточкам/цвету), поэтому дубли схлопываем — иначе колонка врёт.
    uniq, seen = [], set()
    for p in promos:
        nm = p["name"].strip()
        if not nm or nm in seen:
            continue
        seen.add(nm)
        uniq.append(nm)
    names = ", ".join(uniq[:3])
    if len(uniq) > 3:
        names += "…"
    max_part = max((p["participation"] for p in promos), default=0.0)
    cap = max((p["cap"] for p in promos if p["cap"] is not None), default=None)

    # Лучшая акция для добора: ближайший достижимый ranging-тир.
    best, best_next = None, None
    for p in promos:
        nxt = next((t for t in p["tiers"] if t["rate"] > p["participation"]), None)
        if nxt is None:
            continue
        if best_next is None or nxt["rate"] < best_next["rate"]:
            best, best_next = p, nxt

    tier_rate = best_next["rate"] if best_next else None
    best_tiers = best["tiers"] if best else []

    def tier_metrics(r) -> tuple:
        """(разрыв до тира, буст достигнутого уровня) для строки.

        Считается от скидки, в которую автопилот целится (target_discount), а при
        его отсутствии — от текущей. Поэтому после добора (см. ниже) метрики
        пересчитываются и показывают остаток именно по итоговому решению.
        """
        disc = r.get("target_discount")
        if disc is None:
            disc = r.get("current_discount")
        if disc is None:
            return None, None
        d = _num(disc)
        gap = round(tier_rate - d, 1) if tier_rate is not None else None
        reached = [t for t in best_tiers if t["rate"] <= d + 1e-9]
        return gap, (reached[-1]["boost"] if reached else None)

    for r in rows:
        gap_pp, boost_gain = tier_metrics(r)
        r.update({
            "promo_count": len(promos), "promo_names": names,
            "promo_part_pct": round(max_part, 1), "promo_cap_pct": cap,
            "promo_tier_pct": tier_rate,
            "promo_tier_boost": best_next["boost"] if best_next else None,
            "promo_push_applied": False,
            "promo_delta_discount": 0.0,
            "promo_gap_pp": gap_pp,
            "promo_boost_gain": boost_gain,
            "promo_plan_pct": None,
            "promo_cap_applied": False,
        })

    if not s.get("promo_enabled") or best_next is None or best is None or not rows:
        return

    push = _num(s.get("promo_push_pct"))
    beyond = _num(s.get("promo_max_beyond_floor_pp"))
    max_disc = _num(s.get("max_discount_pct"), 100.0)
    min_delta = _num(s.get("min_delta_pp"))

    candidates = []
    for r in rows:
        if r.get("action") not in ("HOLD", "LOWER") or not r.get("nm_id"):
            continue
        if _num(r.get("stock")) == 0:
            continue
        price = _num(r.get("price"))
        cur = _num(r.get("current_discount"))
        if price <= 0 or cur < 0:
            continue
        lo = _num(r.get("floor_price"))
        floor_disc = (1 - lo / price) * 100 if lo > 0 else None
        cap_disc = min(max_disc, 99.0)
        if floor_disc is not None:
            cap_disc = min(cap_disc, floor_disc + beyond)
        base_t = _num(r.get("target_discount"), cur)
        if not (cur <= base_t and base_t < cap_disc - 1e-9):
            continue
        candidates.append((r, cur, base_t, cap_disc))
    if not candidates:
        return

    # Сколько товаров нужно, чтобы покрыть разрыв до тира лучшей акции.
    gap = best_next["rate"] - best["participation"]
    needed = int(math.ceil(max(0.0, gap) / 100 * max(1, len(rows))))
    needed = min(max(needed, 1), len(candidates))

    def margin_key(r):
        m = r.get("margin_per_one")
        return 1e9 if m is None else _num(m)

    def stock_key(r):
        st = r.get("stock")
        return 0.0 if st is None else _num(st)

    def value_key(r):
        # R15: при включённом весе себестоимости приоритет отдаём остатку по
        # деньгам (100 шт × 1000 ₽ важнее 100 шт × 100 ₽), а не по штукам.
        if bool(s.get("stock_value_weight", True)):
            sv = r.get("stock_value")
            if sv is not None:
                return _num(sv)
        return stock_key(r)

    def doc_key(r):
        d = r.get("doc")
        return float("inf") if d is None else _num(d)

    def vol_key(r):
        return _num(volume_by_article.get(r.get("article")))

    candidates.sort(key=lambda c: (margin_key(c[0]), -value_key(c[0]),
                                   -doc_key(c[0]), -vol_key(c[0])))

    for r, cur, base_t, cap_disc in candidates[:needed]:
        new_t = min(cap_disc, base_t + push)
        added = new_t - base_t
        if new_t - cur < min_delta - 1e-9 or added <= 1e-9:
            continue
        # Текст причины описывает ФАКТИЧЕСКИЙ добор, а не «до тира»: строка может
        # уже быть выше уровня тира, и раньше это читалось как ошибка. Итог участия
        # (достигнут ли уровень) показывает promo_gap_pp.
        note = (f"акция WB «{best['name']}»: +{added:.1f} п.п. к скидке"
                f" (уровень {tier_rate:.0f}%, буст ×{best_next['boost']:.0f})")
        if r["action"] == "HOLD":
            r["action"] = "LOWER"
            r["status"] = "suggested"
            r["reason"] = note
        else:
            r["status"] = "suggested"
            reason = (r.get("reason") or "").rstrip(".")
            r["reason"] = ((reason + ". ") if reason else "") + note
        r["promo_delta_discount"] = round(added, 2)
        r["target_discount"] = round(new_t, 1)
        r["target_vis"] = round(_num(r["price"]) * (1 - new_t / 100), 2)
        r["delta_discount"] = round(new_t - cur, 1)
        # Метрики участия — по итоговой скидке: разрыв мог сократиться, а уровень
        # мог подняться (буст) именно благодаря добору.
        r["promo_gap_pp"], r["promo_boost_gain"] = tier_metrics(r)
        if all(k in r for k in ("comm_rate", "logistics_unit", "storage_unit",
                                "other_unit", "net_cost")):
            r["margin_pct_at_target"] = _margin_pct(r["target_vis"], r)
        r["promo_push_applied"] = True


def _promo_plan_map(db) -> dict:
    """nm_id → максимальная рекомендованная WB скидка (planDiscount), %.

    T-41 / R19. Источник — wb_promo_nomenclatures (GET /calendar/promotions/
    nomenclatures), только regular-акции: у автоакций endpoint отвечает 422 и
    per-product требований WB не отдаёт. Берём активные акции (starts_at/ends_at
    покрывают текущий момент) и по каждому товару — максимум planDiscount среди
    участников (inAction=true) и подходящих кандидатов (inAction=false).
    """
    if db is None:
        return {}
    now = datetime.utcnow()
    try:
        rows = db.execute(
            select(
                models.PromoNomenclature.nm_id,
                func.max(models.PromoNomenclature.plan_discount).label("cap"),
            )
            .join(models.Promotion,
                  models.Promotion.promo_id == models.PromoNomenclature.promo_id)
            .where(
                models.Promotion.adv_type == "regular",
                models.Promotion.starts_at <= now,
                models.Promotion.ends_at >= now,
            )
            .group_by(models.PromoNomenclature.nm_id)
        ).all()
    except Exception:  # noqa: BLE001 — нет таблицы/колонки не должен ронять расчёт
        return {}
    return {str(nm): _num(cap) for nm, cap in rows if nm}


def promo_plan_apply(rows: list, plan_map: dict, s: dict) -> int:
    """R19: не уходить ниже скидки, рекомендованной акцией WB (regular).

    Финальный потолок — вызывается ПОСЛЕ push (R11), смягчения (R18) и влияния
    главной связки (R16), чтобы ничто не увело цену ниже рекомендации WB.
    Правило работает только при promo_enabled; инфо-поле promo_plan_pct
    («WB просит скидку») заполняется всегда, когда для товара есть данные.

    effective_cap = max(cap, current_discount): потолок лишь предотвращает
    УГЛУБЛЕНИЕ скидки, но никогда не заставляет повышать цену — товар, уже
    сидящий глубже рекомендации WB, остаётся как есть. Возвращает число строк,
    у которых целевая скидка была ограничена.
    """
    for r in rows:
        r.setdefault("promo_plan_pct", None)
        r.setdefault("promo_cap_applied", False)
    if not plan_map:
        return 0
    for r in rows:
        nm = str(r.get("nm_id") or "")
        cap = plan_map.get(nm)
        if not nm or cap is None:
            continue
        r["promo_plan_pct"] = round(cap, 1)
        if not s.get("promo_enabled"):
            continue
        tgt = r.get("target_discount")
        if tgt is None:
            continue
        tgt = _num(tgt)
        cur = _num(r.get("current_discount"))
        effective_cap = max(cap, cur)
        if tgt <= effective_cap + 1e-9:
            continue
        price = _num(r.get("price"))
        new_disc = round(effective_cap, 1)
        r["target_discount"] = new_disc
        if price > 0:
            r["target_vis"] = round(price * (1 - new_disc / 100), 2)
        r["delta_discount"] = round(new_disc - cur, 1)
        if all(k in r for k in ("comm_rate", "logistics_unit", "storage_unit",
                                "other_unit", "net_cost")):
            r["margin_pct_at_target"] = _margin_pct(r["target_vis"], r)
        r["promo_cap_applied"] = True
        if r.get("action") in ("LOWER", "HALVE"):
            r["status"] = "suggested"
            reason = (r.get("reason") or "").rstrip(".")
            note = (f"потолок акции WB: скидка ограничена планом WB "
                    f"{new_disc:.1f}%")
            r["reason"] = ((reason + ". ") if reason else "") + note
    return sum(1 for r in rows if r.get("promo_cap_applied"))


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


def _value_discount_scale(value, net_cost, s) -> float:
    """Множитель снижения скидки по отношению «ценность/себестоимость».

    value (pure_value из каталога) и net_cost (себестоимость): чем больше
    ценность превышает себестоимость (value/net_cost), тем ценнее товар — тем
    меньше скидка при LOWER. Нет данных (value ≤ 0 или net_cost ≤ 0) или
    value ≤ себестоимости → 1.0 (без ограничений); при отношении
    ≥ ``value_ratio_high`` → 0.0 (скидку не увеличиваем вовсе); между порогами
    ``value_ratio_low`` и ``value_ratio_high`` — линейно.
    """
    v = _num(value)
    c = _num(net_cost)
    if v <= 0 or c <= 0:
        return 1.0
    lo = _num(s.get("value_ratio_low", 1.0))
    hi = _num(s.get("value_ratio_high", 4.0))
    if hi <= lo:
        return 1.0
    r = v / c
    if r <= lo:
        return 1.0
    if r >= hi:
        return 0.0
    return (hi - r) / (hi - lo)


def _valued_item_reason(f: dict, rating_scale: float, value_scale: float) -> str:
    """Пояснение ветки «ценный товар»: какие именно факторы заглушили LOWER."""
    parts = []
    if rating_scale <= 0.01:
        parts.append(f"высокий рейтинг по отзывам {_num(f.get('feedback_rating')):.1f}")
    if value_scale <= 0.01:
        v = _num(f.get("value"))
        c = _num(f.get("net_cost"))
        if v > 0 and c > 0:
            parts.append(f"ценность {v / c:.1f}× себестоимости")
        else:
            parts.append("высокая ценность")
    return "ценный товар (" + "; ".join(parts) + ") — скидку не увеличиваем"


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
    # Ценность (value) относительно себестоимости: тоже тормозит LOWER.
    value_scale = _value_discount_scale(f.get("value"), f.get("net_cost"), s)
    # Оба фактора «ценного товара» объединяются в один множитель снижения.
    item_scale = rating_scale * value_scale

    if use_inv and stock is None:
        base.update(action="SKIP", status="skipped_no_stock",
                    reason="нет данных об остатках (вытяните WB Остатки)")
        return base
    if use_inv and stock <= 0:
        base.update(action="HOLD", status="hold", reason="распродан (остаток 0)")
        return base

    if use_margin:
        if f["comm_rate"] is None or floor is None:
            # R14: unit-экономики нет (нет продаж/доходов за окно), но остаток
            # есть и известна себестоимость. По политике cost_floor работаем от
            # пола = себестоимость + floor_margin_pct (обычный LOWER по
            # мёртвому/перезапасу), иначе — прежний SKIP.
            # R14: unit-экономики нет (нет продаж/доходов за окно), но остаток
            # есть и известна себестоимость. Включённый cost_floor даёт пол =
            # себестоимость + floor_margin_pct (обычный LOWER по мёртвому/
            # перезапасу), иначе — прежний SKIP.
            net_cost = _num(f.get("net_cost"))
            if bool(s.get("no_econ_cost_floor", True)) and net_cost > 0 and price > 0:
                floor = net_cost * (1 + _num(s["floor_margin_pct"]) / 100)
                f["floor_price"] = floor
            else:
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
            if item_scale <= 0.01:
                return _mandatory_step(
                    base, f, s, "LOWER",
                    _valued_item_reason(f, rating_scale, value_scale))
            target_vis = max(_num(floor), eff * (1 - max_drop * item_scale / 100))
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

    # R12: подтверждённый спрос поднимает цену вопреки зоне DOC (в норме и
    # перезапасе), пока DOC не выше demand_raise_max_doc. Обходим «половинный
    # кап» (_half_discount_guard), иначе рост цены почти незаметен. В дефиците
    # (doc < doc_low) работает нижестоящая логика с качественными гейтами T-14.
    if (use_orders and bool(s.get("demand_raise", True)) and price > 0
            and doc >= doc_low):
        demand_reason = _demand_hot(f, s, doc)
        if demand_reason:
            raise_pct = _num(s.get("demand_raise_pct"), 15.0)
            target_vis = min(price, cur_vis * (1 + raise_pct / 100))
            new_disc = (1 - target_vis / price) * 100
            if cur_disc - new_disc >= _num(s["min_delta_pp"]):
                base.update(action="RAISE", status="suggested",
                            reason=f"устойчивый спрос ({demand_reason}) — поднимаем цену",
                            target_discount=round(_clamp(new_disc, 0, 100), 1),
                            target_vis=round(price * (1 - new_disc / 100), 2),
                            margin_pct_at_target=_margin_pct(price * (1 - new_disc / 100), f))
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
        if item_scale <= 0.01:
            return _mandatory_step(
                base, f, s, "LOWER",
                _valued_item_reason(f, rating_scale, value_scale))
        k = 0.5
        if use_orders and conv < 1.0:
            k *= 0.8
        if use_season:
            if f["trend"] < 1.0:
                k *= 0.8
            elif f["trend"] > 1.0:
                k *= 1.2
        k = _clamp(k, 0.2, 1.2)
        factor = max(1 - max_drop * item_scale / 100, (_num(s["target_doc"]) / doc) ** k)
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


def soften_lower(rows: list, s: dict) -> int:
    """R18: снижение цены — в lower_soften раз мягче; повышения не трогаем.

    Дельта LOWER (target_discount − current_discount) делится на lower_soften,
    но никогда не ужимается ниже min_delta_pp: минимальный шаг (правило
    «коррекция обязательна») и защита от флапа остаются как есть. Вызывается
    после решений, ДО акций (чтобы добор участия шёл поверх смягчённой скидки)
    и до влияния главной связки. Возвращает число смягчённых строк.
    """
    soften = _num(s.get("lower_soften"), 1.0)
    if soften <= 1.0:
        return 0
    min_delta = _num(s.get("min_delta_pp"), 1.0)
    n = 0
    for r in rows:
        if r.get("action") != "LOWER":
            continue
        price = _num(r.get("price"))
        cur = _num(r.get("current_discount"))
        tgt = _num(r.get("target_discount"))
        if price <= 0 or tgt - cur <= min_delta + 1e-9:
            continue  # минимальный шаг не смягчаем
        new_disc = max(cur + (tgt - cur) / soften, cur + min_delta)
        new_disc = _clamp(new_disc, 0, 100)
        if abs(new_disc - tgt) < 1e-9:
            continue
        new_vis = price * (1 - new_disc / 100)
        r["target_discount"] = round(new_disc, 1)
        r["target_vis"] = round(new_vis, 2)
        r["delta_discount"] = round(new_disc - cur, 1)
        r["margin_pct_at_target"] = _margin_pct(new_vis, r)
        n += 1
    return n


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
        is_applied = (r["action"] in ("RAISE", "LOWER", "HALVE", "MIN")
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
    main_map: Optional[dict] = None,
) -> dict:
    """Расчёт + применение скидок через WbProvider.update_prices + журнал PriceChange."""
    s = merge_settings(settings)
    # Мёртвые товары скрыты от UI, но применение обязано их обработать
    # (HALVE — шаг «скидка ÷2» идёт каждый прогон).
    rec_settings = merge_settings(s)
    rec_settings["show_zero"] = True
    rec = recommendations(db, rec_settings, prices_df=prices_df, provider=provider,
                          today=today, main_map=main_map)
    rows = rec["rows"]
    today = today or date.today()
    cooldown_from = today - timedelta(days=int(s["cooldown_days"]))

    # Кулдаун — только по изменениям скидки (RAISE/LOWER/HALVE). MIN (минимальные
    # цены, T-33) не должен блокировать последующие шаги автопилота.
    applied_past = set(db.scalars(
        select(models.PriceChange.article).where(
            models.PriceChange.status == "applied",
            models.PriceChange.action.in_(("RAISE", "LOWER", "HALVE")),
            models.PriceChange.applied_at >= cooldown_from,
        )
    ).all())

    items = _pushed_items(rows, s, applied_past)
    now = datetime.now()
    prov = provider or _cab_provider()

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
            models.PriceChange.action.in_(("RAISE", "LOWER", "HALVE")),
            models.PriceChange.applied_at >= cooldown_from,
        )
    ).all())
    items = _pushed_items(rows, s, applied_past)
    prov = provider or _cab_provider()

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


def apply_min_prices(
    db: Session,
    rows: Optional[list] = None,
    settings=None,
    provider=None,
    today: Optional[date] = None,
) -> dict:
    """T-33: минимальные (break-even) цены — price=floor_price, discount=0.

    Отправляются только строки, у которых текущая витринная цена ниже пола
    безубыточности (profit ≥ 0 уже не выполняется). rows=None → пересчитываются
    рекомендации (show_zero=True, чтобы не пропустить убыточные нулевые). Порог
    WB (priceLimits.minPrice) применяется как кламп ИСТИННОЙ минимальной цены:
    итог = max(пол, мин. цена WB). Пишет журнал price_changes с action=MIN
    (не участвует в кулдауне автопилота).
    """
    s = merge_settings(settings or {})
    if rows is None:
        rec_settings = merge_settings(s)
        rec_settings["show_zero"] = True
        prices_df, _u, _src = resolve_prices(db, provider=provider, allow_network=True)
        rows = recommendations(
            db, rec_settings, prices_df=prices_df, provider=provider, today=today,
        ).get("rows") or []
    rows = [dict(r) for r in rows]

    targets, no_nm = [], 0
    for r in rows:
        floor = _num(r.get("floor_price"))
        cur_vis = _num(r.get("current_vis"))
        if floor <= 0 or cur_vis <= 0 or cur_vis >= floor - 1e-9:
            continue  # цена не ниже пола — товар и так безубыточен
        try:
            nm = int(r.get("nm_id"))
        except (TypeError, ValueError):
            nm = 0
        if nm <= 0:
            no_nm += 1
            continue
        r["nm_id"] = nm
        r["_min_price"] = max(100.0, float(floor))
        targets.append(r)

    if not targets:
        return {"applied": [], "pushed": 0, "skipped": no_nm,
                "note": "Нет товаров с ценой ниже безубыточного минимума."}

    prov = provider or _cab_provider()
    items = [{"nmID": r["nm_id"], "price": r["_min_price"], "discount": 0}
             for r in targets]
    get_min = getattr(prov, "get_min_prices", None)
    if callable(get_min):
        try:
            wb_min = get_min([it["nmID"] for it in items]) or {}
        except Exception:  # noqa: BLE001 — клампинг по WB пропускается
            wb_min = {}
        for it in items:
            wb = _num((wb_min or {}).get(str(it["nmID"])))
            if wb > 0:
                it["price"] = max(it["price"], wb)

    # upload/task принимает только целые price и discount (0..99).
    for it in items:
        p = float(it["price"] or 0)
        it["price"] = max(100, int(math.ceil(p - 1e-9)))

    now = datetime.now()
    try:
        result = prov.update_prices(items)
    except Exception as exc:  # noqa: BLE001
        for r in targets:
            r["action"] = "MIN"
            r["status"] = "error"
            r["reason"] = f"минимальная цена: {exc}"
        _record(db, targets, applied_at=None, error=str(exc))
        raise

    by_nm = {it["nmID"]: it["price"] for it in items}
    for r in targets:
        sent = by_nm.get(r["nm_id"], r["_min_price"])
        r["action"] = "MIN"
        r["status"] = "applied"
        r["target_discount"] = 0.0
        r["target_vis"] = round(sent, 2)
        r["reason"] = (f"минимальная цена (безубыток): {sent:.0f} ₽ (пол "
                       f"{_num(r.get('floor_price')):.0f} ₽), скидка 0%")
        r["delta_discount"] = round(0.0 - _num(r.get("current_discount")), 1)
    _record(db, targets, applied_at=now)
    applied_ids = [r["article"] for r in targets]
    note = f"Минимальные цены отправлено: {len(items)}."
    if no_nm:
        note += f" Пропущено без nm_id: {no_nm}."
    return {"applied": applied_ids, "pushed": len(items), "skipped": no_nm,
            "task_id": result.get("task_id") if isinstance(result, dict) else None,
            "note": note}