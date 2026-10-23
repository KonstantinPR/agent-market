# -*- coding: utf-8 -*-
"""Выделение и копирование текста в UI: заголовки таблиц, названия секций.

Регресс: `th.sortable { user-select: none }` не давал выделить название колонки
(«иногда удобно скопировать название поля»), а клики по заголовку/секции/ячейке
срабатывали на mouseup и сбрасывали выделение.
"""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "app" / "static"


def _css() -> str:
    return (STATIC / "style.css").read_text(encoding="utf-8")


def _js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


def _rule(css: str, selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"нет CSS-правила {selector}"
    return m.group(1)


def test_headers_and_titles_are_selectable():
    css = _css()
    for selector in ("th.sortable", ".dash-sec-title h3",
                     ".p-group > summary.p-group-sum"):
        body = _rule(css, selector)
        assert "user-select: text" in body, f"{selector}: текст должен выделяться"
        assert "user-select: none" not in body, f"{selector}: выделение запрещено"


def test_cells_are_selectable():
    body = _rule(_css(), "th, td")
    assert "user-select: text" in body, "ячейки таблиц должны выделяться"


def test_click_handlers_respect_selection():
    js = _js()
    assert "function textSelected()" in js, "нет хелпера textSelected()"
    # Сортировка таблицы, сворачивание секции Обзора, раскрытие ячейки ABC×XYZ:
    # клик после протяжки мышью не должен срабатывать.
    assert js.count("if (textSelected()) return;") >= 3
    # Название группы настроек автопилота — <summary> в <details>.
    assert re.search(
        r'sum\.addEventListener\("click", \(e\) => \{[^}]*textSelected\(\)', js
    ), "summary группы не проверяет выделение"
