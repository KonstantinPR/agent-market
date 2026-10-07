# -*- coding: utf-8 -*-
"""Манифест колонок: app/static/columns.json — единственный источник (этап 4).

Раньше ключи колонок жили в трёх местах: `registerColView` в app.js (панель
«Вид таблицы»), литеральные словари в app/api/*.py (Excel) и KNOWN-списки
расхождений здесь. Теперь и панель, и export-словари приходят из
columns.json, поэтому тест сверяет:

  1. внутренний паритет JSON: колонки всех режимов вкладки == объединение её
     export-словарей (ключ, которого нет в словаре, в Excel молча не
     печатается; ключ, которого нет в панели, не может включить пользователь);
  2. JSON ↔ app.js: registerColView настроен ровно на вкладки JSON, а подписи
     (k, label) панельных headers совпадают с modes;
  3. JSON ↔ Python: каждый вызов export_cols("...") ссылается на существующий
     словарь, и ни один словарь не остался без ссылок;
  4. санити JSON: без дублей ключей, def — булевы, группы ссылаются на
     существующие колонки, а в app/api не вернулись литеральные dict-ы.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COLUMNS_JSON = ROOT / "app" / "static" / "columns.json"
APP_JS = ROOT / "app" / "static" / "app.js"
APP_DIR = ROOT / "app"

# Вкладки дашборда: headers задаются точечными ссылками на объект dashHeaders
# (dashHeaders.tops / .price / .prefix) — читаем его поля по этому маппингу.
DASH_HEADER_FIELD = {
    "dash-profit": "tops",
    "dash-loss": "tops",
    "dash-price": "price",
    "dash-prefix": "prefix",
}

# Дополнительные переменные с headers, входящие в состав вкладки, но не
# упомянутые в её registerColView (колонки включаются другим чекбоксом).
EXTRA_JS_HEADERS = {
    "products": ["productsStockHeaders"],
}


def _load_json() -> dict:
    assert COLUMNS_JSON.exists(), "нет columns.json — единого источника колонок"
    return json.loads(COLUMNS_JSON.read_text(encoding="utf-8"))


def _balanced_block(text: str, open_pos: int, open_ch: str, close_ch: str) -> str:
    """Текст блока от open_pos (символ open_ch) до парной закрывающей скобки."""
    depth = 0
    for j in range(open_pos, len(text)):
        if text[j] == open_ch:
            depth += 1
        elif text[j] == close_ch:
            depth -= 1
            if depth == 0:
                return text[open_pos:j + 1]
    return ""


def _pairs(block: str) -> list:
    """Пары (k, label) из блока вида { k: "...", label: "..." , ... }."""
    return re.findall(r'\bk:\s*"([^"]+)",\s*label:\s*"([^"]*)"', block)


def js_headers(js: str) -> dict:
    """Имя переменной-массива -> список пар (k, label); плюс поля dashHeaders."""
    out = {}
    for m in re.finditer(r"(?:const|let|var)\s+(\w*Headers)\s*=\s*\[", js):
        out[m.group(1)] = _pairs(_balanced_block(js, m.end() - 1, "[", "]"))
    m = re.search(r"(?:const|let|var)\s+dashHeaders\s*=\s*\{", js)
    if m:
        obj = _balanced_block(js, m.end() - 1, "{", "}")
        for f in re.finditer(r"(\w+):\s*\[", obj):
            out["dashHeaders." + f.group(1)] = _pairs(
                _balanced_block(obj, f.end() - 1, "[", "]"))
    return out


def js_register_views(js: str) -> dict:
    """Вкладка -> список имён headers-переменных в её registerColView."""
    out = {}
    for m in re.finditer(r'registerColView\("([^"]+)",\s*\{', js):
        body = _balanced_block(js, m.end() - 1, "{", "}")
        out[m.group(1)] = re.findall(r"headers:\s*([A-Za-z_$][\w$.]*)", body)
    return out


def js_tab_pairs(js: str, tab: str, names: list) -> set:
    """Все пары (k, label) панельных колонок вкладки."""
    headers = js_headers(js)
    pairs = set()
    for name in names:
        if name.startswith("dashHeaders."):
            pairs |= set(headers.get(name, []))
        else:
            pairs |= set(headers.get(name, []))
    for extra in EXTRA_JS_HEADERS.get(tab, []):
        pairs |= set(headers.get(extra, []))
    return pairs


def json_tab_pairs(doc: dict, tab: str) -> set:
    pairs = set()
    for mode in doc["tabs"][tab]["modes"].values():
        pairs |= {(c["k"], c["label"]) for c in mode["columns"]}
    return pairs


def json_tab_keys(doc: dict, tab: str) -> set:
    keys = set()
    for mode in doc["tabs"][tab]["modes"].values():
        keys |= {c["k"] for c in mode["columns"]}
    return keys


def test_register_view_covers_json_tabs():
    """registerColView и columns.json описывают один и тот же набор вкладок."""
    doc = _load_json()
    views = js_register_views(APP_JS.read_text(encoding="utf-8"))
    assert set(views) == set(doc["tabs"]), (
        "вкладки app.js и columns.json разошлись:\n"
        "  только в app.js: %s\n  только в JSON:  %s"
        % (sorted(set(views) - set(doc["tabs"])),
           sorted(set(doc["tabs"]) - set(views)))
    )
    keys = [t.get("storageKey") for t in doc["tabs"].values()]
    assert len(keys) == len(set(keys)), "storageKey вкладок обязаны быть уникальны"
    assert all(keys), "у каждой вкладки должен быть storageKey"


def test_json_modes_match_export_dicts():
    """Колонки режимов вкладки == объединение её export-словарей (строго)."""
    doc = _load_json()
    for tab, tdef in sorted(doc["tabs"].items()):
        mode_keys = json_tab_keys(doc, tab)
        dict_names = tdef.get("exportDicts", [])
        if not dict_names:
            assert tab in DASH_HEADER_FIELD, (
                "вкладка %s без exportDicts — экспорта нет? "
                "добавьте её в DASH_HEADER_FIELD или дайте exportDicts" % tab
            )
            continue
        dict_keys = set()
        for name in dict_names:
            assert name in doc["dicts"], f"{tab}: нет словаря {name}"
            dict_keys |= {c["k"] for c in doc["dicts"][name]}
        assert mode_keys == dict_keys, (
            "вкладка %s: колонки режимов и export-словари разошлись.\n"
            "  только в режимах: %s\n  только в словарях: %s"
            % (tab, sorted(mode_keys - dict_keys), sorted(dict_keys - mode_keys))
        )


def test_js_headers_match_json_modes():
    """Подписи (k, label) панельных headers == колонки modes из JSON."""
    doc = _load_json()
    js = APP_JS.read_text(encoding="utf-8")
    views = js_register_views(js)
    problems = []
    for tab in sorted(views):
        js_pairs = js_tab_pairs(js, tab, views[tab])
        json_pairs = json_tab_pairs(doc, tab)
        if not js_pairs:
            problems.append("%s: headers app.js не разобраны" % tab)
            continue
        if js_pairs != json_pairs:
            only_json = sorted(json_pairs - js_pairs)[:6]
            only_js = sorted(js_pairs - json_pairs)[:6]
            problems.append(
                "%s: только в JSON: %s; только в app.js: %s" % (tab, only_json, only_js)
            )
    assert not problems, "панель и columns.json разошлись:\n  " + "\n  ".join(problems)


def test_all_keys_exist_somewhere_in_js():
    """Каждый ключ колонок JSON встречается в headers app.js (страховка)."""
    doc = _load_json()
    js = APP_JS.read_text(encoding="utf-8")
    missing = []
    for tab in sorted(doc["tabs"]):
        for key in sorted(json_tab_keys(doc, tab)):
            if ('k: "%s"' % key) not in js and ('k:"%s"' % key) not in js:
                missing.append((tab, key))
    assert not missing, "ключи колонок без представления в app.js: %s" % missing[:20]


def test_export_cols_refs_exist():
    """Вызовы export_cols("...") ссылаются на словари JSON, и все словари живы."""
    doc = _load_json()
    calls = set()
    for path in sorted(APP_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        src = path.read_text(encoding="utf-8", errors="replace")
        calls |= set(re.findall(r'export_cols\("([^"]+)"\)', src))
    dicts = set(doc["dicts"])
    unknown = sorted(calls - dicts)
    dead = sorted(dicts - calls)
    assert not unknown, "export_cols со ссылкой на несуществующий словарь: %s" % unknown
    assert not dead, "словари columns.json без использования в коде: %s" % dead


def test_no_literal_export_dicts_in_api():
    """В app/api не должны вернуться литеральные dict-ы в project_export."""
    problems = []
    for path in sorted((APP_DIR / "api").glob("*.py")):
        src = path.read_text(encoding="utf-8")
        for m in re.finditer(r"project_export\(\s*\w+,\s*\{", src):
            line = src.count("\n", 0, m.start()) + 1
            problems.append("%s:%d" % (path.name, line))
    assert not problems, (
        "литеральный словарь вместо export_cols(...) в: %s — "
        "источник колонок только columns.json" % problems
    )


def test_json_is_sane():
    """Дубли, типы и ссылки внутри columns.json."""
    doc = _load_json()
    problems = []
    for tab, tdef in sorted(doc["tabs"].items()):
        assert tdef.get("modes"), f"{tab}: нет режимов"
        for mname, mode in tdef["modes"].items():
            keys = [c["k"] for c in mode["columns"]]
            dups = sorted({k for k in keys if keys.count(k) > 1})
            if dups:
                problems.append("%s/%s: дубли %s" % (tab, mname, dups))
            for c in mode["columns"]:
                if not isinstance(c.get("def"), bool):
                    problems.append("%s/%s: %s.def не bool" % (tab, mname, c["k"]))
                if not c.get("label"):
                    problems.append("%s/%s: %s без подписи" % (tab, mname, c["k"]))
        for group in tdef.get("groups", []):
            for k in group["keys"]:
                if k not in json_tab_keys(doc, tab):
                    problems.append("%s: группа «%s» ссылается на %s" % (tab, group["title"], k))
    for name, entries in sorted(doc["dicts"].items()):
        keys = [c["k"] for c in entries]
        dups = sorted({k for k in keys if keys.count(k) > 1})
        if dups:
            problems.append("dicts[%s]: дубли %s" % (name, dups))
    assert not problems, "columns.json испорчен:\n  " + "\n  ".join(problems)
