"""Проверка чистых JS-хелперов (fmt/q/…) через Node vm.

Выполняется node --check (синтаксис) + прогон функций в изолированном
контексте с заглушкой DOM. Если node недоступен — тест пропускается.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

APP_JS = Path(__file__).resolve().parents[2] / "app" / "static" / "app.js"

HARNESS = r"""
const fs = require("fs");
const vm = require("vm");
const assert = require("assert");

const APP_PATH = process.argv[2];

const el = (sel) => ({
  value: "", checked: false, innerHTML: "", textContent: "", dataset: {},
  addEventListener() {}, appendChild() {}, setAttribute() {},
  classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  style: {}, files: { length: 0 }, disabled: false,
});
const doc = {
  querySelector: () => el(), querySelectorAll: () => [],
  getElementById: () => el(), createElement: () => el(),
  addEventListener() {},
};

const lsStore = {};
const sandbox = {
  document: doc, window: { addEventListener() {} }, location: { search: "" },
  fetch: async () => ({ ok: true, json: async () => ({}) }),
  alert() {}, confirm: () => true, URLSearchParams,
  console, Object, Array, Number, String, Math, Date, JSON, RegExp, Map, Set,
  Promise, setTimeout, clearTimeout, setInterval, clearInterval,
  require, process, __filename, __dirname,
  localStorage: {
    getItem: (k) => (k in lsStore ? lsStore[k] : null),
    setItem: (k, v) => { lsStore[k] = String(v); },
    removeItem: (k) => { delete lsStore[k]; },
  },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(APP_PATH, "utf8"), sandbox);

const { fmt, fmtMoney, fmtFloat, fmtPct, cls, qs, writeDbStorage, setWriteDbStorage } = sandbox;

const norm = (s) => s.replace(/[\u202F\u00A0]/g, " ");
assert.strictEqual(norm(fmt(1234)), "1 234", "fmt thousand separator");
assert.strictEqual(fmt(0), "0", "fmt zero");
assert.ok(fmtMoney(100).endsWith("\u20BD"));
assert.strictEqual(fmtPct(12.345), "12.3%", "fmtPct");
assert.strictEqual(fmtPct(""), "\u2014", "fmtPct empty");
assert.strictEqual(fmtPct("abc"), "\u2014", "fmtPct non-numeric");
assert.strictEqual(cls(-2), "neg", "cls negative");
assert.strictEqual(cls(5), "pos", "cls positive");
assert.strictEqual(
  qs({ a: 1, b: "", c: null, d: undefined, e: "x y" }),
  "?a=1&e=x+y",
  "qs drops empty and encodes"
);
assert.strictEqual(qs({}), "", "qs empty");

// «в разрезе размеров» OZON: переключатель в тулбаре каждого раздела, состояние
// своё у каждого (ключ localStorage «ozBySize:<tab>»), по умолчанию выключено.
const { ozBySize, setOzBySize, ozBySizeParam } = sandbox;
assert.strictEqual(ozBySize("oz-detail"), false, "по умолчанию свёрнуто по товарам");
assert.strictEqual(ozBySizeParam("oz-detail", "?a=1"), "?a=1", "by_size не добавляется");
assert.strictEqual(ozBySizeParam("oz-detail", ""), "", "пустой запрос остаётся пустым");
setOzBySize("oz-detail", true);
assert.strictEqual(ozBySize("oz-detail"), true, "режим по размерам включён");
assert.strictEqual(ozBySizeParam("oz-detail", "?a=1"), "?a=1&by_size=1", "by_size добавлен");
assert.strictEqual(ozBySizeParam("oz-detail", ""), "?by_size=1", "by_size без других параметров");
assert.strictEqual(ozBySize("oz-placement"), false, "состояние разделов независимо");
assert.strictEqual(ozBySizeParam("oz-placement", "?a=1"), "?a=1", "соседний раздел не затронут");
setOzBySize("oz-detail", false);
assert.strictEqual(ozBySize("oz-detail"), false, "режим по размерам выключен обратно");

assert.strictEqual(writeDbStorage("wb", "cards"), true, "write-db default true");
setWriteDbStorage("wb", "cards", false);
assert.strictEqual(writeDbStorage("wb", "cards"), false, "write-db off persisted");
setWriteDbStorage("wb", "cards", true);
assert.strictEqual(writeDbStorage("wb", "cards"), true, "write-db back on");
assert.strictEqual(writeDbStorage("ozon", "realization"), true, "write-db default separate kind");

// «Вид таблицы»: id кнопки/меню/панели, выводимые из tab, должны существовать в index.html.
const HTML_PATH = process.argv[3];
const htmlSrc = fs.readFileSync(HTML_PATH, "utf8");
const jsSrc = fs.readFileSync(APP_PATH, "utf8");
const apiSrc = fs.readFileSync(process.argv[4], "utf8");
const ccTab2 = (tab) => {
  const [head, ...rest] = tab.split("-");
  return head + rest.map((s) => s[0].toUpperCase() + s.slice(1)).join("");
};
const colTabs = [...jsSrc.matchAll(/registerColView\("([^"]+)"/g)].map((m) => m[1]);
assert.ok(colTabs.length >= 10, "registered col-view tabs: " + colTabs.length);
for (const t of colTabs) {
  const base = ccTab2(t);
  for (const id of ["btn" + base + "View", base + "ViewMenu", base + "ViewPanel"]) {
    assert.ok(htmlSrc.includes('id="' + id + '"'),
              "в index.html нет id для «Вид таблицы» таба " + t + ": " + id);
  }
}

// «Обновить базу»: кнопка есть в шапке, для каждой вкладки API — маршрут в app.js.
assert.ok(htmlSrc.includes('id="btnUpdateWbDetail"'), "в шапке нет кнопки «Обновить базу»");
for (const tab of ["wb-cards", "wb-stock", "wb-prices", "oz-cards", "oz-stock",
                   "oz-prices", "oz-realization", "oz-detail", "oz-cashflow"]) {
  assert.ok(jsSrc.includes('"' + tab + '":'),
            "в apiPullByTab нет маршрута для " + tab);
}

// Раздел OZON: кнопки «Скачать Excel» (wb-dl) допустимы только в export-only oz-cashflow.
// Остальные oz-панели — правило эталона: подсказка + тулбар таблицы (без дублей).
const ozPanes = [...htmlSrc.matchAll(/<section id="(tab-oz-[^"]+)" class="pane">([\s\S]*?)<\/section>/g)];
assert.ok(ozPanes.length >= 6, "раздел Ozon должен содержать 6 панелей, найдено: " + ozPanes.length);
for (const [, id, body] of ozPanes) {
  const hasDl = body.includes("wb-dl");
  if (id === "tab-oz-cashflow") {
    assert.ok(hasDl, "oz-cashflow остаётся export-only с кнопкой wb-dl");
  } else {
    assert.ok(!hasDl, "в " + id + " не должно быть wb-dl-кнопок (эталон: без дублей тулбара)");
  }
}

// Переключатель «в разрезе размеров» OZON: по одному в тулбаре каждого раздела,
// где есть свод по товару. Общего переключателя в меню больше нет.
for (const [tab, id] of [["margin-ozon-detail", "marginOzBySize"],
                          ["oz-detail", "ozDetailBySize"],
                          ["oz-placement", "ozPlacementBySize"]]) {
  assert.ok(htmlSrc.includes('id="' + id + '"'), "в index.html нет переключателя " + id);
  assert.ok(htmlSrc.includes('data-oz-size-for="' + tab + '"'),
            "нет переключателя по размерам для " + tab);
  assert.ok(jsSrc.includes('"' + tab + '": "' + id + '"'), "нет таба " + tab + " в OZ_BY_SIZE");
  assert.ok(jsSrc.includes('ozBySizeParam("' + tab + '"'),
            "ozBySizeParam должен получать tab: " + tab);
}
assert.ok(!htmlSrc.includes('id="ozBySize"'), "галочка #ozBySize должна быть убрана из меню OZON");
assert.ok(!jsSrc.includes('#ozBySize'), "js не должен ссылаться на #ozBySize");
assert.ok(jsSrc.includes('mode: () => (ozBySize("margin-ozon-detail")'),
          "набор колонок маржи должен зависеть от режима «в разрезе размеров»");
assert.ok(jsSrc.includes('function syncOzBySizeDisabled()'),
          "нет приглушения переключателя в режиме «Строками»");

// Раздел «Наш склад → Товары» (T-21): каталог, фильтры, «Вид таблицы», экспорт и цены.
assert.ok(jsSrc.includes("async function renderProducts"), "нет renderProducts");
assert.ok(jsSrc.includes('registerColView("products"'), "нет registerColView(\"products\")");
for (const id of ["productsLike", "productsSizes", "productsStocks", "productsOverwrite"]) {
  assert.ok(htmlSrc.includes('id="' + id + '"'), "в index.html нет id=" + id);
}
assert.ok(/renderProducts[\s\S]{0,600}api\("\/products"/.test(jsSrc),
          "renderProducts не вызывает GET /api/products");
assert.ok(jsSrc.includes('apiPost("/products/preview"'), "нет POST /api/products/preview");
assert.ok(jsSrc.includes('api("/products/price-settings"'), "нет GET /api/products/price-settings");
assert.ok(jsSrc.includes('"/api/export/products"'), "нет /api/export/products в app.js");
assert.ok(jsSrc.includes('"products": ["products", "refresh", "#productsMsg"]'),
          "нет маршрута POST /api/products/refresh в apiPullByTab");
assert.ok(apiSrc.includes('@router.post("/products/refresh")'), "в app/api.py нет POST /products/refresh");

console.log("JS_TESTS_OK");
"""


@pytest.fixture()
def node_bin():
    return shutil.which("node")


def test_js_syntax(node_bin):
    if not node_bin:
        pytest.skip("node недоступен")
    res = subprocess.run([node_bin, "--check", str(APP_JS)],
                         capture_output=True, text=True)
    assert res.returncode == 0, res.stderr


def test_js_helpers(node_bin, tmp_path):
    if not node_bin:
        pytest.skip("node недоступен")
    assert APP_JS.exists(), str(APP_JS)
    html_path = Path(__file__).resolve().parents[2] / "app" / "static" / "index.html"
    assert html_path.exists(), str(html_path)
    api_path = Path(__file__).resolve().parents[2] / "app" / "api.py"
    assert api_path.exists(), str(api_path)
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    res = subprocess.run([node_bin, str(harness), str(APP_JS), str(html_path), str(api_path)],
                         capture_output=True, text=True)
    assert res.returncode == 0, f"{res.stdout}\n{res.stderr}"
    assert "JS_TESTS_OK" in res.stdout