# -*- coding: utf-8 -*-
"""Манифест вкладок: навигация ↔ секции ↔ loadTabInner ↔ «Вид таблицы» ↔ apiPullByTab.

Вкладка — это пять точек правки в двух файлах (см. AGENTS.md, «Чек-лист:
новая вкладка целиком»). Тест ловит тихие поломки: пункт меню без секции,
секцию без ветки рендера (клик молчит), «Вид таблицы» без id, мёртвые
записи в apiPullByTab.

Всё статическое — только чтение app.js/index.html, без БД и Node.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "app" / "static" / "app.js"
INDEX_HTML = ROOT / "app" / "static" / "index.html"

# Известные и намеренные исключения (новые не добавлять без обсуждения).
# Секции/ветки без пункта меню — мёртвые панели (T-связанные, остаются на время).
ORPHAN_TABS = {"sales", "stocks"}
# Вкладки без «Вида таблицы»: списки/формы, а не табличные разделы.
NO_COLVIEW_TABS = {
    "dashboard", "ours", "wh-cp", "wh-receipt", "wh-shipment", "wh-stock",
    "wh-turnover", "yandex", "tickets",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def nav_tabs() -> list:
    """Пункты навигации: <a class="nav-link" data-tab="X">."""
    src = _read(INDEX_HTML)
    return re.findall(r'<a\s+class="nav-link"\s+data-tab="([^"]+)"', src)


def section_tabs() -> list:
    """Секции-панели: <section id="tab-X" ...>."""
    return re.findall(r'<section[^>]*\bid="tab-([^"]+)"', _read(INDEX_HTML))


def load_tab_branches() -> set:
    """Имена веток в loadTabInner: name === "X" (в т.ч. «a» || «b»)."""
    src = _read(APP_JS)
    m = re.search(r"async function loadTabInner\b.*?\n\}", src, re.S)
    assert m, "в app.js нет функции loadTabInner"
    return set(re.findall(r'name === "([^"]+)"', m.group(0)))


def colview_tabs() -> set:
    return set(re.findall(r'registerColView\("([^"]+)"', _read(APP_JS)))


def pull_tabs() -> set:
    """Ключи объекта apiPullByTab (кнопка «Обновить базу»)."""
    m = re.search(r"const apiPullByTab\s*=\s*\{(.*?)\n  \};", _read(APP_JS), re.S)
    assert m, "в app.js нет объекта apiPullByTab"
    return set(re.findall(r'"([^"]+)"\s*:', m.group(1)))


def test_nav_has_no_duplicates():
    nav = nav_tabs()
    assert len(nav) >= 30, f"в навигации меньше 30 вкладок: {len(nav)}"
    assert len(nav) == len(set(nav)), "пункт навигации продублирован"


def test_every_nav_tab_has_section():
    missing = set(nav_tabs()) - set(section_tabs())
    assert not missing, f"нет <section id='tab-…'> для пунктов меню: {sorted(missing)}"


def test_every_nav_tab_has_render_branch():
    missing = set(nav_tabs()) - load_tab_branches()
    assert not missing, (
        "нет ветки в loadTabInner — клик по пункту меню не отрисует вкладку: "
        f"{sorted(missing)}"
    )


def test_no_dead_sections_or_branches():
    """Секции и ветки без пункта меню — только из ORPHAN_TABS."""
    extra_sec = set(section_tabs()) - set(nav_tabs())
    extra_br = load_tab_branches() - set(nav_tabs())
    assert extra_sec <= ORPHAN_TABS, f"мёртвая секция без меню: {sorted(extra_sec - ORPHAN_TABS)}"
    assert extra_br <= ORPHAN_TABS, f"мёртвая ветка рендера без меню: {sorted(extra_br - ORPHAN_TABS)}"


def test_colview_is_registered_or_known():
    """Каждая вкладка либо имеет «Вид таблицы», либо входит в NO_COLVIEW_TABS."""
    tabs = set(nav_tabs())
    missing = tabs - colview_tabs() - NO_COLVIEW_TABS
    stale = NO_COLVIEW_TABS & colview_tabs()
    assert not missing, (
        "вкладка без registerColView и без записи в NO_COLVIEW_TABS "
        f"(добавить в список или сделать «Вид таблицы»): {sorted(missing)}"
    )
    assert not stale, f"в NO_COLVIEW_TABS есть вкладки с registerColView: {sorted(stale)}"


def test_no_orphan_colview_tabs():
    """registerColView без пункта меню — только из ORPHAN_TABS (панели дашборда)."""
    known_dash_panels = {"dash-loss", "dash-prefix", "dash-price", "dash-profit"}
    extra = colview_tabs() - set(nav_tabs()) - known_dash_panels - ORPHAN_TABS
    assert not extra, f"registerColView для несуществующей вкладки: {sorted(extra)}"


def test_pull_by_tab_keys_are_nav_tabs():
    extra = pull_tabs() - set(nav_tabs())
    assert not extra, f"мёртвая запись в apiPullByTab (вкладки уже нет в меню): {sorted(extra)}"


def test_api_tabs_have_pull_route():
    """Вкладки WB/Ozon API с кнопкой «Обновить базу» обязаны иметь маршрут.

    Частично дублирует test_js_helpers.py (там хардкод 9 вкладок), здесь —
    все вкладки c apiPullByTab против фактического набора api-вкладок.
    """
    api_tabs = {t for t in nav_tabs() if t.startswith(("wb-", "oz-"))}
    # Не все api-вкладки обновляются кнопкой (нет pull-роута) — явный список.
    no_pull = {"wb-detail"}  # отдельная кнопка btnUpdateWbDetail + свой роут
    missing = api_tabs - pull_tabs() - no_pull
    assert not missing, f"api-вкладка без записи в apiPullByTab: {sorted(missing)}"


def test_manifest_counts_are_stable():
    """Страховка от регрессии парсинга: числа зафиксированы осознанно."""
    assert len(nav_tabs()) == 30, "изменилось число пунктов меню — обновите тест осознанно"
    assert len(section_tabs()) == 32, "изменилось число секций — обновите тест осознанно"
    assert len(pull_tabs()) == 16, "изменилось число записей apiPullByTab — обновите тест"
