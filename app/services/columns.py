# -*- coding: utf-8 -*-
"""Единый источник колонок — ``app/static/columns.json``.

Содержит:
- ``tabs``   — панель «Вид таблицы» (наборы колонок каждой вкладки, def-флаги);
- ``dicts``   — Excel-словари ``{ключ: русская подпись}`` для ``excel_io.project_export``.

JSON — мастер: его правят скриптом ``tests/unit/test_column_manifest.py`` (сверка),
а не разводят по ``app.js``/``app/api``. Нумерация ``UI_VERSION`` в ``app.js``
при правках панели обязательна (кэш браузера).
"""
import json
from functools import lru_cache
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "static" / "columns.json"


@lru_cache(maxsize=1)
def load() -> dict:
    """Весь ``columns.json`` (кэшируется на процесс)."""
    return json.loads(_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def export_cols(name: str) -> dict:
    """Excel-словарь по его имени в ``dicts``: ``{ключ: подпись}``, порядок как в JSON.

    Ключи, которых нет в DataFrame, ``excel_io.project_export`` молча пропускает —
    словарь можно не резать под конкретный df. ``name`` — имя словаря
    (``export_margin_detail``, ``OZON_DETAIL_RU_COLUMNS``, …).
    """
    dicts = load()["dicts"]
    if name not in dicts:
        raise KeyError(f"columns.json: нет словаря {name!r}")
    return {c["k"]: c["label"] for c in dicts[name]}


def tab_columns(tab: str) -> dict:
    """``{режим: [колонки]}`` вкладки (для тестов/диагностики)."""
    return {m: [dict(c) for c in mv["columns"]]
            for m, mv in load()["tabs"][tab]["modes"].items()}
