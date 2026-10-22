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

const { fmt, fmtMoney, fmtFloat, fmtPct, cls, qs, writeDbStorage, setWriteDbStorage,
        pricingAgeText, likeMatch } = sandbox;

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

assert.ok(sandbox.xyzMatrixHtml, "есть xyzMatrixHtml");
assert.ok(sandbox.xyzClassShort, "есть xyzClassShort");
assert.ok(sandbox.xyzClassStats, "есть xyzClassStats");
assert.ok(sandbox.dashXyzNoteText, "есть dashXyzNoteText");

const xyzData = {
  rows: [
    {article:"A1", name:"Т1", abc:"A", xyz_class:"X", avg_month_qty:10, cv_pct:5, active_months:12, revenue:1000, margin:200},
    {article:"A2", name:"Т2", abc:"C", xyz_class:"NEW", avg_month_qty:1, cv_pct:0, active_months:2, revenue:50, margin:10},
  ],
  count: 2,
  meta: {marketplace:"WB", window_months:12, min_active_months:6, x_cv_threshold:15, y_cv_threshold:40},
  matrix: {
    abc_rows:["A","B","C"], xyz_cols:["X","Y","Z","NEW"],
    cells:[
      {abc:"A",xyz:"X",articles:1,revenue:1000,margin:200},
      {abc:"C",xyz:"NEW",articles:1,revenue:50,margin:10},
    ],
    totals:{articles:2,revenue:1050,margin:210},
  },
};
const mx = sandbox.xyzMatrixHtml(xyzData);
assert.ok(mx.includes("xyz-matrix"), "матрица рендерит таблицу");
assert.ok(mx.includes("A1") || mx.includes("A2") || mx.includes("арт"), "матрица содержит данные");
// Артикулы в матрице не выводятся в тексте ячеек — проверяем, что итоги/сумма по строке A есть.
assert.ok(mx.includes("Итого"), "матрица содержит итоги");

assert.strictEqual(sandbox.xyzClassShort("X"), "X");
assert.ok(["NEW", "Новинка"].includes(sandbox.xyzClassShort("NEW")), "xyzClassShort NEW");

// Раскрытие ячейки матрицы: у кликабельных ячеек есть data-abc/data-xyz.
assert.ok(sandbox.openXyzDrill, "есть openXyzDrill");
assert.ok(mx.includes('data-abc="A"') && mx.includes('data-xyz="X"'),
  "ячейки матрицы кликабельны (drill-down)");

// Порог «Новинка» (6/3/1) уходит из селектора в запрос к API.
assert.ok(sandbox.dashXyzQuery, "есть dashXyzQuery");
const _origQS = doc.querySelector;
doc.querySelector = (sel) => (sel === "#dashXyzMinActive"
  ? Object.assign(el(), { value: "3" }) : _origQS(sel));
let xyzQ = null;
try { xyzQ = sandbox.dashXyzQuery(); } finally { doc.querySelector = _origQS; }
assert.strictEqual(xyzQ.min_active_months, "3", "порог «Новинка» попадает в запрос");
assert.strictEqual(xyzQ.group_by, undefined, "group_by в XYZ-запросе не нужен");

// Фильтр по колонкам таблиц: числовые — numMatch (диапазон/оператор), текст —
// вхождение подстроки без регистра; «Итого» пересчитывается по отфильтрованным.
const { numMatch, filterActive, rowPassesFilters, recomputeFooters } = sandbox;
const fHeaders = [{ k: "article" }, { k: "stock", num: true }];
assert.strictEqual(filterActive(fHeaders, {}), false, "нет активных фильтров");
assert.strictEqual(filterActive(fHeaders, { stock: "   " }), false, "пробел не фильтр");
assert.strictEqual(filterActive(fHeaders, { article: "a" }), true, "текстовый фильтр активен");
assert.strictEqual(numMatch(120, ">100"), true, "numMatch >");
assert.strictEqual(numMatch(50, ">100"), false, "numMatch > reject");
assert.strictEqual(numMatch(30, "10..50"), true, "numMatch диапазон");
assert.strictEqual(numMatch(5, "10..50"), false, "numMatch диапазон reject");
const fRows = [
  { article: "Apple", stock: 10 },
  { article: "Pear", stock: 200 },
  { article: "apricot", stock: 3 },
];
assert.strictEqual(
  fRows.filter((r) => rowPassesFilters(r, fHeaders, { article: "pea" })).length, 1,
  "подстрока без учёта регистра (Pear)");
assert.strictEqual(
  fRows.filter((r) => rowPassesFilters(r, fHeaders, { article: "APPLE" })).length, 1,
  "регистронезависимо (APPLE → Apple)");
assert.strictEqual(
  fRows.filter((r) => rowPassesFilters(r, fHeaders, { stock: ">100" })).length, 1,
  "числовой оператор в rowPassesFilters");
const jf = (o) => JSON.stringify(o);
assert.strictEqual(jf(recomputeFooters(fHeaders, { stock: 213 }, fRows)), jf({ stock: 213 }),
  "сумма по всем строкам");
assert.strictEqual(jf(recomputeFooters(fHeaders, { stock: 213 }, fRows.slice(0, 2))), jf({ stock: 210 }),
  "сумма по отфильтрованным");
assert.strictEqual(jf(recomputeFooters(fHeaders, { stock: { avg: 71 } }, fRows.slice(0, 2))),
  jf({ stock: { avg: 105 } }), "среднее по отфильтрованным");
assert.strictEqual(jf(recomputeFooters(fHeaders, { stock: 213 }, [])), jf({}),
  "пустой набор — пустые итоги");

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
const colsJson = JSON.parse(fs.readFileSync(process.argv[6], "utf8"));
const ccTab2 = (tab) => {
  const [head, ...rest] = tab.split("-");
  return head + rest.map((s) => s[0].toUpperCase() + s.slice(1)).join("");
};
const colTabs = [...jsSrc.matchAll(/registerColView\("([^"]+)"/g)].map((m) => m[1]);
assert.ok(colTabs.length >= 10, "registered col-view tabs: " + colTabs.length);
// Обзор: одна таблица с переключателем, поэтому у dash-* видов общий
// «Вид таблицы» (btn/menu/panel) — производные id для них не нужны.
assert.ok(htmlSrc.includes('id="btndashActiveView"')
  && htmlSrc.includes('id="dashActiveViewMenu"')
  && htmlSrc.includes('id="dashActiveViewPanel"'),
  "в index.html нет общего «Вид таблицы» для таблиц обзора");
for (const t of colTabs) {
  if (t.startsWith("dash-")) continue;
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

// «Проверить пропущенные детализации» (WB → Детализация продаж): кнопка обязана
// вызывать checkWbDetailGaps. Функция была объявлена, но нигде не подключена —
// клик не давал никакого результата.
assert.ok(htmlSrc.includes('id="wbGapsCheck"'), "в шапке нет кнопки wbGapsCheck");
assert.ok(htmlSrc.includes('id="wbGapsDownload"'), "в WB-детализации нет кнопки wbGapsDownload");
assert.ok(htmlSrc.includes('id="wbGapsBox"'), "нет контейнера wbGapsBox для таблицы пропусков");
assert.ok(htmlSrc.includes('id="wbGapsInfo"'), "нет строки статуса wbGapsInfo");
assert.ok(jsSrc.includes('async function checkWbDetailGaps()'),
          "нет функции checkWbDetailGaps");
assert.ok(jsSrc.includes('addEventListener("click", () => checkWbDetailGaps())'),
          "кнопка «Проверить пропущенные детализации» не вызывает checkWbDetailGaps");
assert.ok(jsSrc.includes('addEventListener("click", () => downloadMissingWbDetail())'),
          "кнопка «Догрузить пропущенные» не вызывает downloadMissingWbDetail");
assert.ok(jsSrc.includes('api("/wb/detail-gaps"'),
          "checkWbDetailGaps должен обращаться к GET /api/wb/detail-gaps");
assert.ok(apiSrc.includes('@router.get("/wb/detail-gaps")'),
          "в app/api/ нет маршрута GET /wb/detail-gaps");

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
assert.ok(apiSrc.includes('@router.post("/products/refresh")'), "в app/api/ нет POST /products/refresh");

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
// Статус разделов один — в шапке (#headerMsg); в ящике кабинетов допускается
// собственный локальный статус редактора (#cabEditorMsg).
const statusSpans = paneMsgSpans.filter((s) => !/id="cabEditorMsg"/.test(s));
assert.strictEqual(statusSpans.length, 1,
  "в разметке должен остаться ровно один статус — #headerMsg, найдено: " + statusSpans.length);
assert.ok(paneMsgSpans.some((s) => /id="headerMsg"/.test(s)),
  "в шапке нет статуса #headerMsg");
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

// Регрессия T-31: normalizeStatus обязан сходиться. Обрезка даёт
// HEADER_MSG_MAX + 1 символ, то есть снова проходит проверку
// s.length > HEADER_MSG_MAX, и MutationObserver на #headerMsg будит
// normalizeStatus снова и снова. Так зависал главный поток на вкладке
// «Наш склад → Потребность в товаре» — единственном разделе, где статус
// длиннее 150 символов: страница отвечала «не отвечает, ждать дальше».
const { normalizeStatus } = sandbox;
headerEl.textContent = long;
let passes = 0;
let prev = null;
while (passes < 10) {
  normalizeStatus();
  passes++;
  if (headerEl.textContent === prev) break;
  prev = headerEl.textContent;
}
assert.ok(passes <= 2, "normalizeStatus должен сойтись за один проход, крутился " + passes + " раз");
assert.strictEqual(headerEl.textContent.length, 151,
  "статус должен быть 150 символов + многоточие");
assert.strictEqual(headerEl.dataset.tip, long,
  "повторный проход не затирает подсказку обрезанным текстом");
clearStatus();

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

// Актуальность снимка цен WB: расчёт идёт по базе, плашка показывает её свежесть.
assert.strictEqual(pricingAgeText(0.5), "30 мин", "полчаса -> минуты");
assert.strictEqual(pricingAgeText(5), "5 ч", "часы");
assert.strictEqual(pricingAgeText(72), "3 дн", "сутки");
assert.ok(htmlSrc.includes('id="pricingPricesAge"'), "в index.html есть плашка актуальности цен");
assert.ok(htmlSrc.includes('id="pricingPriceRefresh"'), "в index.html есть кнопка обновления цен");
assert.ok(jsSrc.includes("/api/wb/prices"), "кнопка зовёт существующий эндпоинт цен");
// Автопилот цен следует маркетплейсу активной связки: у Ozon-связки
// автоматически выбрана кнопка Ozon (иначе по умолчанию открывался WB).
const cabFn = jsSrc.slice(jsSrc.indexOf("async function loadCabinets"),
                          jsSrc.indexOf("function fillReplenishCabinet"));
assert.ok(cabFn.includes('cabMp === "ozon"') && cabFn.includes('setPricingMp("oz")'),
  "loadCabinets должен выбирать Ozon по маркетплейсу активной связки");
assert.ok(cabFn.includes("syncPricingMpUi()"),
  "loadCabinets должен синхронизировать кнопки маркетплейса автопилота цен");

// «Ключи и настройки связки»: шестерёнка внутри .cab-item[data-cab] не должна
// проваливаться в выбор связки (тот закрывает дровер и настройки не видны).
const cabClick = jsSrc.slice(jsSrc.indexOf('cabList.addEventListener("click"'),
                            jsSrc.indexOf("selectCabinet(parseInt(item.dataset.cab"));
assert.ok(cabClick.includes("data-cab-edit") &&
          cabClick.indexOf("data-cab-edit") < cabClick.indexOf('closest("[data-cab]")'),
  "клик по шестерёнке «Ключи и настройки связки» обрабатывается после выбора связки");
assert.ok(cabClick.includes("openCabEditor("),
  "шестерёнка не открывает редактор ключей связки");
assert.ok(jsSrc.includes('$("#cabEditorClose")') &&
          jsSrc.includes('cabEdClose.addEventListener("click", closeCabEditor)'),
  "кнопка «×» редактора ключей не подключена");
assert.ok(jsSrc.includes('cabEdSave.addEventListener("click", saveCabEditor)'),
  "кнопка «Сохранить» редактора ключей не подключена");
assert.ok(jsSrc.includes('inp.dataset.cleared = "1"'),
  "удаление ключа не помечает поле очищенным (сохранится старое значение)");

// T-46: поля воронки продаж в таблице автопилота (WB) + ярлык решения MIN.
assert.ok(jsSrc.includes('{ k: "orders", label: "Заказы"'),
  "в pricingHeaders нет колонки «Заказы»");
assert.ok(jsSrc.includes('{ k: "views", label: "Просмотры"') &&
          jsSrc.includes('{ k: "adds", label: "Добавления в корзину"') &&
          jsSrc.includes('{ k: "cancelled", label: "Отмены"'),
  "в pricingHeaders нет полей воронки (просмотры/корзина/отмены)");
assert.ok(jsSrc.includes('label: "Просмотр\u2192Корзина, %"') &&
          jsSrc.includes('label: "Корзина\u2192Заказ, %"') &&
          jsSrc.includes('{ k: "avg_orders_per_day", label: "Заказов в день"'),
  "в pricingHeaders нет конверсий/скорости заказов из воронки");
assert.ok(jsSrc.includes('MIN: { txt: "минимальная цена", cls: "p-min" }'),
  "PRICING_ACTION не содержит ярлык решения MIN");
const pxDict = (colsJson.dicts || {}).pricing_export || [];
for (const k of ["views", "adds", "orders", "cancelled",
                 "conv_to_cart_percent", "conv_cart_to_order_percent",
                 "avg_orders_per_day"]) {
  assert.ok(pxDict.some((c) => c.k === k),
    "в Excel-словаре pricing_export нет колонки " + k);
}

// Меню PDF «Потребности»: галка прибыльности включена по умолчанию, едет в URL.
const { pdfParams, savePdfParams } = sandbox;
assert.strictEqual(pdfParams().profit, true, "учёт прибыльности включён по умолчанию");
assert.strictEqual(pdfParams().velDays, 180, "окно скорости по умолчанию 180 дней");
const pdfP = pdfParams();
pdfP.profit = false;
savePdfParams(pdfP);
assert.strictEqual(pdfParams().profit, false, "галка запоминается в localStorage");
pdfP.profit = true;
savePdfParams(pdfP);
assert.strictEqual(pdfParams().profit, true, "и обратно включается");
assert.ok(jsSrc.includes('params.set("profit", p.profit ? 1 : 0)'),
  "replenishPdfUrl должен передавать profit как 1/0");
assert.ok(jsSrc.includes("Учитывать прибыльность"),
  "в меню PDF есть галка «Учитывать прибыльность»");

// Скачать PDF — только из меню «PDF ▾»: дубль в тулбаре удалён, он же и «протухал».
assert.ok(htmlSrc.includes('id="btnReplenishPdf"'), "меню «PDF ▾» осталось");
assert.ok(htmlSrc.includes('id="exportReplenish"'), "экспорт в Excel на месте");
assert.ok(!htmlSrc.includes('id="exportReplenishPdf"'),
  "в тулбаре не должно быть второй кнопки выгрузки PDF");
assert.ok(!jsSrc.includes('const expPdf = $("#exportReplenishPdf")'),
  "app.js не должен обновлять href удалённой ссылки");

// Чекбокс «без нулевых размеров» в разминке «Размеры»: по умолчанию снят
// (показываются все размеры, включая «везде 0»), при включении скрывает
// размеры без продаж и остатков.
assert.ok(htmlSrc.includes('id="replenishHideZeroSizes"'),
  "в тулбаре Потребности есть чекбокс «без нулевых размеров»");
assert.ok(htmlSrc.includes("без нулевых размеров"),
  "подпись чекбокса про нулевые размеры на месте");
assert.ok(jsSrc.includes('hide_zero_sizes: $("#replenishHideZeroSizes")'),
  "app.js передаёт hide_zero_sizes в запрос Потребности");
assert.ok(apiSrc.includes("hide_zero_sizes: int = 0"),
  "в /api/replenish есть параметр hide_zero_sizes");

// «Доп. настройки ▾»: «Неактуальные», «без нулевых размеров» и окна
// спроса/запаса свёрнуты в одно меню в стиле «PDF ▾».
assert.ok(htmlSrc.includes('id="btnReplenishExtra"') &&
  htmlSrc.includes('id="replenishExtraPanel"'),
  "настройки Потребности свёрнуты в меню «Доп. настройки»");
const extraStart = htmlSrc.indexOf('id="replenishExtraPanel"');
const extraHtml = htmlSrc.slice(extraStart, htmlSrc.indexOf('id="replenishPdfMenu"'));
for (const id of ["replenishShowInactive", "replenishHideZeroSizes",
  "replenishWindow", "replenishTarget"]) {
  assert.ok(extraHtml.includes('id="' + id + '"'),
    id + " должен быть внутри «Доп. настроек»");
}
assert.ok(!htmlSrc.slice(0, extraStart).includes('id="replenishShowInactive"'),
  "галка «Неактуальные» убрана из видимого тулбара");
assert.ok(jsSrc.includes("function initReplenishExtraMenu") &&
  jsSrc.includes("initReplenishExtraMenu();"),
  "меню «Доп. настройки» инициализируется");
assert.ok(jsSrc.includes('"replenishHideZeroSizes", "replenishWish"'),
  "галка нулевых размеров перезагружает таблицу при переключении");

// «Потребность в товаре» — первый пункт меню «Наш Склад».
const oursMenu = htmlSrc.slice(htmlSrc.indexOf('id="oursMenu"'),
  htmlSrc.indexOf('id="profitDropdown"'));
const replenishPos = oursMenu.indexOf('data-tab="replenish"');
const productsPos = oursMenu.indexOf('data-tab="products"');
assert.ok(replenishPos >= 0 && replenishPos < productsPos,
  "«Потребность в товаре» первая в меню «Наш Склад»");

// «Пожелание» (T-43): ручной объём отгрузки на WB + шаг размера. Клиентский
// parseWishStep обязан совпадать с серверным parse_wish_step.
assert.ok(htmlSrc.includes('id="replenishWish"'),
  "в тулбаре Потребности нет поля «Пожелание»");
assert.ok(htmlSrc.includes('id="replenishWishStep"'),
  "в тулбаре Потребности нет селектора шага размера");
assert.ok(htmlSrc.includes('id="replenishWishMenu"') && htmlSrc.includes('id="btnReplenishWish"'),
  "«Пожелание» не свёрнуто в меню-селектор (как PDF)");
assert.ok(jsSrc.includes("function initReplenishWishMenu") &&
  jsSrc.includes("initReplenishWishMenu()"),
  "нет инициализации меню «Пожелание»");
assert.ok(htmlSrc.indexOf('id="replenishWishMenu"') < htmlSrc.indexOf('id="replenishPdfMenu"'),
  "меню «Пожелание» должно стоять рядом с «PDF ▾» (перед ним)");
// Runtime assertions: функции должны быть глобальными (не вложены)
assert.strictEqual(typeof sandbox.initReplenishWishMenu, "function",
  "initReplenishWishMenu не глобальная функция");
assert.strictEqual(typeof sandbox.syncReplenishWishBtn, "function",
  "syncReplenishWishBtn не глобальная функция");
assert.ok(jsSrc.includes('q.wish = wish') && jsSrc.includes('q.wish_step ='),
  "replenishQs не передаёт wish/wish_step в запрос");
assert.ok(apiSrc.includes("wish: int = 0"), "в /api/replenish нет параметра wish");
assert.ok(apiSrc.includes("parse_wish_step"), "эндпоинт не валидирует шаг через parse_wish_step");
const { parseWishStep } = sandbox;
const step = (s) => [...parseWishStep(s)];
assert.deepStrictEqual(step("2"), [2, 2], "шаг 2 → [2,2]");
assert.deepStrictEqual(step("2-4"), [2, 4], "шаг 2-4 → [2,4]");
assert.deepStrictEqual(step(""), [1, 1], "пустой шаг → все размеры [1,1]");
assert.deepStrictEqual(step(null), [1, 1], "null → все размеры");
for (const bad of ["0", "-1", "4-2", "a", "2-4-6", "2-", "x-y"]) {
  let threw = false;
  try { parseWishStep(bad); } catch (e) { threw = true; }
  assert.ok(threw, "parseWishStep должен ругаться на " + JSON.stringify(bad));
}

// «Дослать» — колонка плана подсортировки (как PDF) в обоих видах и в Excel.
assert.ok(jsSrc.includes('{ k: "to_sort", label: "Дослать"'),
  "в «По артикулам» нет колонки «Дослать»");
assert.ok((jsSrc.match(/\{ k: "to_sort", label: "Дослать"/g) || []).length === 2,
  "«Дослать» должна быть и в «По артикулам», и в «По размерам»");
assert.ok(jsSrc.includes('to_sort: _footSum(rows, "to_sort")'),
  "в итогах Потребности нет суммы «Дослать»");

// Итоги Потребности: суммы по аддитивным колонкам + среднее по неаддитивным.
// В размерном виде склад/маржа/рентабельность заданы по артикулу целиком и
// повторяются в каждом размере — берём их по уникальным артикулам (иначе 2×/N×).
const footRows = [
  { article: "A", our_stock: 10, our_cost: 100, margin_per_one: 2, margin_pct: 10,
    wb_sells: 3, wb_net: 3, wb_ret: 0, wb_qty: 5, sells: 3, returns_qty: 0,
    income: 900, oz_qty: 1 },
  { article: "A", our_stock: 10, our_cost: 100, margin_per_one: 2, margin_pct: 10,
    wb_sells: 0, wb_net: 0, wb_ret: 1, wb_qty: 2, sells: 3, returns_qty: 0,
    income: 900, oz_qty: 1 },
  { article: "B", our_stock: 7, our_cost: 200, margin_per_one: 4, margin_pct: 20,
    wb_sells: 1, wb_net: 1, wb_ret: 0, wb_qty: 4, sells: 1, returns_qty: 2,
    income: 300, oz_qty: 3 },
];
const fa = sandbox.replenishFooters(footRows);
assert.strictEqual(fa.sells, 7, "итог «Продано, шт» — сумма");
assert.strictEqual(fa.returns_qty, 2, "итог «Возвраты, шт» — сумма");
assert.strictEqual(fa.income, 2100, "итог «К перечислению» — сумма");
assert.strictEqual(fa.oz_qty, 5, "итог «Ozon склад» — сумма");
assert.strictEqual(fa.wb_qty, 11, "итог «WB склад» — сумма");
assert.strictEqual(fa.our_cost.avg, 400 / 3, "итог «Себест-ть» — среднее (≈)");

const fsz = sandbox.replenishSizeFooters(footRows);
assert.strictEqual(fsz.our_stock, 17, "склад по уникальным артикулам (A=10, B=7)");
assert.strictEqual(fsz.margin_per_one.avg, 3, "маржа/шт по уникальным артикулам ((2+4)/2)");
assert.strictEqual(fsz.margin_pct.avg, 15, "рент-сть по уникальным артикулам ((10+20)/2)");
assert.strictEqual(fsz.wb_net, 4, "итог «Продажи WB нетто» — сумма");
assert.strictEqual(fsz.wb_ret, 1, "итог «Возвраты WB» — сумма");
assert.strictEqual(fsz.wb_qty, 11, "итог «WB склад» (размеры) — сумма");
assert.ok(!("margin" in fsz), "в размерном виде нет мёртвого итога «Маржа руб»");

// Итоги «WB API ▸ Остатки»: доступно/всего на складах/в пути — сумма.
const sf = sandbox.stockFooters([
  { quantity: 5, quantity_full: 9, in_way: 3 },
  { quantity: 2, quantity_full: 11, in_way: 3 },
]);
assert.strictEqual(sf.quantity, 7, "итог «Кол-во/Доступно» — сумма");
assert.strictEqual(sf.quantity_full, 20, "итог «Всего на складах» — сумма");
assert.strictEqual(sf.in_way, 6, "итог «В пути» — сумма");
assert.strictEqual(sandbox.stockFooters([]), null,
  "пустой срез остатков — без строки «Итого»");
assert.ok(jsSrc.includes("stockFooters(rows), null, colViewPinKeys(\"wb-stock\")"),
  "в «WB API ▸ Остатки» итоги не передаются в pagedTable");
assert.strictEqual((jsSrc.match(/stockFooters\(rows\)/g) || []).length, 4,
  "итоги остатков должны быть во всех трёх таблицах (Остатки, WB, Ozon)");

// Итоги продаж: количество/выручка/к перечислению — суммы.
const slf = sandbox.salesFooters([
  { quantity: 2, revenue: 1000, income: 500 },
  { quantity: 3, revenue: 1500, income: 700 },
]);
assert.strictEqual(slf.quantity, 5, "итог «Продано, шт» — сумма");
assert.strictEqual(slf.revenue, 2500, "итог «Выручка» — сумма");
assert.strictEqual(slf.income, 1200, "итог «К перечислению» — сумма");
assert.strictEqual((jsSrc.match(/salesFooters\(rows\)/g) || []).length, 4,
  "итоги продаж должны быть во всех таблицах продаж (общая, WB, Ozon)");

// Итоги цен: цена за единицу и скидка — среднее (≈).
const pf = sandbox.priceFooters([
  { price: 1000, discounted_price: 900, discount: 10 },
  { price: 2000, discounted_price: 1500, discount: 25 },
]);
assert.strictEqual(pf.price.avg, 1500, "итог «Цена без скидки» — среднее");
assert.strictEqual(pf.discounted_price.avg, 1200, "итог «Цена со скидкой» — среднее");
assert.strictEqual(pf.discount.avg, 17.5, "итог «Скидка, %» — среднее");
assert.ok(pf.discount.pct, "среднее скидки рендерится как процент");
assert.strictEqual((jsSrc.match(/priceFooters\(rows\)/g) || []).length, 3,
  "итоги цен должны быть в WB и Ozon, но не в свёрнутом виде");

// Хранение WB: баркоды/объём/сумма хранения — сумма, цена за баркод — среднее.
const stf = sandbox.storageFooters([
  { barcodes_count: 2, volume: 1.5, storage_price: 3, warehouse_price: 6 },
  { barcodes_count: 1, volume: 2.5, storage_price: 5, warehouse_price: 5 },
]);
assert.strictEqual(stf.barcodes_count, 3, "итог «Баркодов» — сумма");
assert.strictEqual(stf.volume, 4, "итог «Объём, л» — сумма");
assert.strictEqual(stf.warehouse_price, 11, "итог «Сумма хранения» — сумма");
assert.strictEqual(stf.storage_price.avg, 4, "итог «Хранение за баркод» — среднее");

// Наш склад: документы и их строки.
const wdf = sandbox.whDocFooters([
  { total: 100, items_count: 2 }, { total: 50, items_count: 1 },
]);
assert.strictEqual(wdf.total, 150, "итог «Сумма» документа — сумма");
assert.strictEqual(wdf.items_count, 3, "итог «Строк» — сумма");
const wif = sandbox.whDocItemFooters([
  { quantity: 2, price: 100, amount: 200 },
  { quantity: 3, price: 200, amount: 600 },
]);
assert.strictEqual(wif.quantity, 5, "итог «Кол-во» строк — сумма");
assert.strictEqual(wif.amount, 800, "итог «Сумма» строк — сумма");
assert.strictEqual(wif.price.avg, 150, "итог «Цена» строк — среднее");

// Воронка прибыльности: количества/деньги — сумма, проценты/цены — среднее.
const mff = sandbox.marginFunnelFooters([
  { views: 100, orders: 10, revenue: 5000, margin: 1000, margin_pct: 20,
    avg_price: 500, net_cost: 100, nm_id: 111 },
  { views: 200, orders: 20, revenue: 9000, margin: 2000, margin_pct: 22,
    avg_price: 450, net_cost: 200, nm_id: 222 },
]);
assert.strictEqual(mff.views, 300, "итог «Просмотры» — сумма");
assert.strictEqual(mff.revenue, 14000, "итог «Выручка» — сумма");
assert.strictEqual(mff.margin, 3000, "итог «Маржа» — сумма");
assert.strictEqual(mff.margin_pct.avg, 21, "итог «Маржа, %» — среднее");
assert.strictEqual(mff.avg_price.avg, 475, "итог «Ср. цена» — среднее");
assert.strictEqual(mff.net_cost.avg, 150, "итог «Себестоимость» — среднее");
assert.ok(!("nm_id" in mff), "nm_id не суммируется в итогах воронки");

const toSortLabels = ["_REPLENISH_EXPORT", "_REPLENISH_EXPORT_SIZES"].map(
  (n) => (colsJson.dicts[n] || []).find((c) => c.k === "to_sort")?.label);
assert.deepStrictEqual(toSortLabels, ["Дослать", "Дослать"],
  "в Excel-словарях columns.json нет колонки «Дослать» (article + sizes)");

// Поля карточки PDF = колонки «Вида таблицы» (отдельного списка галочек нет).
const pdfColsOf = sandbox.replenishPdfCols;
assert.ok(!jsSrc.includes("Поля в карточке"),
  "дубль «Поля в карточке» убран из меню PDF");
assert.ok(jsSrc.includes("Поля карточки — колонки во «Вид таблицы»"),
  "в меню PDF осталась подсказка про «Вид таблицы»");
assert.strictEqual(pdfParams().photoCount, 6, "по умолчанию 6 фото на карточку");
assert.ok(!("cols" in pdfParams()),
  "сохранённый когда-то ключ cols больше не читается");
assert.strictEqual(sandbox.colViewState("replenish", "article").name, true,
  "«Наименование» видимо по умолчанию");
assert.ok(pdfColsOf().includes("wb_sells"), "новая «Продано WB» видна по умолчанию");
assert.ok(!pdfColsOf().includes("article"), "артикул печатаётся отдельной строкой");
sandbox.localStorage.setItem("replenishCols_article",
  JSON.stringify({ name: false, wb_sells: false }));
const colsHidden = pdfColsOf();
assert.ok(!colsHidden.includes("name"), "выключенная колонка ушла из карточки");
assert.ok(!colsHidden.includes("wb_sells"), "…в том числе новая «Продано WB»");
assert.ok(colsHidden.includes("demand"), "остальные видимые колонки остались");
sandbox.localStorage.removeItem("replenishCols_article");
sandbox.localStorage.setItem("replenishCols_sizes", JSON.stringify({ name: false }));
assert.ok(pdfColsOf().includes("name"),
  "состояние вида «По размерам» не влияет на карточку PDF");
sandbox.localStorage.removeItem("replenishCols_sizes");
savePdfParams({ cols: ["demand"], photoCount: 4 });
assert.strictEqual(pdfParams().photoCount, 4, "выбранное число фото запоминается");
assert.ok(pdfColsOf().includes("name"), "старый cols не перекрывает «Вид таблицу»");
savePdfParams({});
assert.strictEqual(pdfParams().photoCount, 6, "после сброса — снова 6 фото по умолчанию");

// Режим «для поставщика»: галка в меню PDF, параметр supplier в URL.
assert.strictEqual(pdfParams().supplier, false, "режим поставщика по умолчанию выключен");
savePdfParams({ supplier: true });
assert.strictEqual(pdfParams().supplier, true, "галка поставщика читается из localStorage");
savePdfParams({ supplier: "yes" });
assert.strictEqual(pdfParams().supplier, false, "нестроковое true не включает режим");
savePdfParams({});
assert.strictEqual(pdfParams().supplier, false, "сброс выключает режим поставщика");
assert.ok(jsSrc.includes("Для поставщика (без аналитики)"),
  "в меню PDF есть галка «Для поставщика»");
assert.ok(jsSrc.includes('params.set("supplier", p.supplier ? 1 : 0)'),
  "URL PDF содержит параметр supplier");

// PDF из Excel: дропзона в меню, распознавание файла, сводка предпросмотра.
const { isExcelFileName, replenishExcelSummary } = sandbox;
assert.strictEqual(isExcelFileName("potrebnost_2026-09-01_2026-09-30.xlsx"), true,
  "выгрузка .xlsx распознаётся");
assert.strictEqual(isExcelFileName("FILE.XLSX"), true, "расширение без учёта регистра");
assert.strictEqual(isExcelFileName("old.xls"), false, "старый .xls не принимается");
assert.strictEqual(isExcelFileName("table.csv"), false, "csv не принимается");
assert.strictEqual(isExcelFileName(""), false, "пустое имя — не файл");
const excelSum = replenishExcelSummary({
  name: "правки.xlsx",
  meta: { count: 57, unknown: ["Заметки"], dropped: 2, truncated: 1 },
});
assert.ok(excelSum.includes("правки.xlsx") && excelSum.includes("57"),
  "сводка называет файл и число карточек");
assert.ok(excelSum.includes("Заметки"), "сводка показывает нераспознанные колонки");
assert.ok(excelSum.includes("без артикула"), "сводка считает строки без артикула");
assert.ok(excelSum.includes("обрезано"), "сводка считает обрезанные строки");
assert.ok(jsSrc.includes('"/api/replenish/import-excel"'),
  "app.js отправляет файл на /api/replenish/import-excel");
assert.ok(jsSrc.includes("JSON.stringify({ rows: x.rows, source: x.name })"),
  "PDF из файла грузится POST-ом со строками");
assert.ok(jsSrc.includes('accept = ".xlsx'), "file input принимает только xlsx");
assert.ok(jsSrc.includes("Excel с правками: перетащите файл сюда"),
  "дропзона есть в меню PDF");
assert.ok(jsSrc.includes('"Скачать PDF из Excel"'),
  "кнопка подписывает режим файла");
assert.ok(apiSrc.includes('@router.post("/replenish/import-excel")'),
  "в app/api/ есть POST /replenish/import-excel");
assert.ok(apiSrc.includes('@router.post("/export/replenish/pdf")'),
  "в app/api/ есть POST /export/replenish/pdf для строк файла");
assert.ok(apiSrc.includes("apply_sort_budget"),
  "бюджет «Итого дослать» применяется в PDF-сборке");
const excelCss = fs.readFileSync(process.argv[5], "utf8"); // cssSrc объявится ниже
if (!/\.pdf-drop\s*\{/.test(excelCss)) throw new Error("нет CSS-правил для .pdf-drop");
if (!excelCss.includes(".pdf-x-info")) throw new Error("нет CSS карточки загруженного файла");
const uiVer = (jsSrc.match(/const UI_VERSION = "(\d+)"/) || [])[1];
assert.ok(uiVer, "app.js объявляет UI_VERSION");
assert.ok(htmlSrc.includes("app.js?v=" + uiVer),
  "index.html подключает app.js v" + uiVer + " (бамп UI_VERSION забыл в index.html?)");

// «Массовые действия → Обновить аналитику на диск»: чекбокс + прокидка дат.
assert.ok(htmlSrc.includes('id="batchAnalytics"'),
  "в меню массовых действий есть чекбокс batchAnalytics");
assert.ok(jsSrc.includes('"analytics"') && jsSrc.includes('"batchAnalytics"'),
  "batchActions() знает про действие analytics");
assert.ok(/for \(const id of \["batchRefresh", "batchRecalc", "batchApply", "batchMinpromo", "batchAnalytics", "batchDetailCheck"\]\)/.test(jsSrc),
  "initBatchMenu подписывает batchAnalytics/batchDetailCheck на updateBatchHint");
assert.ok(jsSrc.includes("date_from: f.date_from, date_to: f.date_to"),
  "startBatch передаёт период из шапки (date_from/date_to)");

// «Массовые действия → Проверить пропущенные детализации».
assert.ok(htmlSrc.includes('id="batchDetailCheck"'),
  "в меню массовых действий есть чекбокс batchDetailCheck");
assert.ok(jsSrc.includes("detail_check: \"batchDetailCheck\""),
  "batchActions() знает про действие detail_check");
assert.ok(jsSrc.includes('s.action === "detail_check"') && jsSrc.includes("s.details"),
  "stepHtml показывает периоды пропущенных детализаций");
assert.ok(apiSrc.includes("date_from=payload.get(\"date_from\")"),
  "api /batch/run прокидывает date_from в сервис");
assert.ok(jsSrc.includes("Файлы аналитики будут перезаписаны"),
  "подсказка про перезапись файлов аналитики");

// Массовые действия: полный план виден сразу + прогресс-полоса.
assert.ok(/CAB_STATUS = \{ pending:/.test(jsSrc),
  "CAB_STATUS знает статус pending («ожидание»)");
assert.ok(jsSrc.includes("refreshBar") && jsSrc.includes("refreshBarFill"),
  "renderRefreshBar обновляет полосу прогресса");
assert.ok(jsSrc.includes("renderRefreshBar(done, total)"),
  "renderRefresh считает общий прогресс и рисует полосу");
assert.ok(/refresh-step\s*' \+ s\.status/.test(jsSrc) ||
  jsSrc.includes("refresh-step ' + s.status"),
  "шаги рисуются с классом своего статуса (pending/running/ok/failed)");
assert.ok(htmlSrc.includes('id="refreshBar"'),
  "в модалке есть полоса прогресса refreshBar");

// Автопилот: плашка «последнее применение» + суффикс в зелёной строке.
assert.ok(htmlSrc.includes('id="pricingLastApply"'),
  "в тулбаре автопилота есть плашка pricingLastApply");
assert.ok(jsSrc.includes("refreshPricingLastApply") &&
  jsSrc.includes('"/pricing/last-apply?mp="'),
  "app.js грузит /pricing/last-apply по активному маркетплейсу");
assert.ok(jsSrc.includes("fmtLastApplyShort") && jsSrc.includes("fmtLastApplyFull"),
  "есть короткий и полный форматы последнего применения");
assert.ok(/refreshPricingLastApply\("wb"\)/.test(jsSrc) &&
  /refreshPricingLastApply\("ozon"\)/.test(jsSrc),
  "renderPricing и renderPricingOzon обновляют плашку своего МП");
assert.ok(jsSrc.includes('summary += " · " + laSuffix'),
  "суффикс последнего применения добавляется в зелёную строку");

// Обзор: сравнение за периоды на графиках «Динамика» и «Доход по МП».
assert.ok(htmlSrc.includes('id="btnDashCompare"') && htmlSrc.includes('id="dashComparePanel"'),
  "в фильтрах Обзора есть меню «Сравнить ▾» для периодов");
assert.ok(/"dashboard": \{ btn: "btnDashCompare", panel: "dashComparePanel" \}/.test(jsSrc),
  "CMP_TABS знает вкладку dashboard");
assert.ok(jsSrc.includes('const cmp = cmpQuery("dashboard")'),
  "dashQuery подмешивает cmpQuery(dashboard)");
assert.ok(jsSrc.includes("d.daily_prev") && jsSrc.includes('label: "Пред. период"'),
  "«Динамика за период» рисует прошлый период вторым рядом");
assert.ok(jsSrc.includes("per_marketplace_prev"),
  "«Доход по МП» использует per_marketplace_prev для парных столбцов");
assert.ok(jsSrc.includes("function dashPrevColor"),
  "есть приглушение цвета для прошлого периода");
assert.ok(jsSrc.includes("function paintDashCompareCaption"),
  "подпись окна сравнения выводится в заголовке «Динамика за период»");

// Множественные виды графика «Динамика за период»: столбцы / линия / кривая / область.
assert.ok(htmlSrc.includes('id="chartType"') && htmlSrc.includes('value="smooth"')
  && htmlSrc.includes('value="area"'),
  "виды графика: столбцы, линия, кривая, область");
assert.ok(jsSrc.includes("function dashRgba"),
  "есть dashRgba для полупрозрачной заливки области");
assert.ok(jsSrc.includes('chartType === "smooth"') && jsSrc.includes('chartType === "area"'),
  "paintDashDailyChart учитывает кривую и область");
// Значения по умолчанию для «Динамики за период»: Доход / По неделям / Область.
assert.ok(/<option value="income" selected/.test(htmlSrc),
  "по умолчанию выбран показатель «Доход»");
assert.ok(/<option value="week" selected/.test(htmlSrc),
  "по умолчанию группировка «По неделям»");
assert.ok(/<option value="area" selected/.test(htmlSrc),
  "по умолчанию вид графика «Область»");

// Итоговые суммы в подписи графика + день в метках оси X.
assert.ok(jsSrc.includes("function dashPeriodTotal"),
  "есть dashPeriodTotal для итогов по периоду");
assert.ok(jsSrc.includes("(было ") && jsSrc.includes('m.label + ": " + fmtMoney(cur)'),
  "подпись графика показывает итог текущего и предыдущего периодов");
assert.ok(jsSrc.includes('gran === "month"') && jsSrc.includes('Number(p[2]) + "." + p[1]'),
  "метки оси X содержат день (ДД.ММ) для дневной/недельной группировки");

// Секция «Итого» сворачивается как остальные разделы обзора.
assert.ok(htmlSrc.includes('data-csec="hero"') && htmlSrc.includes('class="dash-sec dash-hero"'),
  "секция Итого оформлена сворачиваемой секцией обзора");

// Быстрый выбор периода (пресеты) у пар дат «С … по …».
assert.ok(htmlSrc.includes('id="fPreset"') && htmlSrc.includes('id="dashPreset"'),
  "в шапке и дашборде есть пресеты периода");
assert.ok(htmlSrc.includes("Последние 2 недели") && htmlSrc.includes("Последний месяц")
  && htmlSrc.includes("Последний квартал") && htmlSrc.includes("Последний год"),
  "пресеты: 2 недели / месяц / квартал / год");
assert.ok(jsSrc.includes("function initDatePreset"),
  "есть initDatePreset для пар дат");
assert.ok(jsSrc.includes('initDatePreset("fPreset", "fFrom", "fTo"')
  && jsSrc.includes('initDatePreset("dashPreset", "dashFrom", "dashTo"'),
  "пресеты подключены к шапке и к дашборду");

// Список «Артикулы» на «Детализация Продаж WB»: текст + Excel, как меню PDF.
const { marginArtsList, setMarginArts, marginArtsParam } = sandbox;
setMarginArts([" tst-1 ", "TST-1", "", "2001"]);
assert.strictEqual(marginArtsParam(), "tst-1,2001",
  "список артикулов: trim + dedup без учёта регистра");
setMarginArts(["a", "b", "c"]);
assert.strictEqual(marginArtsList().length, 3, "список читается из localStorage");
setMarginArts([]);
assert.strictEqual(marginArtsParam(), "", "пустой список не добавляет параметр articles");
setMarginArts(Array.from({ length: 3100 }, (_, i) => "A" + i));
assert.strictEqual(marginArtsList().length, 3000, "список ограничен 3000 значениями");
setMarginArts([]);
assert.ok(jsSrc.includes("articles: marginArtsParam() || undefined"),
  "renderMarginDetail получает параметр articles из списка");
assert.ok(jsSrc.includes('"/api/margin/articles-import"'),
  "Excel со списком грузится на /api/margin/articles-import");
assert.ok(apiSrc.includes('@router.post("/margin/articles-import")'),
  "в app/api/ есть POST /margin/articles-import");
assert.ok(htmlSrc.includes('id="btnMarginDetailArts"') &&
  htmlSrc.includes('id="marginDetailArtsPanel"'),
  "в панели «Детализация Продаж WB» есть меню «Артикулы»");
assert.ok(jsSrc.includes("initMarginArtsMenu();"), "меню «Артикулы» инициализируется");
assert.ok(excelCss.includes(".margin-arts-text"), "есть CSS для поля списка артикулов");

// Всплывающие комментарии: текст можно выделить и скопировать — подсказка
// не исчезает, как только курсор сошёл с заголовка/иконки «?».
assert.ok(jsSrc.includes("function _hdrTipInside(el)"),
  "подсказка заголовка понимает, что курсор внутри неё");
assert.ok(jsSrc.includes("function _hdrTipHasSelection()"),
  "подсказка не прячется, пока её текст выделен");
assert.ok(jsSrc.includes("setTimeout(hideHeaderTip, 250)"),
  "у подсказки заголовка есть задержка скрытия");
assert.ok(jsSrc.includes('_hdrTipEl.addEventListener("mouseover", _cancelHdrTipHide)'),
  "курсор внутри подсказки отменяет скрытие");
assert.ok(jsSrc.includes("function _tipNodeFor(el)") &&
  jsSrc.includes('t.dataset.tipFromTitle = "1"'),
  "подсказки кнопок/селекторов берутся из title и показываются как у заголовков");
assert.ok(jsSrc.includes("function _restoreTipTitle(n)") &&
  jsSrc.includes("_restoreTipTitle(_hdrTipNode)"),
  "title возвращается на место после скрытия подсказки");
assert.ok(/#hdrTip\s*\{[^}]*pointer-events:\s*auto/.test(excelCss),
  "#hdrTip принимает курсор (pointer-events: auto)");
assert.ok(/\.tip \.tip-text\s*\{[^}]*user-select:\s*text/.test(excelCss),
  "текст подсказок «?» можно выделить");
assert.ok(excelCss.includes("transition: opacity .12s ease, visibility 0s linear .3s"),
  "подсказки «?» прячутся с задержкой, а не мгновенно");

// Расширенный поиск «*»: клиентский likeMatch должен совпадать с серверным.
const LIKE_CASES = [
  // базовый пример пользователя
  ["артикул-1-12-blue", "тику*12", true],
  ["новыйАртикул12-1", "тику*12", true],
  // порядок фрагментов значим
  ["ABC12XYZ", "abc*12*xyz", true],
  ["xyz12abc", "abc*12*xyz", false],
  // звёздочка может закрывать несколько фрагментов и пустое место
  ["01597-300-5-GREEN", "01597*GREEN", true],
  ["GREEN-01597", "01597*GREEN", false],
  ["abc", "a*c", true],
  ["ac", "a*c", true],
  ["abc", "*", true],
  // регистр не важен
  ["TIE-CORE-BLACK", "tie*black", true],
  ["tie-core-black", "TIE*BLACK", true],
  // «%» и «_» — литералы, а не спецсимволы (как на сервере)
  ["100% хлопок", "100%", true],
  ["1000 символов", "100%", false],
  ["002юбка_шорты", "юбка_шорты", true],
  ["002юбкаXшорты", "юбка_шорты", false],
  // regex-метасимволы в пользовательском вводе — литералы
  ["a.b", "a.b", true],
  ["aXb", "a.b", false],
  ["a(b)c", "a(b)c", true],
  ["цена $5 (USD)", "цена $5 (usd)", true],
  // пустой запрос = всё подходит
  ["что угодно", "", true],
  ["что угодно", "   ", true],
];

let likeFail = 0;
for (const [hay, query, want] of LIKE_CASES) {
  const got = likeMatch(hay, query);
  if (got !== want) {
    likeFail++;
    console.error(`  likeMatch FAIL: ${JSON.stringify(hay)} ~ ${JSON.stringify(query)} => ${got}, ожидалось ${want}`);
  }
}
// null/undefined не должны падать
if (likeMatch(null, "abc") !== false) { likeFail++; console.error("  likeMatch FAIL: null haystack"); }
if (likeMatch("abc", null) !== true) { likeFail++; console.error("  likeMatch FAIL: null query"); }
// кэш не должен ломать результат при >200 разных запросах.
// haystack подбирается под запрос (иначе «арт*0» = false по смыслу: в
// «артикул-12» нет «0»), плюс отдельная негативная проверка после очистки.
for (let i = 0; i < 250; i++) {
  if (!likeMatch("артикул-" + i, "арт*" + i)) {
    likeFail++;
    console.error(`  likeMatch FAIL: cache (i=${i})`);
    break;
  }
}
if (likeMatch("артикул-12", "арт*0")) {
  likeFail++;
  console.error("  likeMatch FAIL: negative after cache clear");
}
if (likeFail) throw new Error(`${likeFail} ошибок likeMatch`);

// серверные плейсхолдеры подсказывают про «*»
const SEARCH_INPUTS = [
  "salesSearch",
  "marginFunnelLike", "marginOzonDetailLike", "marginDetailLike",
  "pricingLike", "replenishLike", "productsLike",
  "wbCardsLike", "wbStockLike", "wbFunnelLike", "wbSalesLike", "wbDetailLike",
  "wbPricesLike", "wbStorageLike",
  "ozCardsLike", "ozStockLike", "ozPricesLike", "ozRealLike", "ozDetailLike",
];
for (const id of SEARCH_INPUTS) {
  const re = new RegExp(`id="${id}"[^>]*placeholder="([^"]*)"`);
  const m = htmlSrc.match(re);
  if (!m) { console.error(`  placeholder FAIL: нет поля ${id}`); continue; }
  if (!m[1].includes("* = любая часть")) {
    console.error(`  placeholder FAIL: ${id} => ${JSON.stringify(m[1])}`);
  }
}

// клиентских substring-фильтров по артикулу больше не осталось
const STRAY = [
  /\.toLowerCase\(\)\.includes\(/,
  /\.toLowerCase\(\)\s*$/,
];
for (const re of STRAY) {
  const hits = jsSrc.split("\n").filter((l) => re.test(l) && /article|name/.test(l) && /includes/.test(l));
  if (hits.length) console.error(`  stray substring filter: ${hits.join(" | ")}`);
}

// серверных f"%{...}%" шаблонов для article_like/like больше не осталось
const sqlSrc = apiSrc;
const straySql = sqlSrc.split("\n").filter((l) => /ilike\(f?"%\{/.test(l));
if (straySql.length) console.error(`  stray ilike f-string: ${straySql.join(" | ")}`);

console.log("LIKE_TESTS_OK");

// ─── «Вид таблицы»: поле фильтра по колонкам ────────────────────────────
// Фильтр прячет строки и целые группы, не перерисовывая панель (иначе при
// наборе теряется фокус). Семантика «*» — та же, что в общем поиске.
const { setColViewFilter } = sandbox;
let colviewFail = 0;
const colfail = (msg) => { colviewFail++; console.error("  colview FAIL: " + msg); };
const PANELS = {};
doc.querySelector = (sel) =>
  (typeof sel === "string" && sel.endsWith("ViewPanel") ? PANELS[sel] || el(sel) : el(sel));

const mkRow = (lbl) => ({ dataset: { lbl }, style: { display: "" } });
const vis = (r) => r.style.display !== "none";
const mkGrp = (title) => {
  const g = { dataset: { lbl: title }, style: { display: "" }, rows: [] };
  g.querySelectorAll = (s) => (s === ".colview-row" ? g.rows : []);
  return g;
};
const mkPanel = (rows, grps, hrs) => {
  const p = { rows, grps, hrs, empty: { hidden: true } };
  p.querySelectorAll = (s) =>
    s === ":scope > .colview-row" ? rows :
    s === ".colview-grp" ? grps :
    s === "hr" ? hrs : [];
  p.querySelector = (s) => (s === ".colview-filter-empty" ? p.empty : null);
  return p;
};

const rows = [mkRow("Прибыльность"), mkRow("Воронка Продаж WB"), mkRow("Скидка")];
const g1 = mkGrp("Маркетинг"); g1.rows = [mkRow("ACOS"), mkRow("ДРР")];
const g2 = mkGrp("Логистика"); g2.rows = [mkRow("Хранение"), mkRow("Комиссия")];
const hrs = [{ style: { display: "" } }, { style: { display: "" } }];
PANELS["#marginFunnelViewPanel"] = mkPanel(rows, [g1, g2], hrs);

const cf = (q) => setColViewFilter("margin-funnel", q);

// пустой фильтр — всё видно, разделители на месте, «ничего не найдено» скрыто
cf("");
if (!rows.every(vis)) colfail("пустой фильтр: скрыты строки");
if (!g1.rows.every(vis) || !g2.rows.every(vis)) colfail("пустой фильтр: скрыты группы");
if (!hrs.every((h) => h.style.display === "")) colfail("пустой фильтр: спрятаны разделители");
if (PANELS["#marginFunnelViewPanel"].empty.hidden !== true) colfail("пустой фильтр: видна плашка «Ничего не найдено»");

// substring + «*», регистр не важен
cf("ворон*wb");
if (vis(rows[0]) || !vis(rows[1]) || vis(rows[2])) colfail("ворон*wb: " + JSON.stringify(rows.map((r) => [r.dataset.lbl, r.style.display])));

// совпадение заголовка группы открывает все её строки, соседняя — уходит целиком
cf("ЛОГИСТИКА");
if (!g2.rows.every(vis)) colfail("ЛОГИСТИКА: группа не раскрыта целиком");
if (vis(g1.rows[0]) || vis(g1.rows[1])) colfail("ЛОГИСТИКА: чужая группа видна");
if (g1.style.display !== "none") colfail("ЛОГИСТИКА: чужая группа не скрыта");
if (g2.style.display !== "") colfail("ЛОГИСТИКА: своя группа скрыта");

// частичное совпадение внутри группы: группа остаётся, строки фильтруются
cf("acos");
if (g1.style.display !== "") colfail("acos: группа с совпадением скрыта");
if (!vis(g1.rows[0]) || vis(g1.rows[1])) colfail("acos: строки группы отфильтрованы неверно");
if (g2.style.display !== "none") colfail("acos: группа без совпадений осталась");

// ничего не найдено → все строки скрыты, плашка показана, разделители спрятаны
cf("zzz*");
if (rows.some(vis) || g1.rows.some(vis) || g2.rows.some(vis)) colfail("zzz*: что-то осталось видимым");
if (PANELS["#marginFunnelViewPanel"].empty.hidden !== false) colfail("zzz*: не показана плашка «Ничего не найдено»");
if (!hrs.every((h) => h.style.display === "none")) colfail("zzz*: разделители остались");

// возврат к пустому фильтру восстанавливает всё
cf("");
if (!rows.every(vis) || !g1.rows.every(vis)) colfail("сброс: не всё восстановилось");
if (!hrs.every((h) => h.style.display === "")) colfail("сброс: разделители не вернулись");
if (PANELS["#marginFunnelViewPanel"].empty.hidden !== true) colfail("сброс: плашка осталась");

// фильтр живёт по вкладке: запись для соседнего таба не трогает панель
PANELS["#wbCardsViewPanel"] = mkPanel([mkRow("Прибыльность")], [], [{ style: { display: "" } }]);
cf("ворон*wb");
setColViewFilter("wb-cards", "zzz");
if (vis(rows[0]) || !vis(rows[1])) colfail("изоляция по вкладкам: соседний таб перетёр фильтр");
cf("");

// статика: поле реально собирается в buildColViewMenu и подключено к сеттеру
const CSS_PATH = process.argv[5];const cssSrc = fs.readFileSync(CSS_PATH, "utf8");
const needJs = [
  ['className = "colview-filter"', "в панель не добавляется поле фильтра"],
  ["setColViewFilter(tab, e.target.value)", "input не вызывает setColViewFilter"],
  ['placeholder = "Фильтр колонок… * = любая часть"', "нет подсказки про «*» в поле фильтра"],
  ["row.dataset.lbl = label", "у строки нет data-lbl"],
  ["grp.dataset.lbl = g.title", "у группы нет data-lbl"],
  ["row.dataset.lbl = o.label", "у подстроки группы нет data-lbl"],
];
for (const [needle, msg] of needJs) if (!jsSrc.includes(needle)) colfail(msg);
if ((jsSrc.match(/applyColViewFilter\(tab\)/g) || []).length < 2) {
  colfail("фильтр не применяется после перестройки панели");
}
if (!/\.colview-filter\s*\{/.test(cssSrc)) {
  colfail("нет CSS-правила для .colview-filter");
}
if (colviewFail) throw new Error(`${colviewFail} ошибок фильтра «Вид таблицы»`);

console.log("COLVIEW_FILTER_OK");

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
    api_dir = Path(__file__).resolve().parents[2] / "app" / "api"
    assert api_dir.is_dir(), str(api_dir)
    # Один временный файл из всех модулей пакета: границы-сентинелы, чтобы
    # тело роута не сливалось с шапкой соседнего модуля (как в api_source()).
    api_path = tmp_path / "api_all.py"
    api_path.write_text(
        "\n__API_FILE_BOUNDARY__ = None\n".join(
            p.read_text(encoding="utf-8") for p in sorted(api_dir.glob("*.py"))),
        encoding="utf-8")
    css_path = Path(__file__).resolve().parents[2] / "app" / "static" / "style.css"
    assert css_path.exists(), str(css_path)
    columns_path = Path(__file__).resolve().parents[2] / "app" / "static" / "columns.json"
    assert columns_path.exists(), str(columns_path)
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    res = subprocess.run([node_bin, str(harness), str(APP_JS), str(html_path),
                          str(api_path), str(css_path), str(columns_path)],
                         capture_output=True, text=True)
    assert res.returncode == 0, f"{res.stdout}\n{res.stderr}"
    assert "JS_TESTS_OK" in res.stdout
    assert "COLVIEW_FILTER_OK" in res.stdout