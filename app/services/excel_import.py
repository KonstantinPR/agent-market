"""Разбор Excel-файла потребности, загруженного в меню PDF.

Файл — та же выгрузка, что делает кнопка «Excel» раздела «Потребность»
(колонки распознаются по русским подписям из ``_REPLENISH_EXPORT``): владелец
правит его в Excel (удаляет карточки, меняет количества) и возвращает в PDF.
Источник истины — файл: какие колонки в нём есть, те значения и печатаются;
неизвестные колонки не мешают и попадают только в сводку предпросмотра.
"""
from __future__ import annotations

import io
import json
import math
import re

import pandas as pd

#: Потолок строк ответа: дальше смысла нет (PDF и так ограничен PDF_MAX_LIMIT),
#: а раздутый файл не должен раздувать и предпросмотр.
MAX_ROWS = 5000

#: Колонки, которые обязаны остаться строками: артикул и баркод в выгрузке
#: часто числовые, pandas превратил бы их в 12345.0.
STR_KEYS = ("article", "name", "barcode", "status_label", "actual_mp")


class ExcelImportError(ValueError):
    """Файл не является выгрузкой потребности — отдаём как HTTP 400."""


def _as_str(v) -> str:
    """Ячейка -> строка без «12345.0» и без «nan»."""
    if v is None:
        return ""
    if isinstance(v, float):
        if math.isnan(v):
            return ""
        if v.is_integer():
            return str(int(v))
        return str(v)
    if isinstance(v, int):
        return str(v)
    s = str(v).strip()
    return "" if s.lower() == "nan" else s


def parse_replenish_excel(
    data: bytes,
    key_labels: dict,
    *,
    max_rows: int = MAX_ROWS,
) -> tuple[list[dict], dict]:
    """xlsx первого листа -> ``(строки как в выгрузке, сводка)``.

    ``key_labels`` — карта ``{ключ: русская подпись}`` (``_REPLENISH_EXPORT``).
    Колонки сопоставляются по подписи (пробелы по краям срезаются), колонка
    «Артикул» обязательна, строки без артикула отбрасываются и считаются.
    Дубликаты подписей берутся по первому вхождению.

    Возвращает native-типы JSON (int/float/str/None), колонки вне карты в
    строки не включаются. Бросает :class:`ExcelImportError` на битый файл.
    """
    try:
        df = pd.read_excel(io.BytesIO(data), sheet_name=0)
    except ExcelImportError:
        raise
    except Exception as e:  # noqa: BLE001 — pandas кидает десятки типов ошибок
        raise ExcelImportError(f"Не удалось прочитать файл как xlsx: {e}") from e
    if df.empty:
        raise ExcelImportError("В файле нет строк: первый лист пуст")

    labels = [str(h).strip() for h in df.columns]
    rev: dict[str, str] = {}
    for key, label in key_labels.items():
        rev.setdefault(str(label).strip(), key)
    if "Артикул" not in rev or rev["Артикул"] != "article":
        raise ExcelImportError("Карта экспорта не содержит колонки «Артикул»")

    # Дубликаты подписей (в т.ч. скопированная колонка) — берём первое.
    # pandas переименовывает повтор «Метка» в «Метка.1», «Метка.2» — это тот
    # же дубль: узнаём его по основе из уже встреченных подписей.
    seen: set = set()
    keep: list[int] = []
    duplicates = 0
    for i, lab in enumerate(labels):
        base = re.sub(r"\.\d+$", "", lab)
        if base in seen and base in rev:
            duplicates += 1
            continue
        if lab in seen and lab in rev:
            duplicates += 1
            continue
        seen.add(lab)
        keep.append(i)
    if duplicates:
        df = df.iloc[:, keep]
        labels = [labels[i] for i in keep]

    if "Артикул" not in labels:
        raise ExcelImportError(
            "В файле нет колонки «Артикул» — это должна быть выгрузка "
            "«Потребность» (кнопка «Excel»)"
        )

    recognized = [lab for lab in labels if lab in rev]
    unknown = list(dict.fromkeys(lab for lab in labels if lab not in rev))

    # Строки без артикула — либо разделители/итоги внизу листа, либо мусор.
    art_col = labels.index("Артикул")
    articles = df.iloc[:, art_col].map(_as_str)
    mask = articles != ""
    dropped = int((~mask).sum())
    if not bool(mask.any()):
        raise ExcelImportError("В файле нет ни одной строки с артикулом")
    df = df[mask]

    # Только распознанные колонки, ключи вместо подписей.
    df = df.iloc[:, [labels.index(lab) for lab in recognized]]
    df.columns = [rev[lab] for lab in recognized]
    for key in STR_KEYS:
        if key in df.columns:
            df[key] = df[key].map(_as_str)

    # to_json даёт native-типы (numpy int64/float64 не утекают в JSON) и null.
    rows: list[dict] = json.loads(df.to_json(orient="records"))
    truncated = max(0, len(rows) - max_rows)
    rows = rows[:max_rows]

    meta = {
        "count": len(rows),
        "columns": recognized,
        "unknown": unknown,
        "dropped": dropped,
        "duplicates": duplicates,
        "truncated": truncated,
    }
    return rows, meta
