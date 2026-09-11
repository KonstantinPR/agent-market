"""Массовое обновление данных из API маркетплейсов + общий слой выгрузки.

Содержит:
- pull_* функции: «спасти из панельного скачивания» — единый код записи в БД,
  который используют и хендлеры панелей, и фоновое «Обновить WB / Ozon».
- фоновые задания (threading): последовательно по видам, ошибка вида не
  блокирует остальные; история запусков пишется в refresh_runs.
"""
import json
import threading
import time
from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd
import requests
from fastapi import HTTPException
from sqlalchemy import select

from app import models
from app.config import settings
from app.database import SessionLocal
from app.providers import factory as provider_factory
from app.providers.errors import (MarketError, OzonApiError, WbApiError,
                                  translate_request_error)
from app.providers.ozon import OzonProvider
from app.providers.wb import WbProvider
from app.services import sync as sync_service
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


def _funnel_to_db(df: pd.DataFrame, from_, to_) -> pd.DataFrame:
    """Воронка WB -> схема funnel_metric (артикул через nmID->sa_name кэш)."""
    if df.empty:
        return df
    nm_map = _wb_nmid_to_article()

    def art(s):
        s = str(s)
        return nm_map.get(s, s)

    return pd.DataFrame({
        "date_from": from_,
        "date_to": to_,
        "nm_id": df["nmID"].astype(str),
        "article": df["nmID"].astype(str).map(art),
        "views": _col_num(df, ["viewsCount"]),
        "opens": _col_num(df, ["openCardCount"]),
        "adds": _col_num(df, ["addToCartCount"]),
        "orders": _col_num(df, ["orderCount", "ordersCount"]),
        "cancelled": _col_num(df, ["cancelCount", "cancelledOrdersCount"]),
        "avg_price": _col_num(df, ["avgPrice", "orderAvgPrice"]),
        "revenue": _col_num(df, ["revenue", "salesSum"]),
    })


# ----------------------------------------------------------- привязка провайдеров
def _wb_provider(with_fail_fast: bool = False) -> WbProvider:
    return provider_factory.get_wb_provider(with_fail_fast=with_fail_fast)


def _oz_provider(with_fail_fast: bool = False) -> OzonProvider:
    return provider_factory.get_oz_provider(with_fail_fast=with_fail_fast)


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
    if write_db:
        pdf = df.rename(columns={"vendorCode": "article", "title": "name", "skus": "barcode"})
        n = sync_service.upsert_products(db, pdf)
        n_nm = sync_service.upsert_nm_articles(db, df)
        _wb_nm_cache_reset()
    sync_service.record_api_pull(db, "wb", "cards", len(df), n + n_nm, "сегодня")
    return {"df": df, "count": n if write_db else len(df), "db_rows": n + n_nm, "rows": len(df), "window": "сегодня"}


def pull_wb_stock(db, provider: Optional[WbProvider] = None, write_db: bool = True) -> dict:
    prov = provider or _wb_provider()
    df = prov.get_stock_report()
    n = 0
    if write_db and not df.empty:
        if "vendorCode" not in df.columns:
            nm_map = _wb_nmid_to_article()
        else:
            nm_map = {}
        qty = pd.to_numeric(
            df["quantity"] if "quantity" in df.columns else df.get("quantityFull", 0),
            errors="coerce",
        ).fillna(0).astype(int)
        sdf = pd.DataFrame({
            "date": str(date.today()),
            "article": df.apply(
                lambda r: nm_map.get(str(r.get("nmId")), str(r.get("vendorCode", r.get("nmId", ""))).strip()),
                axis=1,
            ),
            "warehouse": df["warehouseName"].astype(str),
            "quantity": qty,
        })
        n = sync_service.upsert_stocks(db, sdf, "wb")
    sync_service.record_api_pull(db, "wb", "stock", len(df), n, "сегодня")
    return {"df": df, "count": n if write_db else len(df), "db_rows": n, "rows": len(df), "window": "сегодня"}


def pull_wb_funnel(db, from_, to_, provider: Optional[WbProvider] = None,
                   write_db: bool = True) -> dict:
    prov = provider or _wb_provider()
    df = prov.get_sales_funnel(from_, to_)
    n = 0
    if write_db and not df.empty:
        fdf = _funnel_to_db(df, from_, to_)
        n = sync_service.upsert_funnel(db, fdf)
    window = f"{from_.isoformat()} — {to_.isoformat()}"
    sync_service.record_api_pull(db, "wb", "funnel", len(df), n, window)
    return {"df": df, "count": n if write_db else len(df), "db_rows": n, "rows": len(df), "window": window}


def pull_wb_prices(db, provider: Optional[WbProvider] = None, write_db: bool = True) -> dict:
    prov = provider or _wb_provider()
    df = prov.get_prices()
    sync_service.record_api_pull(db, "wb", "prices", len(df), 0, "")
    return {"df": df, "count": len(df), "db_rows": 0, "rows": len(df), "window": ""}


def pull_wb_storage(db, days: int = 7, provider: Optional[WbProvider] = None,
                    write_db: bool = True) -> dict:
    prov = provider or _wb_provider()
    df = prov.get_storage_cost(number_last_days=days)
    sync_service.record_api_pull(db, "wb", "storage", len(df), 0, f"{days} дней")
    return {"df": df, "count": len(df), "db_rows": 0, "rows": len(df), "window": f"{days} дней"}


def pull_wb_sales(db, from_, to_, provider: Optional[WbProvider] = None,
                  write_db: bool = True) -> dict:
    prov = provider or _wb_provider()
    df = prov.get_sales_realization(from_, to_)
    n = 0
    if write_db:
        sdf = sync_service.normalize_wb_sales(df)
        n = sync_service.upsert_sales(db, sdf, "wb") if sdf is not None else 0
    window = f"{from_.isoformat()} — {to_.isoformat()}"
    sync_service.record_api_pull(db, "wb", "sales", len(df), n, window)
    return {"df": df, "count": n if write_db else len(df), "db_rows": n, "rows": len(df), "window": window}


def pull_wb_detail(db, from_, to_, provider: Optional[WbProvider] = None,
                   write_db: bool = True) -> dict:
    prov = provider or _wb_provider()
    df = prov.get_sales_detail(from_, to_)
    n = 0
    if write_db:
        sdf = sync_service.normalize_wb_sales(df)
        n = sync_service.upsert_sales(db, sdf, "wb", source="detail") if sdf is not None else 0
    window = f"{from_.isoformat()} — {to_.isoformat()}"
    sync_service.record_api_pull(db, "wb", "detail", len(df), n, window)
    return {"df": df, "count": n if write_db else len(df), "db_rows": n, "rows": len(df), "window": window}


def pull_oz_cards(db, provider: Optional[OzonProvider] = None, write_db: bool = True) -> dict:
    prov = provider or _oz_provider()
    df = prov.get_cards()
    n = 0
    if write_db and not df.empty:
        pdf = df.rename(columns={
            "Артикул": "article", "Offer ID": "article",
            "Название товара": "name", "Name": "name",
            "Бренд": "brand", "Category": "brand",
            "Штрихкод (Серийный номер / EAN)": "barcode", "Barcode": "barcode",
        })
        n = sync_service.upsert_products(db, pdf)
    sync_service.record_api_pull(db, "ozon", "cards", len(df), n, "")
    return {"df": df, "count": n if write_db else len(df), "db_rows": n, "rows": len(df), "window": ""}


def pull_oz_stock(db, provider: Optional[OzonProvider] = None, write_db: bool = True) -> dict:
    prov = provider or _oz_provider()
    df = prov.get_stock()
    n = 0
    if write_db and not df.empty:
        qty = _col_num(df, ["free_to_sell_amount"])
        sdf = pd.DataFrame({
            "date": str(date.today()),
            "article": df.apply(
                lambda r: str(r.get("item_code") or r.get("sku") or "").strip(), axis=1,
            ),
            "warehouse": df["warehouse_name"].astype(str),
            "quantity": qty,
        })
        n = sync_service.upsert_stocks(db, sdf, "ozon")
    sync_service.record_api_pull(db, "ozon", "stock", len(df), n, "сегодня")
    return {"df": df, "count": n if write_db else len(df), "db_rows": n, "rows": len(df), "window": "сегодня"}


def pull_oz_prices(db, provider: Optional[OzonProvider] = None, write_db: bool = True) -> dict:
    prov = provider or _oz_provider()
    df = prov.get_prices()
    sync_service.record_api_pull(db, "ozon", "prices", len(df), 0, "")
    return {"df": df, "count": len(df), "db_rows": 0, "rows": len(df), "window": ""}


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


def pull_oz_cashflow(db, from_, to_, provider: Optional[OzonProvider] = None,
                     write_db: bool = True) -> dict:
    prov = provider or _oz_provider()
    df = prov.get_cash_flow(from_, to_)
    window = f"{from_.isoformat()} — {to_.isoformat()}"
    sync_service.record_api_pull(db, "ozon", "cashflow", len(df), 0, window)
    return {"df": df, "count": len(df), "db_rows": 0, "rows": len(df), "window": window}


# ------------------------------------------------------------------ планы обновления
_KIND_LABELS = {
    "wb": [
        ("cards", "Карточки товара"), ("stock", "Остатки"),
        ("funnel", "Воронка продаж"), ("sales", "Продажи"),
        ("prices", "Цены"), ("storage", "Хранение"),
    ],
    "ozon": [
        ("cards", "Карточки товара"), ("stock", "Остатки"),
        ("prices", "Цены"), ("realization", "Реализация"),
        ("cashflow", "Движение средств"),
    ],
}


def _plan_steps(api: str, include_detail: bool, date_from=None, date_to=None):
    """Список (kind, label, callable) для массового обновления маркетплейса."""
    from_, to_ = resolve_range(date_from, date_to)
    steps = []
    for kind, label in _KIND_LABELS[api]:
        if api == "wb":
            if kind == "funnel":
                fn = lambda db: pull_wb_funnel(db, from_, to_, provider=_BULK_PROV["wb"])  # noqa: E731
            elif kind == "sales":
                fn = lambda db: pull_wb_sales(db, from_, to_, provider=_BULK_PROV["wb"])  # noqa: E731
            elif kind == "storage":
                fn = lambda db: pull_wb_storage(db, 7, provider=_BULK_PROV["wb"])  # noqa: E731
            else:
                fn = (lambda k: lambda db: _BULK_PULLS["wb"][k](db, provider=_BULK_PROV["wb"]))(kind)
        else:
            if kind == "realization":
                today = date.today().replace(day=1) - timedelta(days=1)
                yy, mm = today.year, today.month
                fn = lambda db: pull_oz_realization(db, mm, yy, provider=_BULK_PROV["ozon"])  # noqa: E731
            elif kind == "cashflow":
                fn = lambda db: pull_oz_cashflow(db, from_, to_, provider=_BULK_PROV["ozon"])  # noqa: E731
            else:
                fn = (lambda k: lambda db: _BULK_PULLS["ozon"][k](db, provider=_BULK_PROV["ozon"]))(kind)
        steps.append((kind, label, fn))
    if api == "wb" and include_detail:
        steps.append(("detail", "Детализация (finance)",
                      lambda db: pull_wb_detail(db, from_, to_, provider=_BULK_PROV["wb"])))
    return steps


_BULK_PULLS = {
    "wb": {"cards": pull_wb_cards, "stock": pull_wb_stock, "prices": pull_wb_prices},
    "ozon": {"cards": pull_oz_cards, "stock": pull_oz_stock, "prices": pull_oz_prices},
}


# ------------------------------------------------------------------ фоновые задания
_RUNNING = {"wb": None, "ozon": None}
_PENDING = {"wb": None, "ozon": None}
_LOCK = threading.Lock()
_SEQ = 0
_JOBS = {}  # job_id -> state dict


def _next_id() -> int:
    global _SEQ
    _SEQ += 1
    return _SEQ


def start_refresh(api: str, include_detail: bool = False,
                  date_from=None, date_to=None) -> dict:
    """Запускает фоновое обновление. Возвращает {job_id, queued}."""
    global _JOBS
    with _LOCK:
        if _PENDING[api]:
            return {"job_id": None, "queued": False, "rejected": "уже поставлено в очередь"}
        job_id = _next_id()
        steps = _plan_steps(api, include_detail, date_from, date_to)
        job = {
            "id": job_id, "api": api, "include_detail": bool(include_detail),
            "date_from": date_from, "date_to": date_to,
            "status": "running" if _RUNNING[api] is None else "queued",
            "started_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
            "finished_at": None,
            "ok": 0, "failed": 0,
            "steps": [{"kind": k, "label": lbl, "status": "pending",
                       "rows": 0, "db_rows": 0, "window": "", "error": ""}
                      for k, lbl, _ in steps],
        }
        _JOBS[job_id] = job
        queued = _RUNNING[api] is not None
        if queued:
            _PENDING[api] = job_id
            return {"job_id": job_id, "queued": True}
        _RUNNING[api] = job_id
        threading.Thread(
            target=_refresh_worker, args=(api, job_id, date_from, date_to, steps),
            daemon=True,
        ).start()
        return {"job_id": job_id, "queued": False}


def _refresh_worker(api: str, job_id: int, date_from, date_to, steps):
    db = SessionLocal()
    job = _JOBS[job_id]
    job["status"] = "running"
    run = models.RefreshRun(api=api, status="running", results="[]")
    db.add(run)
    db.commit()
    db.refresh(run)
    job["run_id"] = run.id
    results = []
    fatal = None
    try:
        prov_wb = _wb_provider(with_fail_fast=True)
        prov_oz = _oz_provider(with_fail_fast=True)
        _BULK_PROV["wb"] = prov_wb
        _BULK_PROV["ozon"] = prov_oz
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
            if step["status"] == "running":
                step["status"] = "failed"
                step["error"] = fatal
        job["failed"] = job["failed"] or 1
    finally:
        job["status"] = "done"
        job["finished_at"] = datetime.now().isoformat(sep=" ", timespec="seconds")
        if fatal is not None:
            run.status = "failed"
            results.append({"kind": "_", "status": "failed", "rows": 0, "db_rows": 0,
                            "window": "", "error": fatal})
        else:
            run.status = "ok" if job["failed"] == 0 else "partial" if job["ok"] > 0 else "failed"
        run.finished_at = datetime.now()
        run.results = json.dumps(results, ensure_ascii=False)
        db.commit()
        db.close()
        _advance_queue(api)


def _advance_queue(api: str):
    with _LOCK:
        _RUNNING[api] = None
        next_id = _PENDING[api]
        _PENDING[api] = None
        if next_id:
            j = _JOBS[next_id]
            j["status"] = "running"
            _RUNNING[api] = next_id
            threading.Thread(
                target=_refresh_worker,
                args=(api, next_id, j["date_from"], j["date_to"],
                      _plan_steps(api, j["include_detail"], j["date_from"], j["date_to"])),
                daemon=True,
            ).start()


_BULK_PROV = {"wb": None, "ozon": None}


def job_state(job_id: int) -> Optional[dict]:
    job = _JOBS.get(job_id)
    if job is None:
        return None
    return {k: job[k] for k in ("id", "api", "include_detail", "status",
                                "started_at", "finished_at", "ok", "failed", "steps")}


def active_jobs() -> list:
    out = []
    for job in _JOBS.values():
        out.append({k: job[k] for k in ("id", "api", "include_detail", "status",
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