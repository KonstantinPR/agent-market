# -*- coding: utf-8 -*-
"""Сверка колонок JS ↔ Python: панель «Вид таблицы» и Excel-экспорт.

Каждая вкладка хранит свои ключи колонок в двух местах: в `registerColView`
(app.js — что видно на экране) и в словаре export-роута (app/api.py — что
попадает в Excel). Ключ, которого нет в export-словаре, молча не печатается
в файле; ключ, которого нет в панели, не может быть включён пользователем.

Тест фиксирует текущие расхождения в KNOWN_JS_ONLY / KNOWN_PY_ONLY (устранимые
на этапе рефакторинга колонок) и падает на любых новых. Строгое равенство —
после устранения расхождения запись убирается из KNOWN, чтобы возврат не
прошёл незамеченным.

Расхождения по ПОДПИСЯМ («Выручка» vs «Выручка, руб» и т.п.) здесь не
проверяются — они отдельным шагом на том же этапе колонок.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "app" / "static" / "app.js"
API_PY = ROOT / "app" / "api.py"

# Вкладка -> экспорт-роуты, чьи ключи обязаны покрывать колонки вкладки.
# (Пути из app/api.py — без префикса /api; для режимов rows/summary — оба.)
TAB_EXPORT = {
    "margin-detail": ["/export/margin/detail"],
    "margin-funnel": ["/export/margin/funnel"],
    "margin-ozon-detail": ["/export/margin/ozon-detail"],
    "products": ["/export/products"],
    "replenish": ["/export/replenish"],
    "pricing": ["/pricing/export"],
    "wb-cards": ["/export/wb/cards"],
    "oz-cards": ["/export/wb/cards"],
    "wb-stock": ["/export/wb/stock"],
    "oz-stock": ["/export/wb/stock"],
    "wb-funnel": ["/export/wb/funnel"],
    "wb-prices": ["/export/wb/prices"],
    "oz-prices": ["/export/wb/prices"],
    "wb-sales": ["/export/sales"],
    "oz-realization": ["/export/sales"],
    "wb-storage": ["/export/wb/storage"],
    "wb-detail": ["/export/wb/detail-rows", "/export/wb/detail-summary"],
    "oz-detail": ["/export/ozon/detail-rows", "/export/ozon/detail-summary"],
    "oz-accrual": ["/export/ozon/accrual-rows"],
    "oz-cashflow": ["/export/ozon/cashflow-rows"],
    "oz-placement": ["/export/ozon/placement-rows", "/export/ozon/placement-summary"],
}

# Известные расхождения (устранимые). Строго: фактический список обязан
# совпасть с KNOWN — иначе тест просит обновить KNOWN осознанно.
# Вкладки панели дашборда (dash-*) не покрываются: их headers задаются
# точечными ссылками (dashHeaders.tops), парсер их не читает.
KNOWN_JS_ONLY = {
    "margin-funnel": ["nm_id"],
    "oz-detail": ["source"],
    "oz-prices": ["disc_min", "disc_min_p", "price_min", "sizes"],
    "pricing": ["nm_id"],
    "products": ["min_price"],
    "wb-funnel": [
        "dy_adds", "dy_avg_price", "dy_buyouts", "dy_cancelled", "dy_orders",
        "dy_revenue", "dy_views", "past_adds", "past_avg_price",
        "past_buyout_sum", "past_buyouts", "past_cancel_sum",
        "past_cancelled", "past_orders", "past_revenue", "past_views",
        "subject_id", "tags", "title",
    ],
    "wb-prices": ["disc_min", "disc_min_p", "price_min", "sizes"],
}

KNOWN_PY_ONLY = {
    "margin-detail": ["delta_pct", "delta_ru", "margin_pp", "net_cost_est", "sells_pp"],
    "margin-ozon-detail": [
        "accrued_coverage", "delta_pct", "delta_ru", "margin_gross",
        "margin_pp", "net_cost_est", "sells_pp", "storage_per_one",
    ],
    "oz-detail": ["barcode", "commission_ratio"],
    "oz-prices": ["nm_id"],
    "oz-stock": ["date", "marketplace"],
    "pricing": [
        "detail_returns_qty", "detail_sells", "eff", "floor_price",
        "max_discount_item", "price", "replenishable", "status",
    ],
    "products": ["composition", "mp_stock", "own_stock", "subject", "tags", "volume_l"],
    "replenish": [
        "income", "oz_qty", "returns_qty", "sells", "status_label",
        "wb_net", "wb_qty", "wb_ret",
    ],
    "wb-detail": ["retail_price", "srid"],
    "wb-prices": ["nm_id"],
    "wb-stock": ["date", "marketplace"],
    "wb-storage": ["nm_id"],
}

# Дополнительные python-константы, участвующие в cols конкретной вкладки,
# но не упомянутые прямо в теле её роута (алиасы применяет helper).
EXTRA_CONST = {"replenish": ["_REPLENISH_COL_ALIASES"]}

PAIR = re.compile(r'"([a-z_][a-z0-9_]*)"\s*:\s*"([^"]*)"')
CONST_SUFFIX = re.compile(
    r"\b([A-Z_][A-Z0-9_]*_(?:RU_COLUMNS|RENAME|EXPORT|EXPORT_SIZES|COL_ALIASES))\b"
)
BODY_END = re.compile(r"(?m)^(?:@router\.(?:get|post)\(|def |async def |[A-Z_][A-Z0-9_]*\s*=)")


def _brace_dict(text: str, start: int):
    """Пары (ключ, подпись) из литерала dict, начинающегося после `start`."""
    i = text.find("{", start)
    if i < 0:
        return set()
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return set(PAIR.findall(text[i:j + 1]))
    return set()


def python_route_keys(api_src: str) -> dict:
    """Путь роута -> множество ключей его export-словарей."""
    consts = {}

    def const_pairs(name):
        if name not in consts:
            m = re.search(r"(?m)^%s\s*=\s*\{" % re.escape(name), api_src)
            consts[name] = _brace_dict(api_src, m.start()) if m else set()
        return consts[name]

    out = {}
    for m in re.finditer(r'@router\.(get|post)\("([^"]+)"\)', api_src):
        path = m.group(2)
        fn = re.search(r"(?m)^(?:async )?def\s+\w+", api_src[m.end():])
        body_start = m.end() + fn.start() if fn else m.end()
        nxt = BODY_END.search(api_src, body_start + 1)
        body = api_src[body_start:nxt.start()] if nxt else api_src[body_start:]
        pairs = PAIR.findall(body)
        for name in set(CONST_SUFFIX.findall(body)):
            pairs += list(const_pairs(name))
        if pairs:
            out.setdefault(path, set()).update(k for k, _ in pairs)
    for tab, names in EXTRA_CONST.items():
        for name in names:
            for path in TAB_EXPORT[tab]:
                out.setdefault(path, set()).update(k for k, _ in const_pairs(name))
    return out


def _brace_array(text: str, start: int) -> str:
    i = text.find("[", start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "[":
            depth += 1
        elif text[j] == "]":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    return ""


def js_colview_keys(js_src: str) -> dict:
    """Вкладка -> множество ключей колонок всех её режимов."""
    headers = {}
    for m in re.finditer(r"(?:const|let|var)\s+(\w*Headers)\s*=\s*\[", js_src):
        body = _brace_array(js_src, m.end() - 1)
        headers[m.group(1)] = re.findall(r'\bk:\s*"([^"]+)"', body)

    out = {}
    for m in re.finditer(r'registerColView\("([^"]+)",\s*\{', js_src):
        tab = m.group(1)
        depth = 0
        body = ""
        for j in range(m.end() - 1, len(js_src)):
            if js_src[j] == "{":
                depth += 1
            elif js_src[j] == "}":
                depth -= 1
                if depth == 0:
                    body = js_src[m.end() - 1:j + 1]
                    break
        keys = []
        for name in re.findall(r"headers:\s*([A-Za-z_$][\w$]*)", body):
            if "." in name:  # точечные ссылки (dashHeaders.tops) не читаем
                keys = []
                break
            keys += headers.get(name, [])
        out[tab] = set(keys)
    return out


def _diff(tab: str, js_keys: set, py_keys: set):
    js_only = sorted(js_keys - py_keys)
    py_only = sorted(py_keys - js_keys)
    assert js_only == sorted(KNOWN_JS_ONLY.get(tab, [])), (
        "колонки панели без поддержки в Excel-экспорте вкладки %s.\n"
        "  фактически: %s\n  KNOWN:     %s\n"
        "Если расхождение новое — починить export или осознанно добавить в KNOWN_JS_ONLY."
        % (tab, js_only, sorted(KNOWN_JS_ONLY.get(tab, [])))
    )
    assert py_only == sorted(KNOWN_PY_ONLY.get(tab, [])), (
        "колонки Excel-экспорта вкладки %s недоступны в панели «Вид таблицы».\n"
        "  фактически: %s\n  KNOWN:     %s"
        % (tab, py_only, sorted(KNOWN_PY_ONLY.get(tab, [])))
    )


def _load():
    return (
        js_colview_keys(APP_JS.read_text(encoding="utf-8")),
        python_route_keys(API_PY.read_text(encoding="utf-8")),
    )


def test_every_colview_tab_has_export_mapping():
    """Каждая вкладка с «Вид таблицы» должна иметь известный export-роут."""
    js_keys, py_keys = _load()
    tabs = {t for t, k in js_keys.items() if k}
    assert set(TAB_EXPORT) <= tabs, f"маппинг на несуществующие вкладки: {sorted(set(TAB_EXPORT) - tabs)}"
    missing = tabs - set(TAB_EXPORT) - {"dash-loss", "dash-prefix", "dash-price", "dash-profit"}
    assert not missing, (
        "вкладка с колонками, но без строки в TAB_EXPORT (добавьте роуты экспорта): "
        f"{sorted(missing)}"
    )


def test_mapped_export_routes_exist():
    _, py_keys = _load()
    missing = [(t, p) for t, paths in TAB_EXPORT.items() for p in paths if p not in py_keys]
    assert not missing, f"export-роут без словаря колонок (переименован/удалён?): {missing}"


def test_column_keys_match_between_ui_and_export():
    """Главная сверка: ключи панели и export-словаря совпадают (с KNOWN)."""
    js_keys, py_keys = _load()
    for tab, paths in sorted(TAB_EXPORT.items()):
        if not js_keys.get(tab):
            continue
        py = set()
        for p in paths:
            py |= py_keys.get(p, set())
        _diff(tab, js_keys[tab], py)


def test_export_dicts_have_no_duplicate_keys():
    """Дубль ключа в словаре экспорта: Python молча оставит первое значение."""
    api_src = API_PY.read_text(encoding="utf-8")
    problems = []
    for name in set(CONST_SUFFIX.findall(api_src)):
        m = re.search(r"(?m)^%s\s*=\s*\{" % re.escape(name), api_src)
        if not m:
            continue
        raw = re.findall(r'"([a-z_][a-z0-9_]*)"\s*:', _brace_dict_source(api_src, m.start()))
        dups = sorted({k for k in raw if raw.count(k) > 1})
        if dups:
            problems.append(f"{name}: {dups}")
    # литеральные dict-ы внутри export-роутов
    for m in re.finditer(r"project_export\(\s*\w+,\s*\{", api_src):
        raw = re.findall(r'"([a-z_][a-z0-9_]*)"\s*:', _brace_dict_source(api_src, m.start()))
        dups = sorted({k for k in raw if raw.count(k) > 1})
        if dups:
            line = api_src.count("\n", 0, m.start()) + 1
            problems.append(f"api.py:{line}: {dups}")
    assert not problems, "дубли ключей в словарях экспорта:\n  " + "\n  ".join(problems)


def _brace_dict_source(text: str, start: int) -> str:
    i = text.find("{", start)
    if i < 0:
        return ""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i:j + 1]
    return ""


def test_known_lists_are_json_sane():
    """Сами KNOWN-списки — отсортированы и без дублей (иначе сравнение бессмысленно)."""
    for name, data in (("KNOWN_JS_ONLY", KNOWN_JS_ONLY), ("KNOWN_PY_ONLY", KNOWN_PY_ONLY)):
        for tab, keys in data.items():
            assert keys == sorted(set(keys)), f"{name}[{tab}] не отсортирован/с дублями"
        assert json.dumps(data, ensure_ascii=False)  # сериализуемы (для будущих выгрузок)
