"""Массовое обновление данных из API маркетплейсов + общий слой выгрузки.

Содержит:
- pull_* функции: «спасти из панельного скачивания» — единый код записи в БД,
  который используют и хендлеры панелей, и фоновое «Обновить WB / Ozon».
- фоновые задания (threading): последовательно по видам, ошибка вида не
  блокирует остальные; история запусков пишется в refresh_runs.
"""
import json
import logging
import threading
import time
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
import requests
from fastapi import HTTPException
from sqlalchemy import select, text

from app import models

logger = logging.getLogger("agent_market.refresh")
from app.database import SessionLocal
from app.providers import factory as provider_factory
from app.providers.errors import (MarketError, translate_request_error)
from app.providers.ozon import OzonProvider
from app.providers.wb import WbProvider
from app.services import sync as sync_service
from app.services import progress as progress_service
from app.services import cabinets
from app.services.cabinets import active_credentials
from app.services.window import parse_window


# ------------------------------------------------------------------ окна и help
def default_range():
    """Окно по умолчанию (сегодня минус sync_days_default)."""
    return parse_window()


def resolve_range(date_from=None, date_to=None):
    """Разрешает строки дат в (date, date); пустое — окно по умолчанию."""
    return parse_window(date_from, date_to)


def _col_num(df: pd.DataFrame, names) -> pd.Series:
    """Числовая колонка из df по списку имён (иначе нулевой столбец)."""
    for n in names:
        if n in df.columns:
            return pd.to_numeric(df[n], errors="coerce").fillna(0)
    return pd.Series(0, index=df.index)


def _pick_col(df: pd.DataFrame, names) -> Optional[pd.Series]:
    """Первый существующий столбец из списка кандидатов (или None)."""
    for n in names:
        if n in df.columns:
            return df[n]
    return None


# ---------------------------------------------------- карта nmID -> артикул WB
_NM_CACHE = {"ts": 0.0, "data": {}}


def _wb_nmid_to_article() -> dict:
    """Карта nmId -> артикул продавца. Источник: таблица nm_articles,
    заполняется при скачивании 'WB API ▸ Карточки товара' (без v5 rate-лимита)."""
    if time.time() - _NM_CACHE["ts"] < 3600:
        return _NM_CACHE["data"]
    db = SessionLocal()
    try:
        pairs = db.execute(select(models.NmArticle.nm_id, models.NmArticle.article)).all()
        _NM_CACHE["data"] = {nm: art.strip() for nm, art in pairs if art and art.strip()}
        _NM_CACHE["ts"] = time.time()
    except Exception:  # noqa: BLE001
        pass
    finally:
        db.close()
    return _NM_CACHE["data"]


def _wb_nm_cache_reset():
    _NM_CACHE["data"] = {}
    _NM_CACHE["ts"] = 0.0


def _wb_card_dims(db) -> dict:
    """Карта chrt_id -> (size, barcode) из загруженных карточек WB."""
    mp_wb = db.execute(
        select(models.Marketplace.id).where(models.Marketplace.code == "wb")
    ).scalar()
    if mp_wb is None:
        return {}
    rows = db.execute(
        select(models.MarketplaceCard.chrt_id,
               models.MarketplaceCard.size,
               models.MarketplaceCard.barcode)
        .where(models.MarketplaceCard.marketplace_id == mp_wb)
    ).all()
    return {str(r.chrt_id): (str(r.size or ""), str(r.barcode or "")) for r in rows}


# Колонки реального ответа WB analytics/v3/sales-funnel/products
# (pd.json_normalize верхнего уровня: product.*, statistic.selected.* и т.д.)
FUNNEL_NM_CANDIDATES = ["product.nmID", "product.nmId", "nmID", "nmId", "nm_id"]
FUNNEL_VENDOR_CANDIDATES = ["product.vendorCode", "vendorCode", "vendor_code",
                            "supplierArticle", "article"]
FUNNEL_COL_MAP = {
    "views": ["statistic.selected.openCount", "viewsCount", "views", "viewCount"],
    "opens": ["statistic.selected.openCardCount", "opens", "openCardCount"],
    "adds": ["statistic.selected.cartCount", "addToCartCount", "adds", "addCartCount"],
    "orders": ["statistic.selected.orderCount", "orderCount", "ordersCount", "orders"],
    "cancelled": ["statistic.selected.cancelCount", "cancelCount",
                  "cancelledOrdersCount", "cancelled"],
    "buyouts": ["statistic.selected.buyoutCount", "buyoutCount", "boughtCount", "buyouts"],
    "avg_price": ["statistic.selected.avgPrice", "avgPrice", "orderAvgPrice", "avg_price"],
    "revenue": ["statistic.selected.orderSum", "statistic.selected.orderSumRub",
                "revenue", "orderSum", "orderSumRub", "salesSum", "sales_sum"],
    "buyout_sum": ["statistic.selected.buyoutSum", "buyoutSum", "boughtSum", "buyout_sum"],
}

# Все остальные поля, которые отдаёт analytics/v3/sales-funnel/products
# (проверено на живом ответе 2026-09-17): карточка, остатки и блок selected.
FUNNEL_EXTRA_COL_MAP = {
    "subject_name": ["product.subjectName", "subjectName"],
    "brand_name": ["product.brandName", "brandName"],
    "title": ["product.title", "title"],
    "subject_id": ["product.subjectId", "subjectId"],
    "tags": ["product.tags", "tags"],
    "product_rating": ["product.productRating", "productRating"],
    "feedback_rating": ["product.feedbackRating", "feedbackRating"],
    "stock_wb": ["product.stocks.wb", "stocks.wb"],
    "stock_mp": ["product.stocks.mp", "stocks.mp"],
    "stock_balance_sum": ["product.stocks.balanceSum", "stocks.balanceSum"],
    "cancel_sum": ["statistic.selected.cancelSum", "cancelSum"],
    "avg_orders_per_day": ["statistic.selected.avgOrdersCountPerDay",
                          "avgOrdersCountPerDay"],
    "share_order_percent": ["statistic.selected.shareOrderPercent", "shareOrderPercent"],
    "add_to_wishlist": ["statistic.selected.addToWishlist", "addToWishlist"],
    "localization_percent": ["statistic.selected.localizationPercent", "localizationPercent"],
    "conv_to_cart_percent": ["statistic.selected.conversions.addToCartPercent"],
    "conv_cart_to_order_percent": ["statistic.selected.conversions.cartToOrderPercent"],
    "conv_buyout_percent": ["statistic.selected.conversions.buyoutPercent"],
    "wb_club_order_count": ["statistic.selected.wbClub.orderCount"],
    "wb_club_order_sum": ["statistic.selected.wbClub.orderSum"],
    "wb_club_buyout_count": ["statistic.selected.wbClub.buyoutCount"],
    "wb_club_buyout_sum": ["statistic.selected.wbClub.buyoutSum"],
    "wb_club_cancel_count": ["statistic.selected.wbClub.cancelCount"],
    "wb_club_cancel_sum": ["statistic.selected.wbClub.cancelSum"],
    "wb_club_avg_price": ["statistic.selected.wbClub.avgPrice"],
    "wb_club_buyout_percent": ["statistic.selected.wbClub.buyoutPercent"],
    "wb_club_avg_orders_per_day": ["statistic.selected.wbClub.avgOrderCountPerDay"],
}

FUNNEL_TIME_TO_READY = {
    "days": ["statistic.selected.timeToReady.days", "timeToReady.days"],
    "hours": ["statistic.selected.timeToReady.hours", "timeToReady.hours"],
    "mins": ["statistic.selected.timeToReady.mins", "timeToReady.mins"],
}


def _col_str(df: pd.DataFrame, names) -> pd.Series:
    """Строковая колонка из df по списку имён (иначе пустой столбец)."""
    col = _pick_col(df, names)
    if col is None:
        return pd.Series("", index=df.index)
    return col.astype(str).str.strip().replace("nan", "")


def _col_json(df: pd.DataFrame, names) -> pd.Series:
    """Колонка-список (напр. tags) -> текст ", "-разделитель (иначе "").

    Плоский список строк — люди-читаемые теги; иначе — JSON-дамп значения.
    """
    col = _pick_col(df, names)
    if col is None:
        return pd.Series("", index=df.index)

    def _d(v):
        if isinstance(v, list):
            if not v:
                return ""
            if all(isinstance(x, str) and x for x in v):
                return ", ".join(v)
            try:
                return json.dumps(v, ensure_ascii=False, default=str)
            except Exception:  # noqa: BLE001
                return ""
        if v is None or (isinstance(v, float) and v != v):
            return ""
        s = str(v).strip()
        return "" if s in ("", "nan", "None") else s

    return col.map(_d)


def _stat_block_json(df: pd.DataFrame, block: str) -> pd.Series:
    """JSON-строка блока statistic.<block> (selected/past/comparison).

    Для реального ответа читается неуплощённый словарь из df["_raw"];
    для тестов/моков — сериализуем плоскую строку (те же значения).
    """
    rows = list(df["_raw"]) if "_raw" in df.columns else df.to_dict("records")
    vals = []
    for r in rows:
        if not isinstance(r, dict):
            vals.append("")
            continue
        st = r.get("statistic") if isinstance(r, dict) else None
        bl = st.get(block) if isinstance(st, dict) else None
        if not isinstance(bl, dict) or not bl:
            vals.append("")
            continue
        try:
            vals.append(json.dumps(bl, ensure_ascii=False, default=str))
        except Exception:  # noqa: BLE001
            vals.append("")
    return pd.Series(vals, index=df.index)


def _funnel_to_db(df: pd.DataFrame, from_, to_) -> pd.DataFrame:
    """Воронка WB -> схема funnel_metric.

    Артикул берётся напрямую из vendorCode реального ответа (product.vendorCode),
    иначе через кэш nmID->артикул; метрики читаются из statistic.selected.*.
    Устойчив к вариантам имён и к полному отсутствию идентификатора.
    """
    if df.empty:
        return df
    nm_map = _wb_nmid_to_article()

    nm_id = _pick_col(df, FUNNEL_NM_CANDIDATES)
    vendor = _pick_col(df, FUNNEL_VENDOR_CANDIDATES)

    def art(s):
        s = str(s)
        if not s or s == "nan":
            return ""
        return nm_map.get(s, s)

    if vendor is not None:
        article = vendor.astype(str).str.strip()
    else:
        article = (nm_id.astype(str) if nm_id is not None
                   else pd.Series("", index=df.index)).map(art)
    article = article.replace("nan", "")

    if nm_id is None and vendor is not None:
        logger.warning(
            "Воронка WB без колонки nmID, использую артикул продавца; колонки: %s",
            ", ".join(sorted(map(str, df.columns))),
        )

    nm_id_series = nm_id if nm_id is not None else pd.Series("", index=df.index)

    out = {"date_from": from_, "date_to": to_,
           "nm_id": nm_id_series.astype(str), "article": article}
    for key, cands in FUNNEL_COL_MAP.items():
        out[key] = _col_num(df, cands)
    for key, cands in FUNNEL_EXTRA_COL_MAP.items():
        out[key] = _col_num(df, cands)
    t_days = _col_num(df, FUNNEL_TIME_TO_READY["days"])
    t_hours = _col_num(df, FUNNEL_TIME_TO_READY["hours"])
    t_mins = _col_num(df, FUNNEL_TIME_TO_READY["mins"])
    out["time_to_ready_min"] = (t_days * 1440 + t_hours * 60 + t_mins).fillna(0)
    for key in ("subject_name", "brand_name", "title", "subject_id"):
        out[key] = _col_str(df, FUNNEL_EXTRA_COL_MAP[key])
    out["tags"] = _col_json(df, FUNNEL_EXTRA_COL_MAP["tags"])
    out["past_json"] = _stat_block_json(df, "past")
    out["comparison_json"] = _stat_block_json(df, "comparison")
    out["raw_json"] = _raw_json(df)
    return pd.DataFrame(out)


def _raw_json(df: pd.DataFrame) -> pd.Series:
    """Полный исходный объект товара (product + statistic, включая past/comparison).

    get_sales_funnel дополнительно кладёт неуплощённый словарь в колонку _raw;
    для моков/тестов — сериализуем плоскую строку (значения те же).
    """
    result_row = None
    if "_raw" in df.columns:
        result_row = df["_raw"]
    if result_row is None:
        result_row = pd.Series([dict(r) for r in df.to_dict("records")], index=df.index)
    def _dump(o):
        try:
            return json.dumps(o, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            return ""
    return result_row.apply(_dump)


# ----------------------------------------------------------- привязка провайдеров
def _wb_provider(with_fail_fast: bool = False) -> WbProvider:
    """Провайдер WB активного кабинета (по creds кабинета, иначе из .env)."""
    creds = active_credentials("wb")
    return provider_factory.get_wb_provider(
        with_fail_fast=with_fail_fast, credentials=creds or None
    )


# ----------------------------------------------------------- экспорт карточек (разбивка полей)


def expand_wb_card_export(df: pd.DataFrame) -> pd.DataFrame:
    """Разбивает dimensions/characteristics/subject из WB карточек в отдельные колонки.

    Используется только для Excel-экспорта, не влияет на БД.
    """
    if df is None or df.empty:
        return df
    out = df.copy()

    # --- dimensions dict -> колонки (мм / г)
    if "dimensions" in out.columns:
        dim = out["dimensions"]
        out["Длина, мм"] = dim.map(lambda d: (d.get("length") if isinstance(d, dict) else None))
        out["Ширина, мм"] = dim.map(lambda d: (d.get("width") if isinstance(d, dict) else None))
        out["Высота, мм"] = dim.map(lambda d: (d.get("height") if isinstance(d, dict) else None))
        out["Вес брутто, г"] = dim.map(lambda d: (d.get("weightGross") if isinstance(d, dict) else None))
        out["Вес нетто, г"] = dim.map(lambda d: (d.get("weightNet") if isinstance(d, dict) else None))

    # --- characteristics list-of-dicts -> колонка на имя
    if "characteristics" in out.columns:
        name_map = {}
        for items in out["characteristics"].dropna():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name", "")).strip()
                if name and name not in name_map:
                    name_map[name] = len(name_map) + 1  # preserving order
        if name_map:
            cols_sorted = list(name_map)
            top = cols_sorted[:150]

            def _char_value(items, col_name):
                if not isinstance(items, list):
                    return ""
                vals = []
                for item in items:
                    if not isinstance(item, dict) or item.get("name") != col_name:
                        continue
                    v = item.get("value")
                    if isinstance(v, list):
                        vals.extend(str(x) for x in v)
                    elif v is not None:
                        vals.append(str(v))
                return "; ".join(dict.fromkeys(vals))  # unique order

            for col_name in top:
                out[f"Хар-ка: {col_name}"] = out["characteristics"].map(
                    lambda items, cn=col_name: _char_value(items, cn)
                )
            if len(cols_sorted) > 150:
                other_cols = cols_sorted[150:]
                out["Другие характеристики"] = out["characteristics"].map(
                    lambda items, _oc=other_cols: "; ".join(
                        f"{item.get('name','')}: {item.get('value','')}"
                        for item in (items if isinstance(items, list) else [])
                        if isinstance(item, dict) and item.get("name") in _oc
                    ) or ""
                )

    # --- subject dict -> "Предмет" и "Предмет (родитель)"
    if "subject" in out.columns:
        subj = out["subject"]
        out["Предмет"] = subj.map(lambda s: (str(s.get("name") or "") if isinstance(s, dict) else str(s or "")))
        out["Предмет (родитель)"] = subj.map(
            lambda s: (str(s.get("parentName") or "") if isinstance(s, dict) else "")
        )

    return out


def _oz_provider(with_fail_fast: bool = False) -> OzonProvider:
    creds = active_credentials("ozon")
    return provider_factory.get_oz_provider(
        with_fail_fast=with_fail_fast, credentials=creds or None
    )


_WB_MESSAGES = {
    401: "Токен WB API невалиден или истёк. Проверьте WB_API_KEY в .env.",
    403: "У токена WB нет прав на этот отчёт (финансовый отчёт требует отдельный токен продавца).",
    429: "Превышен лимит запросов к WB API, попробуйте позже.",
}
_OZ_MESSAGES = {
    401: "Ozon API: неверный Client-Id или Api-Key. Проверьте OZON_CLIENT_ID / OZON_API_KEY в .env.",
    403: "Ozon API: нет доступа к запрошенным данным (проверьте права ключа).",
    429: "Превышен лимит запросов к Ozon API, попробуйте позже.",
}


def _market_error(e: Exception, market: str, messages: dict):
    """Обобщённые ошибки в статусный HTTPException с понятным текстом."""
    if isinstance(e, MarketError):
        raise HTTPException(status_code=e.status_code, detail=str(e))
    if isinstance(e, requests.RequestException):
        err = translate_request_error(market, e, messages)
        raise HTTPException(status_code=err.status_code, detail=str(err))
    if isinstance(e, RuntimeError):
        raise HTTPException(status_code=502, detail=str(e))
    if isinstance(e, ValueError):
        raise HTTPException(status_code=400, detail=str(e))
    raise HTTPException(status_code=502, detail=str(e))


def wb_error(e: Exception):
    """Превращает исключение вызова WB API в понятный HTTP-ответ."""
    _market_error(e, "wb", _WB_MESSAGES)


def oz_error(e: Exception):
    """Превращает исключение вызова Ozon API в понятный HTTP-ответ."""
    _market_error(e, "ozon", _OZ_MESSAGES)


# ------------------------------------------------------------- pull_* (общий слой)
def pull_wb_cards(db, provider: Optional[WbProvider] = None, write_db: bool = True) -> dict:
    prov = provider or _wb_provider()
    df = prov.get_cards()
    n = 0
    n_nm = 0
    n_sk = 0
    if write_db:
        progress_service.report(stage="запись в БД")
        pdf = df.rename(columns={"vendorCode": "article", "title": "name", "skus": "barcode"})
        n = sync_service.upsert_products(db, pdf)
        n_nm = sync_service.upsert_nm_articles(db, df)
        mdf = sync_service.normalize_marketplace_cards(df)
        n_sk = sync_service.upsert_marketplace_cards(db, mdf, "wb") if mdf is not None else 0
        _wb_nm_cache_reset()
    total_db = n + n_nm + n_sk
    sync_service.record_api_pull(db, "wb", "cards", len(df), total_db, "сегодня")
    return {"df": df, "count": n if write_db else len(df), "db_rows": total_db,
            "rows": len(df), "window": "сегодня"}


def pull_wb_stock(db, provider: Optional[WbProvider] = None, write_db: bool = True) -> dict:
    prov = provider or _wb_provider()
    raw = prov.get_stock_report()
    n = 0
    if not raw.empty:
        if "vendorCode" not in raw.columns:
            nm_map = _wb_nmid_to_article()
        else:
            nm_map = {}
        qty = pd.to_numeric(
            raw["quantity"] if "quantity" in raw.columns else raw.get("quantityFull", 0),
            errors="coerce",
        ).fillna(0).astype(int)
        qty_full = pd.to_numeric(
            raw["quantityFull"] if "quantityFull" in raw.columns else qty,
            errors="coerce",
        ).fillna(0).astype(int)
        in_way = pd.to_numeric(raw.get("inWayToClient", 0), errors="coerce").fillna(0) \
            + pd.to_numeric(raw.get("inWayFromClient", 0), errors="coerce").fillna(0)
        in_way = in_way.astype(int)
        sdf = pd.DataFrame({
            "date": str(date.today()),
            "article": raw.apply(
                lambda r: nm_map.get(str(r.get("nmId")), str(r.get("vendorCode", r.get("nmId", ""))).strip()),
                axis=1,
            ),
            "warehouse": raw["warehouseName"].astype(str),
            "chrt_id": raw["chrtId"].astype(str).str.strip() if "chrtId" in raw.columns else "",
            "quantity": qty,
            "quantity_full": qty_full,
            "in_way": in_way,
        })
        if "chrtId" in raw.columns:
            dims = _wb_card_dims(db)
            sdf["size"] = sdf["chrt_id"].map(lambda c: dims.get(c, ("", ""))[0])
            sdf["barcode"] = sdf["chrt_id"].map(lambda c: dims.get(c, ("", ""))[1])
        else:
            sdf["size"] = ""
            sdf["barcode"] = ""
        sdf["article"] = sdf["article"].astype(str).str.strip()
        sdf = sdf[["date", "article", "chrt_id", "size", "barcode", "warehouse", "quantity", "quantity_full", "in_way"]]
        if write_db:
            progress_service.report(stage="запись в БД")
            n = sync_service.upsert_stocks(db, sdf, "wb")
    else:
        sdf = raw
    sync_service.record_api_pull(db, "wb", "stock", len(sdf), n, "сегодня")
    return {"df": sdf, "count": n if write_db else len(sdf), "db_rows": n,
            "rows": len(sdf), "window": "сегодня"}


def pull_wb_funnel(db, from_, to_, provider: Optional[WbProvider] = None,
                   write_db: bool = True) -> dict:
    prov = provider or _wb_provider()
    df = prov.get_sales_funnel(from_, to_)
    n = 0
    if write_db and not df.empty:
        progress_service.report(stage="запись в БД")
        fdf = _funnel_to_db(df, from_, to_)
        n = sync_service.upsert_funnel(db, fdf)
    window = f"{from_.isoformat()} — {to_.isoformat()}"
    sync_service.record_api_pull(db, "wb", "funnel", len(df), n, window)
    return {"df": df, "count": n if write_db else len(df), "db_rows": n, "rows": len(df), "window": window}


def pull_wb_prices(db, provider: Optional[WbProvider] = None, write_db: bool = True) -> dict:
    prov = provider or _wb_provider()
    df = prov.get_prices()
    n = 0
    if write_db and not df.empty:
        progress_service.report(stage="запись в БД")
        n = sync_service.upsert_price_snapshots(db, df)
    sync_service.record_api_pull(db, "wb", "prices", len(df), n, "сейчас")
    return {"df": df, "count": n if write_db else len(df), "db_rows": n,
            "rows": len(df), "window": "сейчас"}


def pull_wb_storage(db, days: int = 7, provider: Optional[WbProvider] = None,
                    write_db: bool = True) -> dict:
    prov = provider or _wb_provider()
    df = prov.get_storage_cost(number_last_days=days)
    n = 0
    if write_db and not df.empty:
        n = sync_service.upsert_storage_costs(db, df)
    sync_service.record_api_pull(db, "wb", "storage", len(df), n, f"{days} дней")
    return {"df": df, "count": n if write_db else len(df), "db_rows": n,
            "rows": len(df), "window": f"{days} дней"}


def pull_wb_promotions(db, provider: Optional[WbProvider] = None,
                       write_db: bool = True) -> dict:
    """Акции WB (Календарь акций): окно [−30 дн, +90 дн], доступные для участия.

    Строки и details пишутся в wb_promotions; лог в api_pulls (kind=promotions).
    """
    prov = provider or _wb_provider()
    from_ = date.today() - timedelta(days=30)
    to_ = date.today() + timedelta(days=90)
    df = prov.get_promotions(from_, to_, all_promo=False)
    n = 0
    if write_db and not df.empty:
        ids = [
            int(i) for i in pd.to_numeric(
                df["id"] if "id" in df.columns else [], errors="coerce"
            ).dropna().tolist()
            if int(i) > 0
        ]
        det = prov.get_promotion_details(ids) if ids else pd.DataFrame()
        n = sync_service.upsert_promotions(db, df, det)
    window = f"{from_.isoformat()} — {to_.isoformat()}"
    sync_service.record_api_pull(db, "wb", "promotions", len(df), n, window)
    return {"df": df, "count": n if write_db else len(df), "db_rows": n,
            "rows": len(df), "window": window}


def pull_wb_sales(db, from_, to_, provider: Optional[WbProvider] = None,
                  write_db: bool = True) -> dict:
    """Продажи WB пересчитываются из детализации (finance-API): статистический
    отчёт v5 (statistics-api/reportDetailByPeriod) отключён WB 15.07.2026."""
    df = sync_service.sales_from_detail_df(db, from_, to_)
    n = 0
    if write_db and not df.empty:
        progress_service.report(stage="пересчёт из детализации")
        n = sync_service.upsert_sales(db, df, "wb", source="v5")
    window = f"{from_.isoformat()} — {to_.isoformat()}"
    sync_service.record_api_pull(db, "wb", "sales", len(df), n, window)
    return {"df": df, "count": n if write_db else len(df), "db_rows": n, "rows": len(df), "window": window}


def pull_wb_detail(db, from_, to_, provider: Optional[WbProvider] = None,
                   write_db: bool = True) -> dict:
    prov = provider or _wb_provider()
    df = prov.get_sales_detail(from_, to_)
    n = 0
    if write_db and not df.empty:
        progress_service.report(stage="запись в БД")
        rdf = sync_service.normalize_wb_detail(df, source="api")
        if rdf is not None and not rdf.empty:
            sync_service.upsert_wb_detail_rows(db, rdf, source="api")
            n = sync_service.rebuild_sales_from_detail(
                db, sale_from=from_, sale_to=to_, source="v5")
    window = f"{from_.isoformat()} — {to_.isoformat()}"
    sync_service.record_api_pull(db, "wb", "detail", len(df), n, window)
    return {"df": df, "count": n if write_db else len(df), "db_rows": n, "rows": len(df), "window": window}


def pull_oz_cards(db, provider: Optional[OzonProvider] = None, write_db: bool = True) -> dict:
    prov = provider or _oz_provider()
    df = prov.get_cards()
    n = 0
    n_sk = 0
    if write_db and not df.empty:
        progress_service.report(stage="запись в БД")
        pdf = df.rename(columns={
            "Артикул": "article", "Offer ID": "article",
            "Название товара": "name", "Name": "name",
            "Бренд": "brand", "Category": "brand",
            "Штрихкод (Серийный номер / EAN)": "barcode", "Штрихкод": "barcode",
            "Barcode": "barcode",
        })
        if "barcode" not in pdf.columns and "SKU" in pdf.columns:
            pdf = pdf.rename(columns={"SKU": "barcode"})
        n = sync_service.upsert_products(db, pdf)
        mdf = sync_service.normalize_oz_cards(df)
        if mdf is not None and write_db:
            mdf = sync_service.enrich_oz_cards_with_wb_nm(db, mdf)
        n_sk = sync_service.upsert_marketplace_cards(db, mdf, "ozon") if mdf is not None else 0
    total_db = n + n_sk
    sync_service.record_api_pull(db, "ozon", "cards", len(df), total_db, "")
    return {"df": df, "count": total_db if write_db else len(df), "db_rows": total_db,
            "rows": len(df), "window": ""}


def _oz_marketplace_cards(df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """Ozon-карточки → схема marketplace_cards (для тегов каталога и экспорта)."""
    if df is None or df.empty:
        return None
    cols = {str(c).strip().lower(): c for c in df.columns if isinstance(c, str)}

    def series(*names) -> pd.Series:
        for n in names:
            if n.lower() in cols:
                return df[cols[n.lower()]]
        return pd.Series("", index=df.index)

    pid = series("Ozon Product ID", "Product ID", "product_id").fillna("").astype(str).str.strip()
    out = pd.DataFrame({
        "chrt_id": pid,
        "nm_id": pid,
        "vendor_code": series("Offer ID", "Артикул", "Артикул продавца").fillna("").astype(str).str.strip(),
        "brand": series("Бренд", "Category").fillna("").astype(str).str.strip(),
        "subject": series("Category", "Бренд").fillna("").astype(str).str.strip(),
        "size": "",
        "barcode": series("Штрихкод (Серийный номер / EAN)", "Штрихкод", "Barcode", "SKU").map(sync_service._first_barcode),
        "volume_l": 0.0,
        "composition": "",
        "name": series("Name", "Название товара").fillna("").astype(str).str.strip(),
    })
    out = out[out["chrt_id"] != ""]
    return out if not out.empty else None


def pull_catalog(db, overwrite: bool = False, write_db: bool = True,
                 provider_wb: Optional[WbProvider] = None,
                 provider_oz: Optional[OzonProvider] = None) -> dict:
    """Обновляет общий каталог «Наш склад → Товары» из карточек WB и Ozon.

    Тянет get_cards() у обоих провайдеров, пишет marketplace_cards/nm_articles
    (существующая инфраструктура: размеры остатков, карта nmID→артикул) и
    сливает карточки в общий каталог (products + product_sizes +
    product_aliases) через sync_catalog_from_cards.
    """
    prov_wb = provider_wb or _wb_provider()
    prov_oz = provider_oz or _oz_provider()
    wb_df = prov_wb.get_cards()
    oz_df = prov_oz.get_cards()
    wb_rows = 0 if wb_df is None else int(len(wb_df))
    oz_rows = 0 if oz_df is None else int(len(oz_df))
    report = None
    if write_db:
        if wb_rows:
            sync_service.upsert_nm_articles(db, wb_df)
            mdf = sync_service.normalize_marketplace_cards(wb_df)
            if mdf is not None:
                sync_service.upsert_marketplace_cards(db, mdf, "wb")
            _wb_nm_cache_reset()
        if oz_rows:
            mdf = _oz_marketplace_cards(oz_df)
            if mdf is not None:
                mdf = sync_service.enrich_oz_cards_with_wb_nm(db, mdf)
                sync_service.upsert_marketplace_cards(db, mdf, "ozon")
        wc = sync_service.normalize_catalog_card(wb_df, "wb")
        oc = sync_service.normalize_catalog_card(oz_df, "ozon")
        frames = [f for f in (wc, oc) if f is not None and not f.empty]
        cards_df = pd.concat(frames, ignore_index=True) if frames else None
        report = sync_service.sync_catalog_from_cards(db, cards_df, overwrite)
        sync_service.record_api_pull(db, "catalog", "cards", wb_rows + oz_rows,
                                     int(report["rows"]), "сегодня")
    return {"rows": wb_rows + oz_rows, "wb_rows": wb_rows, "oz_rows": oz_rows,
            "db_rows": int(report["rows"]) if report else 0, "report": report}


def _enrich_oz_stock_barcode(db, sdf: pd.DataFrame):
    """Проставляет правильный штрихкод и размер остаткам Ozon из карточек.

    API остатков Ozon не возвращает штрихкод/размер, поэтому берём их из
    marketplace_cards (ozon) по совпадению vendor_code (артикул). Если у артикула
    несколько карточек — берём первую попавшуюся (все SKU-размеры обычно имеют
    одинаковый EAN артикула).
    """
    if sdf is None or sdf.empty:
        return
    mp_id = sync_service.marketplace_id(db, "ozon")
    rows = db.execute(
        select(models.MarketplaceCard.vendor_code, models.MarketplaceCard.size,
               models.MarketplaceCard.barcode)
        .where(models.MarketplaceCard.marketplace_id == mp_id,
               models.MarketplaceCard.barcode != "")
    ).all()
    info = {}
    for vc, size, bc in rows:
        if not vc:
            continue
        info.setdefault(str(vc).strip().lower(), (size or "", bc or ""))
    if not info:
        return
    keys = sdf["article"].astype(str).str.strip().str.lower()
    sizes = keys.map(lambda k: info.get(k, (None, None))[0])
    barcodes = keys.map(lambda k: info.get(k, (None, None))[1])
    sdf["size"] = sizes.fillna("")
    sdf["barcode"] = barcodes.fillna("")
    logger.info("[ozon stock] штрихкод/размер проставлены %d строкам из %d",
                int((keys.isin(info.keys())).sum()), len(sdf))


def pull_oz_stock(db, provider: Optional[OzonProvider] = None, write_db: bool = True) -> dict:
    prov = provider or _oz_provider()
    df = prov.get_stock()
    n = 0
    if write_db and not df.empty:
        progress_service.report(stage="запись в БД")
        free = pd.to_numeric(df.get("free_to_sell_amount", 0), errors="coerce").fillna(0).astype(int) \
            if "free_to_sell_amount" in df.columns else pd.Series(0, index=df.index)
        reserved = pd.to_numeric(df.get("reserved_amount", 0), errors="coerce").fillna(0).astype(int) \
            if "reserved_amount" in df.columns else pd.Series(0, index=df.index)
        promised = pd.to_numeric(df.get("promised_amount", 0), errors="coerce").fillna(0).astype(int) \
            if "promised_amount" in df.columns else pd.Series(0, index=df.index)
        sdf = pd.DataFrame({
            "date": str(date.today()),
            "article": df.apply(
                lambda r: str(r.get("item_code") or r.get("sku") or "").strip(), axis=1,
            ),
            "warehouse": df["warehouse_name"].astype(str),
            "quantity": free,
            "quantity_full": free + reserved + promised,
            "in_way": promised,
        })
        _enrich_oz_stock_barcode(db, sdf)
        n = sync_service.upsert_stocks(db, sdf, "ozon")
    sync_service.record_api_pull(db, "ozon", "stock", len(df), n, "сегодня")
    return {"df": df, "count": n if write_db else len(df), "db_rows": n, "rows": len(df), "window": "сегодня"}


def pull_oz_prices(db, provider: Optional[OzonProvider] = None, write_db: bool = True) -> dict:
    prov = provider or _oz_provider()
    df = prov.get_prices()
    n = 0
    if write_db and not df.empty:
        progress_service.report(stage="запись в БД")
        n = sync_service.upsert_ozon_price_snapshots(db, df)
    sync_service.record_api_pull(db, "ozon", "prices", len(df), n, "сейчас")
    return {"df": df, "count": n if write_db else len(df), "db_rows": n,
            "rows": len(df), "window": "сейчас"}


def pull_oz_realization(db, month: int, year: int, provider: Optional[OzonProvider] = None,
                        write_db: bool = True) -> dict:
    prov = provider or _oz_provider()
    df = prov.get_realization(month, year)
    n = 0
    if write_db:
        sdf = sync_service.normalize_ozon_realization(df)
        n = sync_service.upsert_sales(db, sdf, "ozon", source="ozon") if sdf is not None else 0
    window = f"{year:04d}-{month:02d}"
    sync_service.record_api_pull(db, "ozon", "realization", len(df), n, window)
    return {"df": df, "count": n if write_db else len(df), "db_rows": n, "rows": len(df), "window": window}


def _month_range(from_, to_):
    """Список (year, month) месяцев, покрывающих интервал [from_, to_]."""
    months = []
    y, m = from_.year, from_.month
    while (y, m) <= (to_.year, to_.month):
        months.append((y, m))
        m += 1
        if m == 13:
            m, y = 1, y + 1
    return months


def pull_oz_realizations(db, from_, to_, provider: Optional[OzonProvider] = None,
                         write_db: bool = True) -> dict:
    """Реализация за месяцы интервала [from_, to_] (Ozon API отдаёт помесячно).

    Отчёт реализации датирован концом месяца, поэтому строки не фильтруются
    по дням окна — окно выбирает набор месяцев.
    """
    prov = provider or _oz_provider()
    frames = []
    for yy, mm in _month_range(from_, to_):
        df = prov.get_realization(mm, yy)
        if df is not None and not df.empty:
            frames.append(df)
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    n = 0
    if write_db and not df.empty:
        sdf = sync_service.normalize_ozon_realization(df)
        n = sync_service.upsert_sales(db, sdf, "ozon", source="ozon") if sdf is not None else 0
    window = f"{from_.isoformat()}..{to_.isoformat()}"
    sync_service.record_api_pull(db, "ozon", "realization", len(df), n, window)
    return {"df": df, "count": n if write_db else len(df), "db_rows": n, "rows": len(df), "window": window}


def pull_oz_cashflow(db, from_, to_, provider: Optional[OzonProvider] = None,
                     write_db: bool = True) -> dict:
    prov = provider or _oz_provider()
    df = prov.get_cash_flow(from_, to_)
    n = 0
    if write_db and not df.empty:
        rdf = sync_service.normalize_ozon_cashflow(df)
        if rdf is not None and not rdf.empty:
            n = sync_service.upsert_ozon_cashflows(db, rdf)
    window = f"{from_.isoformat()} — {to_.isoformat()}"
    sync_service.record_api_pull(db, "ozon", "cashflow", len(df), n, window)
    return {"df": df, "count": n if write_db else len(df), "db_rows": n, "rows": len(df), "window": window}


def pull_oz_detail(db, from_, to_, provider: Optional[OzonProvider] = None,
                   write_db: bool = True) -> dict:
    prov = provider or _oz_provider()
    df = prov.get_sales_detail(from_, to_)
    n = 0
    if write_db and not df.empty:
        progress_service.report(stage="запись в БД")
        rdf = sync_service.normalize_ozon_detail(df, source="api")
        if rdf is not None and not rdf.empty:
            n = sync_service.upsert_ozon_detail_rows(db, rdf, source="api")
    window = f"{from_.isoformat()} — {to_.isoformat()}"
    sync_service.record_api_pull(db, "ozon", "detail", len(df), n, window)
    return {"df": df, "count": n if write_db else len(df), "db_rows": n,
            "rows": len(df), "window": window}


def pull_oz_buyout(db, from_, to_, provider: Optional[OzonProvider] = None,
                   write_db: bool = True) -> dict:
    prov = provider or _oz_provider()
    df = prov.get_buyout(from_, to_)
    n = 0
    if write_db and not df.empty:
        rdf = sync_service.normalize_ozon_buyout(df)
        if rdf is not None and not rdf.empty:
            n = sync_service.upsert_ozon_buyouts(db, rdf)
    window = f"{from_.isoformat()} — {to_.isoformat()}"
    sync_service.record_api_pull(db, "ozon", "buyout", len(df), n, window)
    return {"df": df, "count": n if write_db else len(df), "db_rows": n,
            "rows": len(df), "window": window}


def pull_oz_placements(db, from_, to_, provider: Optional[OzonProvider] = None,
                       write_db: bool = True) -> dict:
    prov = provider or _oz_provider()
    df = prov.get_placement(from_, to_)
    n = 0
    if write_db and not df.empty:
        rdf = sync_service.normalize_ozon_placement(df)
        if rdf is not None and not rdf.empty:
            n = sync_service.upsert_ozon_placements(db, rdf)
    window = f"{from_.isoformat()} — {to_.isoformat()}"
    sync_service.record_api_pull(db, "ozon", "placement", len(df), n, window)
    return {"df": df, "count": n if write_db else len(df), "db_rows": n,
            "rows": len(df), "window": window}


def pull_oz_accrual(db, from_, to_, provider: Optional[OzonProvider] = None,
                    write_db: bool = True) -> dict:
    """Начисления по товарам (аккруалы) за окно: /v1/finance/accrual/by-day.

    offer_id дорезолвивается в два прохода: сначала по детализации (SKU продаж),
    затем для оставшихся SKU — картой SKU → Offer ID из /v3/product/info/list
    (нужна для начислений по товарам без продаж в загруженной детализации).
    """
    prov = provider or _oz_provider()
    df = prov.get_accrual(from_, to_)
    n = 0
    if write_db and not df.empty:
        rdf = sync_service.normalize_ozon_accrual(df)
        if rdf is not None and not rdf.empty:
            missing = rdf[(rdf["offer_id"] == "") & (rdf["sku"] != "")]
            if not missing.empty:
                try:
                    smap = prov.get_sku_map()
                except Exception:  # noqa: BLE001 — карта справочная
                    smap = None
                if smap is not None and not smap.empty:
                    pairs = {str(r["sku"]).strip(): str(r["offer_id"]).strip()
                             for r in smap.to_dict("records")
                             if str(r.get("offer_id") or "").strip()}
                    if pairs:
                        keys = rdf["sku"].astype(str).str.strip()
                        filled = rdf["offer_id"].replace("", pd.NA).fillna(keys.map(pairs)).fillna("")
                        rdf["offer_id"] = filled
            n = sync_service.upsert_ozon_accruals(db, rdf)
    window = f"{from_.isoformat()} — {to_.isoformat()}"
    sync_service.record_api_pull(db, "ozon", "accrual", len(df), n, window)
    return {"df": df, "count": n if write_db else len(df), "db_rows": n,
            "rows": len(df), "window": window}


# ------------------------------------------------------------------ планы обновления
_KIND_LABELS = {
    "wb": [
        ("cards", "Карточки товара"), ("stock", "Остатки"),
        ("funnel", "Воронка продаж"), ("sales", "Продажи"),
        ("prices", "Цены"), ("storage", "Хранение"),
        ("promotions", "Акции (календарь)"),
    ],
    "ozon": [
        ("cards", "Карточки товара"), ("stock", "Остатки"),
        ("prices", "Цены"), ("realization", "Реализация"),
        ("cashflow", "Движение средств"),
        ("placement", "Размещение (хранение)"),
    ],
}


def _plan_steps(api: str, include_detail: bool, date_from=None, date_to=None,
                cab_id: Optional[int] = None):
    """Список (kind, label, callable) для массового обновления маркетплейса."""
    from_, to_ = resolve_range(date_from, date_to)
    steps = []
    pkey_wb = (cab_id, "wb")
    pkey_oz = (cab_id, "ozon")
    for kind, label in _KIND_LABELS[api]:
        if api == "wb":
            if kind == "funnel":
                fn = lambda db: pull_wb_funnel(db, from_, to_, provider=_BULK_PROV[pkey_wb])  # noqa: E731
            elif kind == "sales":
                fn = lambda db: pull_wb_sales(db, from_, to_, provider=_BULK_PROV[pkey_wb])  # noqa: E731
            elif kind == "storage":
                fn = lambda db: pull_wb_storage(db, 7, provider=_BULK_PROV[pkey_wb])  # noqa: E731
            else:
                fn = (lambda k: lambda db: _BULK_PULLS["wb"][k](db, provider=_BULK_PROV[pkey_wb]))(kind)
        else:
            if kind == "realization":
                fn = lambda db: pull_oz_realizations(db, from_, to_, provider=_BULK_PROV[pkey_oz])  # noqa: E731
            elif kind == "cashflow":
                fn = lambda db: pull_oz_cashflow(db, from_, to_, provider=_BULK_PROV[pkey_oz])  # noqa: E731
            elif kind == "placement":
                fn = lambda db: pull_oz_placements(db, from_, to_, provider=_BULK_PROV[pkey_oz])  # noqa: E731
            else:
                fn = (lambda k: lambda db: _BULK_PULLS["ozon"][k](db, provider=_BULK_PROV[pkey_oz]))(kind)
        steps.append((kind, label, fn))
    if include_detail:
        if api == "wb":
            steps.append(("detail", "Детализация (finance)",
                          lambda db: pull_wb_detail(db, from_, to_, provider=_BULK_PROV[pkey_wb])))
        else:
            steps.append(("detail", "Детализация продаж",
                          lambda db: pull_oz_detail(db, from_, to_, provider=_BULK_PROV[pkey_oz])))
            steps.append(("buyout", "Выкупы",
                          lambda db: pull_oz_buyout(db, from_, to_, provider=_BULK_PROV[pkey_oz])))
            steps.append(("accrual", "Начисления (аккруалы)",
                          lambda db: pull_oz_accrual(db, from_, to_, provider=_BULK_PROV[pkey_oz])))
    return steps


_BULK_PULLS = {
    "wb": {"cards": pull_wb_cards, "stock": pull_wb_stock, "prices": pull_wb_prices,
           "promotions": pull_wb_promotions},
    "ozon": {"cards": pull_oz_cards, "stock": pull_oz_stock, "prices": pull_oz_prices},
}


# ------------------------------------------------------------------ фоновые задания
# Ключи очередей — (cab_id, api): у каждого кабинета свои обновления и
# провайдеры (creds кабинета). cab_id=None — «без кабинета» (скрипты/тесты).
_RUNNING = {}  # (cab_id, api) -> job_id
_PENDING = {}  # (cab_id, api) -> job_id
_LOCK = threading.Lock()
_SEQ = 0
_JOBS = {}  # job_id -> state dict
_BULK_PROV = {}  # (cab_id, api) -> provider, выставляется воркером перед шагами


def _next_id() -> int:
    global _SEQ
    _SEQ += 1
    return _SEQ


def _cab_info(db, cab_id: Optional[int]) -> Optional[object]:
    if cab_id is None:
        return None
    return cabinets.info_by_id(db, cab_id)


def start_refresh(api: str, include_detail: bool = False,
                  date_from=None, date_to=None,
                  cab_id: Optional[int] = None) -> dict:
    """Запускает фоновое обновление. Возвращает {job_id, queued}.

    cab_id=None — активный кабинет запроса (contextvar get_db) или «как раньше»
    (public, без кабинета) для скриптов/тестов.
    """
    global _RUNNING, _PENDING
    if cab_id is None:
        active = cabinets.get_active()
        cab_id = active.id if active else None
    with _LOCK:
        if _PENDING.get((cab_id, api)):
            return {"job_id": None, "queued": False, "rejected": "уже поставлено в очередь"}
        job_id = _next_id()
        steps = _plan_steps(api, include_detail, date_from, date_to, cab_id)
        job = {
            "id": job_id, "api": api, "cab_id": cab_id, "cab_name": "",
            "include_detail": bool(include_detail),
            "date_from": date_from, "date_to": date_to,
            "status": "running" if not _RUNNING.get((cab_id, api)) else "queued",
            "started_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "finished_at": None,
            "ok": 0, "failed": 0,
            "steps": [{"kind": k, "label": lbl, "status": "pending",
                       "rows": 0, "db_rows": 0, "window": "", "error": ""}
                      for k, lbl, _ in steps],
        }
        _JOBS[job_id] = job
        queued = bool(_RUNNING.get((cab_id, api)))
        if queued:
            _PENDING[(cab_id, api)] = job_id
            return {"job_id": job_id, "queued": True}
        _RUNNING[(cab_id, api)] = job_id
        threading.Thread(
            target=_refresh_worker,
            args=(api, job_id, date_from, date_to, steps, cab_id),
            daemon=True,
        ).start()
        return {"job_id": job_id, "queued": False}


def _refresh_worker(api: str, job_id: int, date_from, date_to, steps,
                    cab_id: Optional[int]):
    from app.services.cabinets import apply_search_path, pin_search_path

    db = SessionLocal()
    job = _JOBS[job_id]
    job["status"] = "running"
    run = None
    results = []
    fatal = None
    try:
        info = _cab_info(db, cab_id)
        if info is not None:
            apply_search_path(db, info.schema)
            # commit внутри шагов отдаёт соединение в пул и откатывает SET —
            # без pin следующий шаг/refresh падал на «refresh_runs не существует»
            pin_search_path(db, info.schema)
            job["cab_name"] = info.name
        run = models.RefreshRun(api=api, status="running", results="[]")
        db.add(run)
        db.commit()
        db.refresh(run)
        job["run_id"] = run.id
        creds_wb = info.creds_for("wb") if info else {}
        creds_oz = info.creds_for("ozon") if info else {}
        prov_wb = provider_factory.get_wb_provider(
            with_fail_fast=True, credentials=creds_wb or None)
        prov_oz = provider_factory.get_oz_provider(
            with_fail_fast=True, credentials=creds_oz or None)
        _BULK_PROV[(cab_id, "wb")] = prov_wb
        _BULK_PROV[(cab_id, "ozon")] = prov_oz
        for kind, label, fn in steps:
            step = next(s for s in job["steps"] if s["kind"] == kind)
            step["status"] = "running"
            try:
                res = fn(db)
                step["status"] = "ok"
                step["rows"] = int(res["rows"])
                step["db_rows"] = int(res["db_rows"])
                step["window"] = res["window"]
                job["ok"] += 1
                results.append({
                    "kind": kind, "status": "ok", "rows": int(res["rows"]),
                    "db_rows": int(res["db_rows"]), "window": res["window"], "error": "",
                })
            except Exception as e:  # noqa: BLE001
                err = str(e)[:300]
                step["status"] = "failed"
                step["error"] = err
                job["failed"] += 1
                results.append({"kind": kind, "status": "failed", "rows": 0,
                                "db_rows": 0, "window": "", "error": err})
    except Exception as e:  # noqa: BLE001 — падение самого воркера не должно замораживать очередь
        fatal = str(e)[:300]
        for step in job["steps"]:
            if step["status"] == "pending" or step["status"] == "running":
                step["status"] = "failed"
                step["error"] = fatal
        job["failed"] = job["failed"] or 1
    finally:
        job["status"] = "done"
        job["finished_at"] = datetime.now().isoformat(sep=" ", timespec="seconds")
        if run is not None:
            if fatal is not None:
                run.status = "failed"
                results.append({"kind": "_", "status": "failed", "rows": 0,
                                "db_rows": 0, "window": "", "error": fatal})
            else:
                run.status = "ok" if job["failed"] == 0 else \
                    "partial" if job["ok"] > 0 else "failed"
            run.finished_at = datetime.now()
            run.results = json.dumps(results, ensure_ascii=False)
            try:
                db.commit()
            except Exception:  # noqa: BLE001
                db.rollback()
        try:
            db.execute(text("RESET search_path"))
        except Exception:  # noqa: BLE001
            pass
        db.close()
        _advance_queue(api, cab_id)


def _advance_queue(api: str, cab_id: Optional[int]):
    with _LOCK:
        _RUNNING[(cab_id, api)] = None
        next_id = _PENDING.get((cab_id, api))
        _PENDING[(cab_id, api)] = None
        if next_id:
            j = _JOBS[next_id]
            j["status"] = "running"
            _RUNNING[(cab_id, api)] = next_id
            threading.Thread(
                target=_refresh_worker,
                args=(api, next_id, j["date_from"], j["date_to"],
                      _plan_steps(api, j["include_detail"], j["date_from"],
                                  j["date_to"], cab_id), cab_id),
                daemon=True,
            ).start()


def job_state(job_id: int) -> Optional[dict]:
    job = _JOBS.get(job_id)
    if job is None:
        return None
    return {k: job[k] for k in ("id", "api", "cab_id", "cab_name",
                                "include_detail", "status",
                                "started_at", "finished_at", "ok", "failed", "steps")}


def active_jobs() -> list:
    out = []
    for job in _JOBS.values():
        out.append({k: job[k] for k in ("id", "api", "cab_id", "cab_name",
                                        "include_detail", "status",
                                        "started_at", "finished_at", "ok", "failed")})
    return sorted(out, key=lambda j: j["id"], reverse=True)[:20]


def history(db, limit: int = 10) -> list:
    rows = db.execute(
        select(models.RefreshRun).order_by(models.RefreshRun.id.desc()).limit(limit)
    ).scalars().all()
    out = []
    for r in rows:
        try:
            results = json.loads(r.results or "[]")
        except json.JSONDecodeError:
            results = []
        out.append({
            "id": r.id, "api": r.api, "status": r.status,
            "started_at": r.started_at.isoformat(sep=" ", timespec="seconds") if r.started_at else None,
            "finished_at": r.finished_at.isoformat(sep=" ", timespec="seconds") if r.finished_at else None,
            "results": results,
        })
    return out