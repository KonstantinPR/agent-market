# -*- coding: utf-8 -*-
"""Реестр localStorage (LS_KEYS / LS_COLVIEW_KEYS в app.js) ↔ фактические ключи.

Агент, добавляющий новое хранилище в браузере, обязан упомянуть ключ в реестре
с описанием — иначе тест падает. Реестр же служит готовой документацией
«что где живёт у пользователя в браузере» (см. AGENTS.md, «Тесты»).

Проверяются обе стороны:
  1) каждый фактический ключ покрыт реестром (точный, по префиксу «*» или
     как storageKey вкладки + опциональный «_<mode>»);
  2) в реестре нет мёртвых записей;
  3) неизвестные выражения вида `localStorage.getItem(someVar)` не проскользнут
     мимо (разрешены только именованные формы из ALLOWED_EXPR).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "app" / "static" / "app.js"

# Выражения в скобках localStorage-вызовов, которые осмысленно не являются
# литеральным ключом и покрыты реестром по-другому (см. _resolve).
ALLOWED_EXPR = {
    "set.key",  # colview: ключ = storageKey (+ «_<mode>»), считает colViewSet
    "k",        # цикл «сброса колонок»: перебор Object.keys(localStorage)
}

# ozBySizeKey(tab) → префикс "ozBySize:" (функция определена в app.js).
CALL_PREFIX = {"ozBySizeKey": "ozBySize:"}

# Первый аргумент: либо строковый литерал целиком, либо выражение до «)».
LS_CALL = re.compile(r'localStorage\.(?:getItem|setItem|removeItem)\(\s*("([^"]*)"|[^)]+)')
LITERAL = re.compile(r'^\s*"([^"]*)"')
NAME = re.compile(r"^\s*([A-Za-z_$][\w$]*)")
CONST_VALUE = re.compile(r'(?m)^const\s+([A-Z_][A-Z0-9_]*)\s*=\s*"([^"]*)"')
STORAGE_KEY = re.compile(r'storageKey:\s*"([^"]+)"')


def _object_body(src: str, name: str) -> str:
    """Тело литерального объекта `const <name> = {…}` (вложенные скобки — по счётчику)."""
    m = re.search(r"const\s+%s\s*=\s*\{" % re.escape(name), src)
    assert m, f"в app.js нет объекта {name}"
    depth = 0
    for j in range(m.end() - 1, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[m.end() - 1:j + 1]
    raise AssertionError(f"{name}: незакрытый литерал объекта")


def _array_body(src: str, name: str) -> str:
    m = re.search(r"const\s+%s\s*=\s*\[" % re.escape(name), src)
    assert m, f"в app.js нет массива {name}"
    depth = 0
    for j in range(m.end() - 1, len(src)):
        if src[j] == "[":
            depth += 1
        elif src[j] == "]":
            depth -= 1
            if depth == 0:
                return src[m.end() - 1:j + 1]
    raise AssertionError(f"{name}: незакрытый литерал массива")


def _load():
    src = APP_JS.read_text(encoding="utf-8")
    ls_keys = dict(re.findall(r'"([^"]*?)":\s*"([^"]*)"', _object_body(src, "LS_KEYS")))
    colview = re.findall(r'"([^"]+)"', _array_body(src, "LS_COLVIEW_KEYS"))
    consts = dict(CONST_VALUE.findall(src))
    return src, ls_keys, colview, consts


def _covers(ls_keys: dict, colview: list, key: str) -> bool:
    """Ключ покрыт реестром: точный, префикс «*», или storageKey вкладки (+ _mode)."""
    if key in ls_keys:
        return True
    for pattern in ls_keys:
        if pattern.endswith("*") and key.startswith(pattern[:-1]):
            return True
    for base in colview:
        if key == base or key.startswith(base + "_"):
            return True
    return False


def _resolve(expr: str, ls_keys, colview, consts):
    """Что за ключ скрывается за выражением в скобках localStorage-вызова."""
    expr = expr.strip()
    if expr in ALLOWED_EXPR:
        return None  # покрыто реестром по построению
    if expr.startswith('"'):
        return expr.strip('"')
    m = NAME.match(expr)
    if m and m.group(1) in consts:
        return consts[m.group(1)]
    call = re.match(r"^([A-Za-z_$][\w$]*)\(", expr)
    if call and call.group(1) in CALL_PREFIX:
        return CALL_PREFIX[call.group(1)]  # префикс-ключ
    raise AssertionError(
        "неизвестное выражение в localStorage-вызове: %r — либо осмысленный "
        "ключ не занесён в LS_KEYS, либо форму нужно добавить в ALLOWED_EXPR "
        "теста (с обоснованием)" % expr
    )


def _first_arg(raw: str) -> str:
    """Первый аргумент вызова: всё до первой запятой на верхнем уровне скобок."""
    depth = 0
    for i, ch in enumerate(raw):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == "," and depth == 0:
            return raw[:i]
    return raw


def _actual_keys(src, ls_keys, colview, consts):
    """Все фактические ключи: литералы, значения констант, storageKey колвью."""
    keys = set()
    for m in LS_CALL.finditer(src):
        resolved = _resolve(_first_arg(m.group(1)), ls_keys, colview, consts)
        if resolved is not None:
            keys.add(resolved)
    keys.update(STORAGE_KEY.findall(src))
    return keys


def test_every_actual_key_is_registered():
    """Каждый фактический ключ.localStorage обязан быть в реестре."""
    src, ls_keys, colview, consts = _load()
    missing = sorted(k for k in _actual_keys(src, ls_keys, colview, consts)
                     if not _covers(ls_keys, colview, k))
    assert not missing, (
        "ключи localStorage не занесены в LS_KEYS/LS_COLVIEW_KEYS (app.js) "
        "с описанием: %s" % missing
    )


def test_registry_has_no_dead_entries():
    """В реестре нет записей, которым больше не соответствует ни один ключ."""
    src, ls_keys, colview, consts = _load()
    actual = _actual_keys(src, ls_keys, colview, consts)

    dead_simple = sorted(k for k in ls_keys if not k.endswith("*") and k not in actual)
    dead_prefix = sorted(
        p for p in ls_keys if p.endswith("*")
        and not any(k.startswith(p[:-1]) for k in actual)
    )
    dead_colview = sorted(k for k in colview if k not in actual)
    assert not dead_simple and not dead_prefix and not dead_colview, (
        "мёртвые ключи в реестре: точные=%s префиксы=%s colview=%s"
        % (dead_simple, dead_prefix, dead_colview)
    )


def test_colview_registry_matches_register_col_view():
    """LS_COLVIEW_KEYS — ровно storageKey из registerColView (без лишних и без пропусков)."""
    src, _, colview, _ = _load()
    actual = sorted(STORAGE_KEY.findall(src))
    assert sorted(colview) == actual, (
        "расхождение LS_COLVIEW_KEYS ↔ registerColView:\n"
        "  лишние в реестре: %s\n  не занесены в реестр: %s"
        % (sorted(set(colview) - set(actual)), sorted(set(actual) - set(colview)))
    )


def test_registry_entries_have_descriptions():
    """Каждый пункт LS_KEYS задокументирован (значение — описание для агентов)."""
    _, ls_keys, _, _ = _load()
    empty = sorted(k for k, v in ls_keys.items() if not v.strip())
    assert not empty, "ключи без описания в LS_KEYS: %s" % empty
