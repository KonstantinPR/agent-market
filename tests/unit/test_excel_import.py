"""Тесты разбора Excel-файла потребности (app/services/excel_import.py)."""
import io

import pytest
from openpyxl import Workbook

from app.api import _REPLENISH_EXPORT
from app.services.excel_import import ExcelImportError, parse_replenish_excel


def _xlsx(headers: list, rows: list) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _parse(data: bytes, **kw):
    return parse_replenish_excel(data, _REPLENISH_EXPORT, **kw)


def test_roundtrip_uses_export_labels():
    data = _xlsx(
        ["Артикул", "Наименование", "Спрос, шт/д", "WB дефицит"],
        [["ART-1", "Платье летнее", 1.5, 10]],
    )
    rows, meta = _parse(data)
    assert rows == [{
        "article": "ART-1", "name": "Платье летнее",
        "demand": 1.5, "wb_def": 10,
    }]
    assert meta["count"] == 1
    assert meta["columns"] == ["Артикул", "Наименование", "Спрос, шт/д", "WB дефицит"]
    assert meta["unknown"] == []
    assert meta["dropped"] == 0
    assert meta["truncated"] == 0


def test_numeric_article_is_a_string_without_dot():
    data = _xlsx(["Артикул", "Штрихкод"], [[12345, 7712345678901.0]])
    rows, _ = _parse(data)
    assert rows[0]["article"] == "12345"
    assert rows[0]["barcode"] == "7712345678901"


def test_missing_article_column_is_rejected():
    data = _xlsx(["Наименование", "Спрос, шт/д"], [["Платье", 2]])
    with pytest.raises(ExcelImportError, match="Артикул"):
        _parse(data)


def test_rows_without_article_are_dropped_and_counted():
    data = _xlsx(
        ["Артикул", "Наименование"],
        [["ART-1", "Первая"], ["", "Итого: 15"], [None, "Пустая"],
         ["   ", "Пробел"], ["ART-2", "Вторая"]],
    )
    rows, meta = _parse(data)
    assert [r["article"] for r in rows] == ["ART-1", "ART-2"]
    assert meta["count"] == 2
    assert meta["dropped"] == 3


def test_sheet_without_single_article_is_rejected():
    data = _xlsx(["Артикул", "Наименование"], [["", "Итого"], ["", "Ещё итог"]])
    with pytest.raises(ExcelImportError, match="ни одной строки"):
        _parse(data)


def test_only_headers_sheet_is_rejected():
    data = _xlsx(["Артикул", "Наименование"], [])
    with pytest.raises(ExcelImportError, match="нет строк"):
        _parse(data)


def test_unknown_columns_are_reported_but_not_in_rows():
    data = _xlsx(
        ["Артикул", "Заметки", "Спрос, шт/д"],
        [["ART-1", "не трогать", 3]],
    )
    rows, meta = _parse(data)
    assert rows == [{"article": "ART-1", "demand": 3}]
    assert meta["unknown"] == ["Заметки"]


def test_empty_cell_becomes_none_not_zero():
    """Пустой «WB дефицит» = бюджет не задан, а не ноль."""
    data = _xlsx(["Артикул", "WB дефицит"], [["ART-1", None]])
    rows, _ = _parse(data)
    assert rows[0]["wb_def"] is None


def test_duplicate_label_keeps_first_value():
    data = _xlsx(
        ["Артикул", "Спрос, шт/д", "Спрос, шт/д"],
        [["ART-1", 7, 99]],
    )
    rows, meta = _parse(data)
    assert rows[0]["demand"] == 7
    assert meta["duplicates"] == 1


def test_max_rows_truncates_and_reports():
    data = _xlsx(["Артикул"], [["A1"], ["A2"], ["A3"]])
    rows, meta = _parse(data, max_rows=2)
    assert len(rows) == 2
    assert meta["count"] == 2
    assert meta["truncated"] == 1


def test_garbage_bytes_are_rejected():
    with pytest.raises(ExcelImportError, match="Не удалось прочитать"):
        _parse(b"this is not a zip archive")
