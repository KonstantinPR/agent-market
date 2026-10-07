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

// «Дослать» — колонка плана подсортировки (как PDF) в обоих видах и в Excel.
assert.ok(jsSrc.includes('{ k: "to_sort", label: "Дослать"'),
  "в «По артикулам» нет колонки «Дослать»");
assert.ok((jsSrc.match(/\{ k: "to_sort", label: "Дослать"/g) || []).length === 2,
  "«Дослать» должна быть и в «По артикулам», и в «По размерам»");
assert.ok(jsSrc.includes('to_sort: _footSum(rows, "to_sort")'),
  "в итогах Потребности нет суммы «Дослать»");
assert.ok(apiSrc.includes('"to_sort": "Дослать на WB, шт"'),
  "в Excel-выгрузке нет колонки «Дослать на WB, шт»");
assert.strictEqual(
  (apiSrc.match(/"to_sort": "Дослать на WB, шт"/g) || []).length, 2,
  "«Дослать» должна выгружаться и в «По артикулам», и в «По размерам»");

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
  "в app/api.py есть POST /replenish/import-excel");
assert.ok(apiSrc.includes('@router.post("/export/replenish/pdf")'),
  "в app/api.py есть POST /export/replenish/pdf для строк файла");
assert.ok(apiSrc.includes("apply_sort_budget"),
  "бюджет «Итого дослать» применяется в PDF-сборке");
const excelCss = fs.readFileSync(process.argv[5], "utf8"); // cssSrc объявится ниже
if (!/\.pdf-drop\s*\{/.test(excelCss)) throw new Error("нет CSS-правил для .pdf-drop");
if (!excelCss.includes(".pdf-x-info")) throw new Error("нет CSS карточки загруженного файла");
assert.ok(htmlSrc.includes("app.js?v=73"), "index.html подключает app.js v73");

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
  "pricingLike", "whStockLike", "replenishLike", "productsLike",
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
    api_path = Path(__file__).resolve().parents[2] / "app" / "api.py"
    assert api_path.exists(), str(api_path)
    css_path = Path(__file__).resolve().parents[2] / "app" / "static" / "style.css"
    assert css_path.exists(), str(css_path)
    harness = tmp_path / "harness.js"
    harness.write_text(HARNESS, encoding="utf-8")
    res = subprocess.run([node_bin, str(harness), str(APP_JS), str(html_path),
                          str(api_path), str(css_path)],
                         capture_output=True, text=True)
    assert res.returncode == 0, f"{res.stdout}\n{res.stderr}"
    assert "JS_TESTS_OK" in res.stdout
    assert "COLVIEW_FILTER_OK" in res.stdout