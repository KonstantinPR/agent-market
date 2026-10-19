# -*- coding: utf-8 -*-
"""Пакет эндпоинтов agent_market: роутеры групп собраны в один APIRouter(prefix="/api")."""
from fastapi import APIRouter

from . import (
    _common,
    cabinets,
    export_wb,
    export_ozon,
    export_reports,
    wb,
    ozon,
    margin,
    pricing,
    pricing_ozon,
    products,
    replenish,
    imports,
    warehouse,
    tickets,
    catalog,
    maintenance,
    automation,
    batch,
)

router = APIRouter(prefix="/api")
for _mod in (
    _common,
    cabinets,
    export_wb,
    export_ozon,
    export_reports,
    wb,
    ozon,
    margin,
    pricing,
    pricing_ozon,
    products,
    replenish,
    imports,
    warehouse,
    tickets,
    catalog,
    maintenance,
    automation,
    batch,
):
    router.include_router(_mod.router)

# Совместимость: раньше эти константы жили в app/api.py — тесты
# импортируют их отсюда (from app.api import PDF_MEDIA, ...).
from app.api._common import PDF_DEFAULT_LIMIT, PDF_MAX_LIMIT, PDF_MEDIA, _REPLENISH_EXPORT  # noqa: F401
