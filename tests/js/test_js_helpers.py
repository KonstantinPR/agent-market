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
assert.ok(jsSrc.includes('"products": ["products", "refresh", "#headerMsg"]'),
    "вкладка products в apiPullByTab пишет статус в шапку (#headerMsg)");
assert.ok(apiSrc.includes('@router.post("/products/refresh")'), "в app/api.py нет POST /products/refresh");

// T-29: статус разделов один — в шапке рядом с «Применить» (#headerMsg).
assert.ok(htmlSrc.includes('<span class="msg header-msg" id="headerMsg">'),
          "в шапке нет строки статуса #headerMsg");
// Статус должен идти после всех кнопок ряда (в т.ч. «Обновить базу»), а прижат
// к правому краю шапки через margin-left: auto в .header-msg.
const hdrEnd = htmlSrc.indexOf("</header>");
const hdr = htmlSrc.slice(htmlSrc.indexOf('<header class="filters">'), hdrEnd);
const order = ["id=\"btnApply\"", "id=\"btnUpdateWbDetail\"", "id=\"busySpinner\"",
               "id=\"headerMsg\""].map((k) => hdr.indexOf(k));
assert.ok(order.every((v) => v >= 0), "в шапке нет кнопок/статуса: " + order.join(","));
for (let i = 1; i < order.length; i++) {
  assert.ok(order[i] > order[i - 1],
    "статус должен идти после всех кнопок шапки, нарушен порядок: " + order.join(","));
}
const paneMsgSpans = htmlSrc.match(/<span[^>]*class="[^"]*\bmsg\b[^"]*"[^>]*>/g) || [];
assert.strictEqual(paneMsgSpans.length, 1,
  "в разметке должен остаться ровно один статус — #headerMsg, найдено: " + paneMsgSpans.length);
assert.ok(!htmlSrc.includes("data-date"),
  "в панелях не должно быть своих полей периода (data-date): период только в шапке");
assert.ok(jsSrc.includes("function paneDates()") &&
           /function paneDates\(\)[\s\S]{0,320}filters\(\)/.test(jsSrc),
  "paneDates должен брать период из шапки (filters), а не из панели");
assert.ok(jsSrc.includes("function clearStatus()"), "нет clearStatus");
assert.ok(/clearStatus\(\);\s*try \{/.test(jsSrc),
          "статус должен гаситься при загрузке вкладки (loadTabInner)");
const { setStatus, clearStatus, emptyPeriodReason, ozEmptyReason } = sandbox;
const headerEl = {
  textContent: "", dataset: {}, _err: false,
  classList: {
    toggle: (_c, v) => { headerEl._err = v; },
    add: (_c) => { headerEl._err = true; },
    remove: (c) => { if (c === "error") headerEl._err = false; },
    contains: (c) => (c === "error" ? headerEl._err : false),
  },
};
sandbox.document.getElementById = (id) => (id === "headerMsg" ? headerEl : null);
clearStatus();
setStatus("Строк: 12");
assert.strictEqual(headerEl.textContent, "Строк: 12", "setStatus пишет в #headerMsg");
setStatus("Ошибка: boom", { error: true });
assert.strictEqual(headerEl._err, true, "setStatus({error}) должен красить строку в ошибку");
setStatus("Строк: 1");
assert.strictEqual(headerEl._err, false, "error должен сниматься следующимsetStatus");
const long = "я".repeat(300);
setStatus(long);
assert.ok(headerEl.textContent.length < 300, "длинный статус должен обрезаться");
assert.strictEqual(headerEl.dataset.tip, long, "полный текст длинного статуса — в data-tip");
clearStatus();
assert.strictEqual(headerEl.textContent, "", "clearStatus очищает строку");
assert.ok(!("tip" in headerEl.dataset), "clearStatus убирает data-tip");

// Пустой результат объясняется фактами, а не зашитыми датами.
assert.ok(!jsSrc.includes("период с данными: 2026-02-21"),
          "в app.js не должно быть зашитого периода в тексте пустого раздела");
const win = { date_from: "2026-03-01", date_to: "2026-03-31" };
const emptyNoCover = { window: win, detail_range: { date_from: null, date_to: null, rows: 0 } };
assert.ok(/Пусто: детализации продаж Ozon в базе нет/.test(ozEmptyReason(emptyNoCover)),
          "нет текста «в базе нет»: " + ozEmptyReason(emptyNoCover));
const cover = { date_from: "2026-02-21", date_to: "2026-08-30", rows: 100 };
const covered = { window: win, detail_range: cover };
assert.ok(/за 2026-03-01 … 2026-03-31/.test(ozEmptyReason(covered)),
          "в тексте должно быть запрошенное окно: " + ozEmptyReason(covered));
assert.ok(/в базе покрыто 2026-02-21 … 2026-08-30/.test(ozEmptyReason(covered)),
          "в тексте должно быть покрытие базы: " + ozEmptyReason(covered));
assert.ok(/размещения Ozon/.test(emptyPeriodReason({ window: win, detail_range: cover }, "размещения Ozon", "Х")),
          "emptyPeriodReason должен называть раздел и брать подсказку");

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