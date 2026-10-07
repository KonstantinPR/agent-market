# -*- coding: utf-8 -*-
"""Снимок маршрутов API: защита перед разбиением app/api.py (этап рефакторинга).

Что проверяет:
- ни один существующий путь/метод не исчез и не переименован (все пути из
  снимка обязаны быть в приложении);
- нет дублей (одинаковый путь + метод у двух функций);
- новые маршруты допустимы — они не ломают тест, но попадут в снимок после
  осознанного обновления (см. внизу файла).

Снимок намеренно полный: при разбиении api.py на роутеры префиксы и имена
путей обязаны сохраниться байт в байт.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Снимок: "METHOD /path" (порядок методов алфавитный). Менять осознанно.
# ---------------------------------------------------------------------------
SNAPSHOT = """
DELETE /api/warehouse/counterparties/{cp_id}
DELETE /api/warehouse/docs/{doc_id}
DELETE /api/yandex/delete
GET /api/cards
GET /api/custom-stock
GET /api/dashboard
GET /api/export/dashboard
GET /api/export/margin/detail
GET /api/export/margin/funnel
GET /api/export/margin/ozon-detail
GET /api/export/ozon/accrual-rows
GET /api/export/ozon/buyout-rows
GET /api/export/ozon/cashflow-rows
GET /api/export/ozon/detail-rows
GET /api/export/ozon/detail-summary
GET /api/export/ozon/placement-rows
GET /api/export/ozon/placement-summary
GET /api/export/products
GET /api/export/replenish
GET /api/export/replenish/pdf
GET /api/export/sales
GET /api/export/wb/cards
GET /api/export/wb/detail-rows
GET /api/export/wb/detail-summary
GET /api/export/wb/funnel
GET /api/export/wb/prices
GET /api/export/wb/stock
GET /api/export/wb/storage
GET /api/funnel
GET /api/margin/detail
GET /api/margin/funnel
GET /api/margin/ozon-detail
GET /api/ozon/accrual-rows
GET /api/ozon/buyout-rows
GET /api/ozon/cashflow-rows
GET /api/ozon/detail-rows
GET /api/ozon/detail-summary
GET /api/ozon/placement-rows
GET /api/ozon/placement-summary
GET /api/prices
GET /api/pricing/defaults
GET /api/pricing/history
GET /api/products
GET /api/products/price-settings
GET /api/promo/list
GET /api/pulls
GET /api/refresh
GET /api/refresh/history
GET /api/refresh/{job_id}
GET /api/replenish
GET /api/sales
GET /api/stocks
GET /api/storage-cost
GET /api/tickets
GET /api/warehouse/counterparties
GET /api/warehouse/docs
GET /api/warehouse/docs/{doc_id}/items
GET /api/warehouse/export/{kind}
GET /api/warehouse/stock
GET /api/warehouse/turnover
GET /api/wb/detail-rows
GET /api/wb/detail-summary
GET /api/yandex/download
GET /api/yandex/list
POST /api/export/replenish/pdf
POST /api/import/cards
POST /api/import/custom-stock
POST /api/import/net-cost
POST /api/import/products
POST /api/ozon/accrual
POST /api/ozon/buyout
POST /api/ozon/cards
POST /api/ozon/cashflow
POST /api/ozon/detail
POST /api/ozon/placement
POST /api/ozon/prices
POST /api/ozon/realization
POST /api/ozon/stock
POST /api/pricing/apply
POST /api/pricing/export
POST /api/pricing/recommendations
POST /api/products/preview
POST /api/products/prices/apply
POST /api/products/refresh
POST /api/products/replenishable
POST /api/promo/refresh
POST /api/refresh
POST /api/replenish/import-excel
POST /api/sync/{code}
POST /api/tickets
POST /api/tickets/{tid}/block
POST /api/tickets/{tid}/close
POST /api/tickets/{tid}/decline
POST /api/tickets/{tid}/reopen
POST /api/tickets/{tid}/start
POST /api/tickets/{tid}/unblock
POST /api/warehouse/counterparties
POST /api/warehouse/docs
POST /api/warehouse/fromdisk
POST /api/warehouse/import/counterparties
POST /api/warehouse/import/docs
POST /api/warehouse/todisk
POST /api/wb/cards
POST /api/wb/detail
POST /api/wb/detail-upload
POST /api/wb/funnel
POST /api/wb/prices
POST /api/wb/sales
POST /api/wb/stock
POST /api/wb/storage
POST /api/yandex/upload
"""


SKIP_EXACT = {"/", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}


def _api_routes() -> list:
    from app.main import app

    keys = set()
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if not methods or route.path in SKIP_EXACT or route.path.startswith("/static"):
            continue
        for method in sorted(m for m in methods if m != "HEAD"):
            keys.add(f"{method} {route.path}")
    return sorted(keys)


def _snapshot_keys() -> list:
    return sorted(line for line in SNAPSHOT.splitlines() if line.strip())


def test_snapshot_routes_still_exist():
    """Ни один путь из снимка не исчез/не переименован."""
    actual = set(_api_routes())
    missing = [k for k in _snapshot_keys() if k not in actual]
    assert not missing, (
        "маршруты из снимка пропали (имена/методы нельзя менять молча):\n  "
        + "\n  ".join(missing)
    )


def test_no_duplicate_routes():
    """Один путь+метод не может обслуживать две функции — FastAPI молча возьмёт последний."""
    from app.main import app

    seen = {}
    dups = []
    for route in app.routes:
        methods = getattr(route, "methods", None)
        if not methods:
            continue
        for method in (m for m in methods if m != "HEAD"):
            key = (method, route.path)
            if key in seen:
                dups.append(f"{method} {route.path}: {seen[key]} vs {route.endpoint.__name__}")
            seen[key] = getattr(route.endpoint, "__name__", str(route.endpoint))
    assert not dups, "дубли маршрутов:\n  " + "\n  ".join(dups)


def test_all_api_routes_are_prefixed():
    """Все рабочие маршруты — под /api (служебные openapi/docs допустимы)."""
    from app.main import app

    allowed = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc", "/"}
    stray = [
        r.path for r in app.routes
        if getattr(r, "methods", None)
        and r.path not in allowed
        and not r.path.startswith("/api")
        and not r.path.startswith("/static")
    ]
    assert not stray, f"роуты вне /api: {sorted(stray)}"


def test_snapshot_is_sorted_and_unique():
    keys = _snapshot_keys()
    assert keys == sorted(set(keys)), "снимок должен быть отсортирован и без дублей"
    assert len(keys) >= 100, f"снимок подозрительно мал: {len(keys)}"


def test_new_routes_are_intentional():
    """Новые маршруты допустимы, но их число не должно расти бесконтрольно.

    После добавления роута — вписать его в SNAPSHOT (тогда и этот тест, и
    test_snapshot_routes_still_exist говорят одно и то же).
    """
    actual = set(_api_routes())
    new = sorted(actual - set(_snapshot_keys()))
    assert len(new) <= 10, (
        "появилось много маршрутов, не занесённых в снимок — обновите SNAPSHOT:\n  "
        + "\n  ".join(new)
    )
