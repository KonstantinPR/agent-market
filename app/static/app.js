"use strict";

const $ = (s) => document.querySelector(s);
const MP_LABELS = { wb: "Wildberries", ozon: "Ozon" };
let currentTab = "dashboard";
const charts = {};

function fmt(n) {
  return new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 0 }).format(n || 0);
}
function fmtMoney(n) {
  return fmt(Math.round(n || 0)) + " \u20BD";
}
function fmtFloat(n, d) {
  return new Intl.NumberFormat("ru-RU", { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 }).format(n || 0);
}
function fmtPct(n) {
  return (n == null || n === "" || isNaN(Number(n))) ? "—" : Number(n).toFixed(1) + "%";
}
function cls(n) { return Number(n) < 0 ? "neg" : "pos"; }

function writeDbStorage(api, kind) {
  const v = localStorage.getItem("write_db_" + api + "_" + kind);
  return v == null ? true : v === "1";
}
function setWriteDbStorage(api, kind, on) {
  localStorage.setItem("write_db_" + api + "_" + kind, on ? "1" : "0");
}
function initWriteDb() {
  document.querySelectorAll(".write-db").forEach((cb) => {
    cb.checked = writeDbStorage(cb.dataset.api, cb.dataset.kind);
    cb.addEventListener("change", () =>
      setWriteDbStorage(cb.dataset.api, cb.dataset.kind, cb.checked));
  });
}

function filters() {
  return {
    marketplace: $("#fMarketplace").value,
    date_from: $("#fFrom").value,
    date_to: $("#fTo").value,
  };
}

function marginMarketplace() {
  const sel = [];
  if ($("#mWb").checked) sel.push("wb");
  if ($("#mOzon").checked) sel.push("ozon");
  const g = $("#fMarketplace").value;
  const base = g ? [g] : sel;
  return base.filter((x) => sel.includes(x)).join(",");
}

function qs(params) {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") usp.set(k, v);
  }
  const s = usp.toString();
  return s ? "?" + s : "";
}

async function api(path) {
  const resp = await fetch("/api" + path);
  if (!resp.ok) throw new Error(resp.status + " " + (await resp.text()));
  return resp.json();
}

async function apiPost(path, body) {
  const resp = await fetch("/api" + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!resp.ok) {
    let detail = "";
    try {
      const j = await resp.json();
      detail = j.detail || "";
    } catch (e) { /* не Json */ }
    throw new Error(resp.status + " " + detail);
  }
  return resp.json();
}

function table(headers, rows) {
  if (!rows.length) return '<div class="empty">Нет данных за выбранный период</div>';
  let h = "<thead><tr>";
  for (const c of headers) h += '<th class="' + (c.num ? "num" : "") + '">' + c.label + "</th>";
  h += "</tr></thead><tbody>";
  for (const r of rows) {
    h += "<tr>";
    for (const c of headers) h += '<td class="' + (c.num ? "num" : "") + '">' + c.render(r[c.k], r) + "</td>";
    h += "</tr>";
  }
  return "<table>" + h + "</tbody></table>";
}

const cellFmts = {
  money: (v) => v == null ? "—" : fmtMoney(v),
  moneyCls: (v) => v == null ? "—" : `<span class="${cls(v)}">${fmtMoney(v)}</span>`,
  pct: (v) => v == null ? "—" : `<span class="${cls(v)}">${fmtPct(v)}</span>`,
  int: (v) => v == null ? "—" : fmt(v),
  text: (v) => (v == null || v === "") ? "—" : v,
  tag: (v) => `<span class="tag ${v}">${MP_LABELS[v] || v}</span>`,
};

function pagedTable(container, headers, rows) {
  if (!container._pt) container._pt = { limit: 100, showAll: false };
  const st = container._pt;
  st.headers = headers;
  st.rows = rows;
  const paint = () => {
    container.innerHTML = "";
    if (!rows || !rows.length) {
      container.innerHTML = '<div class="empty">Нет данных за выбранный период</div>';
      return;
    }
    const bar = document.createElement("div");
    bar.className = "pager";
    const span = document.createElement("span");
    span.className = "pager-lbl";
    bar.appendChild(span);
    if (!st.showAll) {
      const lbl = document.createElement("label");
      lbl.className = "pager-n";
      lbl.appendChild(document.createTextNode("Показывать "));
      const input = document.createElement("input");
      input.type = "number";
      input.min = "1";
      input.value = String(st.limit);
      lbl.appendChild(input);
      bar.appendChild(lbl);
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn pager-all";
      btn.textContent = "Загрузить все (" + fmt(Math.max(0, rows.length - st.limit)) + ")";
      bar.appendChild(btn);
      input.addEventListener("change", () => {
        let v = parseInt(input.value, 10);
        if (!v || v < 1) v = 1;
        st.limit = Math.min(v, rows.length);
        paint();
      });
      btn.addEventListener("click", () => {
        st.showAll = true;
        paint();
      });
    }
    const visible = st.showAll ? rows : rows.slice(0, st.limit);
    span.textContent = "Показано " + fmt(visible.length) + " из " + fmt(rows.length);
    container.appendChild(bar);
    const wrap = document.createElement("div");
    wrap.innerHTML = table(headers, visible);
    container.appendChild(wrap);
  };
  paint();
}

function tabLike(id) {
  return $("#" + id) ? $("#" + id).value.trim() : "";
}

async function loadTab(name) {
  const f = filters();
  try {
    if (name === "dashboard") await renderDashboard(qs(f));
    else if (name === "margin") {
      await renderMargin(qs({ marketplace: marginMarketplace(), date_from: f.date_from, date_to: f.date_to, article_like: tabLike("marginLike") || undefined }));
    } else if (name === "margin-funnel") {
      await renderMarginFunnel(qs({ date_from: f.date_from, date_to: f.date_to, article_like: tabLike("marginFunnelLike") || undefined }));
    } else if (name === "margin-detail") {
      await renderMarginDetail(qs({ date_from: f.date_from, date_to: f.date_to, article_like: tabLike("marginDetailLike") || undefined }));
    } else if (name === "sales") await renderSales(qs(f));
    else if (name === "stocks") await renderStocks(f.marketplace);
    else if (name === "ours") await renderOurs();
    else if (name === "products") await renderProducts();
    else if (name === "pricing") await renderPricing(false);
    else if (name === "wb-cards" || name === "oz-cards") await renderCards(name);
  } catch (err) {
    console.error("loadTab error:", err);
  }
}

function setChart(id, cfg) {
  if (charts[id]) { charts[id].destroy(); }
  charts[id] = new Chart($("#" + id), cfg);
}

async function renderDashboard(p) {
  const d = await api("/dashboard" + p);
  let html = "";
  const total = { mp: "Итого", ...d.total };
  const cards = [total, ...d.per_marketplace];
  for (const c of cards) {
    html += `
      <div class="kpi ${c.marketplace || "total"}">
        <div class="title">${c.marketplace ? MP_LABELS[c.marketplace] : "Итого"}</div>
        <div class="lines">
          <div class="line"><span>Выручка</span><span class="kvalue">${fmtMoney(c.revenue)}</span></div>
          <div class="line"><span>К перечислению</span><span class="kvalue">${fmtMoney(c.income)}</span></div>
          <div class="line"><span>Продано, шт</span><span class="kvalue">${fmt(c.sells)}</span></div>
        </div>
      </div>`;
  }
  $("#kpi").innerHTML = html;

  const labels = d.per_marketplace.map((m) => MP_LABELS[m.marketplace] || m.marketplace);
  const values = d.per_marketplace.map((m) => m.income);
  setChart("chartDoughnut", {
    type: "doughnut",
    data: {
      labels,
      datasets: [{ data: values, backgroundColor: ["#3b6cff", "#6f4bff"], borderWidth: 0 }],
    },
    options: { plugins: { legend: { position: "bottom" } } },
  });

  setChart("chartDaily", {
    type: "bar",
    data: {
      labels: d.daily.map((x) => x.date.slice(5)),
      datasets: [
        { label: "Выручка", data: d.daily.map((x) => x.revenue), backgroundColor: "#3b6cff" },
        { label: "Доход", data: d.daily.map((x) => x.income), backgroundColor: "#1fae64" },
      ],
    },
    options: {
      plugins: { legend: { position: "bottom" } },
      scales: { x: { ticks: { maxTicksLimit: 12 } } },
    },
  });
}

async function renderMargin(p) {
  const data = await api("/margin" + p);
  const headers = [
    { k: "article", label: "Артикул", render: cellFmts.text },
    { k: "name", label: "Наименование", render: cellFmts.text },
    { k: "sells", label: "Продано, шт", num: true, render: cellFmts.int },
    { k: "revenue", label: "Выручка", num: true, render: cellFmts.money },
    { k: "commission", label: "Комиссия", num: true, render: cellFmts.money },
    { k: "logistics", label: "Логистика", num: true, render: cellFmts.money },
    { k: "income", label: "К перечислению", num: true, render: cellFmts.money },
    { k: "net_cost", label: "Себестоимость", num: true, render: cellFmts.money },
    { k: "margin", label: "Маржа", num: true, render: cellFmts.moneyCls },
    { k: "margin_per_one", label: "Маржа на ед.", num: true, render: cellFmts.moneyCls },
    { k: "margin_pct", label: "Маржа, %", num: true, render: cellFmts.pct },
  ];
  const draw = (rows) => { pagedTable($("#marginTable"), headers, rows); };
  draw(data.rows);
  $("#marginSearch").oninput = (e) => {
    const q = e.target.value.trim().toLowerCase();
    draw(data.rows.filter((r) => (r.article + " " + (r.name || "")).toLowerCase().includes(q)));
  };
  $("#exportMargin").href = "/api/export/margin" + p;
}

const marginHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text },
  { k: "name", label: "Наименование", render: cellFmts.text },
  { k: "sells", label: "Продано, шт", num: true, render: cellFmts.int },
  { k: "revenue", label: "Выручка", num: true, render: cellFmts.money },
  { k: "commission", label: "Комиссия", num: true, render: cellFmts.money },
  { k: "logistics", label: "Логистика", num: true, render: cellFmts.money },
  { k: "storage", label: "Хранение", num: true, render: cellFmts.money },
  { k: "services", label: "Услуги", num: true, render: cellFmts.money },
  { k: "income", label: "К перечислению", num: true, render: cellFmts.money },
  { k: "other", label: "Прочее", num: true, render: cellFmts.moneyCls },
  { k: "net_cost", label: "Себестоимость", num: true, render: cellFmts.money },
  { k: "margin", label: "Маржа", num: true, render: cellFmts.moneyCls },
  { k: "margin_per_one", label: "Маржа на ед.", num: true, render: cellFmts.moneyCls },
  { k: "margin_pct", label: "Маржа, %", num: true, render: cellFmts.pct },
];

const funnelHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text },
  { k: "name", label: "Наименование", render: cellFmts.text },
  { k: "views", label: "Просмотры", num: true, render: cellFmts.int },
  { k: "opens", label: "Открытия", num: true, render: cellFmts.int },
  { k: "adds", label: "В корзину", num: true, render: cellFmts.int },
  { k: "orders", label: "Заказы", num: true, render: cellFmts.int },
  { k: "cancelled", label: "Отмены", num: true, render: cellFmts.int },
  { k: "cart_pct", label: "В корзину, %", num: true, render: cellFmts.pct },
  { k: "order_pct", label: "Заказы, %", num: true, render: cellFmts.pct },
  { k: "avg_price", label: "Ср. цена", num: true, render: cellFmts.money },
  { k: "revenue", label: "Выручка (оценка)", num: true, render: cellFmts.money },
  { k: "net_cost", label: "Себестоимость", num: true, render: cellFmts.money },
  { k: "margin", label: "Маржа (оц.)", num: true, render: cellFmts.moneyCls },
  { k: "margin_pct", label: "Маржа, %", num: true, render: cellFmts.pct },
];

async function renderMarginFunnel(p) {
  const data = await api("/margin/funnel" + p);
  pagedTable($("#marginFunnelTable"), funnelHeaders, data.rows || []);
  $("#exportMarginFunnel").href = "/api/export/margin/funnel" + p;
  const msg = $("#marginFunnelMsg");
  if (data.snapshot_from && data.snapshot_to) {
    msg.textContent = "Оценка прибыльности по срезу воронки за " + data.snapshot_from + " … " + data.snapshot_to +
      " (до комиссий WB). Обновите: WB API ▸ Воронка продаж.";
  } else {
    msg.textContent = "Нет данных. Сначала скачайте WB API ▸ Воронка продаж.";
  }
}

async function renderMarginDetail(p) {
  const data = await api("/margin/detail" + p);
  pagedTable($("#marginDetailTable"), marginHeaders, data.rows || []);
  $("#exportMarginDetail").href = "/api/export/margin/detail" + p;
  const msg = $("#marginDetailMsg");
  if ((data.rows || []).length === 0) {
    msg.textContent =
      "Нет данных. Финансовый отчёт WB скачивается отдельным ключом (finance): WB API ▸ Детализация продаж. " +
      "Запрос редкий (1 в ~12 ч), отчёт формируется на вчерашний день.";
  } else {
    msg.textContent =
      "Данные из финансового отчёта WB (детализация продаж). Маржа = \"К перечислению\" − себестоимость. " +
      "\"Прочее\" — разница (возвраты/корректировки).";
  }
}

async function renderSales(p) {
  const data = await api("/sales" + p);
  const headers = [
    { k: "date", label: "Дата", render: cellFmts.text },
    { k: "marketplace", label: "Маркетплейс", render: cellFmts.tag },
    { k: "article", label: "Артикул", render: cellFmts.text },
    { k: "name", label: "Наименование", render: cellFmts.text },
    { k: "quantity", label: "Продано, шт", num: true, render: cellFmts.int },
    { k: "revenue", label: "Выручка", num: true, render: cellFmts.money },
    { k: "income", label: "К перечислению", num: true, render: cellFmts.money },
  ];
  const draw = (rows) => { pagedTable($("#salesTable"), headers, rows); };
  draw(data.rows);
  $("#salesSearch").oninput = (e) => {
    const q = e.target.value.trim().toLowerCase();
    draw(data.rows.filter((r) => (r.article + " " + (r.name || "")).toLowerCase().includes(q)));
  };
  $("#exportSales").href = "/api/export/sales" + p;
}

async function renderStocks(marketplace) {
  const data = await api("/stocks" + qs({ marketplace }));
  const headers = [
    { k: "date", label: "Дата", render: cellFmts.text },
    { k: "marketplace", label: "Маркетплейс", render: cellFmts.tag },
    { k: "article", label: "Артикул", render: cellFmts.text },
    { k: "name", label: "Наименование", render: cellFmts.text },
    { k: "warehouse", label: "Склад", render: cellFmts.text },
    { k: "quantity", label: "Кол-во", num: true, render: cellFmts.int },
  ];
  pagedTable($("#stocksTable"), headers, data.rows || []);
}

async function renderOurs() {
  const data = await api("/custom-stock");
  const headers = [
    { k: "article", label: "Артикул", render: cellFmts.text },
    { k: "name", label: "Наименование", render: cellFmts.text },
    { k: "quantity", label: "Кол-во на складе", num: true, render: cellFmts.int },
    { k: "net_cost", label: "Себестоимость", num: true, render: cellFmts.money },
    { k: "updated_at", label: "Обновлено", render: cellFmts.text },
  ];
  pagedTable($("#oursTable"), headers, data.rows || []);
}

async function renderProducts() {
  const data = await api("/products");
  const headers = [
    { k: "article", label: "Артикул", render: cellFmts.text },
    { k: "name", label: "Наименование", render: cellFmts.text },
    { k: "brand", label: "Бренд", render: cellFmts.text },
    { k: "barcode", label: "Баркод", render: cellFmts.text },
    { k: "net_cost", label: "Себестоимость", num: true, render: cellFmts.money },
    { k: "replenishable", label: "Докупаемый", render: replenishableCell },
  ];
  pagedTable($("#productsTable"), headers, data.rows || []);
  initReplenishToggle();
}

function replenishableCell(v) {
  return '<input type="checkbox" class="repl-toggle" ' + (v ? "checked" : "") + ">";
}

function initReplenishToggle() {
  const box = $("#productsTable");
  if (!box || box._replBound) return;
  box._replBound = true;
  box.addEventListener("change", async (e) => {
    const t = e.target;
    if (!t.classList.contains("repl-toggle")) return;
    const row = t.closest("tr");
    const cell = row ? row.cells[0] : null;
    const article = cell ? cell.textContent.trim() : "";
    if (!article) return;
    try {
      await apiPost("/products/replenishable", { article, value: t.checked });
      $("#productsMsg").textContent =
        "Докупаемый: " + article + " → " + (t.checked ? "да" : "нет");
    } catch (err) {
      t.checked = !t.checked;
      $("#productsMsg").textContent = "Ошибка: " + err.message;
    }
  });
}

async function uploadFile(url, input, msgSel, tab) {
  const file = input.files[0];
  if (!file) { return; }
  const fd = new FormData();
  fd.append("file", file);
  try {
    const resp = await fetch("/api" + url, { method: "POST", body: fd });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || resp.status);
    let msg = "Импортировано строк: " + data.imported;
    if (data.total != null && Number(data.total) !== Number(data.imported)) {
      msg += " из " + data.total;
    }
    if (data.with_cost_total != null) {
      msg += "; с себестоимостью в базе: " + data.with_cost_total;
    }
    document.querySelector(msgSel).textContent = msg + " (" + file.name + ")";
    input.value = "";
    await loadTab(tab);
  } catch (err) {
    document.querySelector(msgSel).textContent = "Ошибка: " + err.message;
  }
}

function initDates() {
  const to = new Date();
  const from = new Date();
  from.setDate(to.getDate() - 30);
  $("#fFrom").value = from.toISOString().slice(0, 10);
  $("#fTo").value = to.toISOString().slice(0, 10);
}

function filenameFromDisposition(d) {
  const m = /filename\*?=(?:UTF-8''|")?([^";]+)/.exec(d || "");
  return m ? decodeURIComponent(m[1]) : "wb_download.xlsx";
}

async function apiDownload(api, kind, msgSel) {
  const pane = document.querySelector(".pane.active");
  const from = pane.querySelector('input[data-date="from"]');
  const to = pane.querySelector('input[data-date="to"]');
  const days = pane.querySelector("input[data-days]");
  const month = pane.querySelector("input[data-month]");
  const year = pane.querySelector("input[data-year]");
  const writeDb = pane.querySelector(".write-db");
  const params = {};
  if (from) params.date_from = from.value;
  if (to) params.date_to = to.value;
  if (days) params.days = days.value;
  if (month) params.month = month.value;
  if (year) params.year = year.value;
  if (writeDb) params.write_db = writeDb.checked ? 1 : 0;
  const msg = document.querySelector(msgSel);
  msg.textContent = "Загрузка…";
  try {
    const resp = await fetch("/api/" + api + "/" + kind + qs(params), { method: "POST" });
    if (!resp.ok) throw new Error(resp.status + " " + (await resp.text()));
    const blob = await resp.blob();
    const count = resp.headers.get("X-Count");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filenameFromDisposition(resp.headers.get("Content-Disposition"));
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);
    msg.textContent = "Готово, строк: " + count + (writeDb && !writeDb.checked ? " (без записи в базу)" : "");
    pullsCache = null;
    updateLastPull(currentTab);
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

let pullsCache = null;

async function updateLastPull(name) {
  if (!name.startsWith("wb-") && !name.startsWith("oz-")) return;
  if (!pullsCache) {
    try {
      pullsCache = await api("/pulls");
    } catch (err) {
      return;
    }
  }
  const pane = document.getElementById("tab-" + name);
  const form = pane ? pane.querySelector(".wb-form") : null;
  if (!form) return;
  let el = form.querySelector(".last-pull");
  if (!el) {
    el = document.createElement("div");
    el.className = "last-pull";
    form.insertBefore(el, form.firstChild);
  }
  const [apiName, kind] = name.split("-");
  let p = pullsCache.find((x) => x.api === apiName && x.kind === kind);
  if (kind === "cards") {
    const cand = pullsCache
      .filter((x) => x.api === apiName && (x.kind === "cards" || x.kind === "cards_excel"))
      .sort((a, b) => ((new Date(b.last_success_at.replace(" ", "T"))).getTime() || 0) - ((new Date(a.last_success_at.replace(" ", "T"))).getTime() || 0));
    p = cand[0] || null;
  }
  if (!p || !p.last_success_at) {
    el.textContent = "Ещё не загружалось (данные в базу появятся после первого успешного скачивания)";
    return;
  }
  const t = new Date(p.last_success_at.replace(" ", "T"));
  const parts = ["Последняя загрузка: " + t.toLocaleString("ru-RU"), "строк: " + fmt(p.rows)];
  if (p.db_rows > 0) {
    parts.push("в базу записано: " + fmt(p.db_rows));
  } else {
    parts.push("(в базу не пишет)");
  }
  if (p.window) parts.push("период: " + p.window);
  el.textContent = parts.join(" · ");
}

// ----------------------------------------------------- карточки маркетплейса (Excel)
const cardsHeaders = [
  { k: "chrt_id", label: "Код размера", render: cellFmts.text },
  { k: "vendor_code", label: "Артикул продавца", render: cellFmts.text },
  { k: "nm_id", label: "Артикул WB", render: cellFmts.text },
  { k: "brand", label: "Бренд", render: cellFmts.text },
  { k: "subject", label: "Предмет", render: cellFmts.text },
  { k: "size", label: "Размер", render: cellFmts.text },
  { k: "barcode", label: "Баркод", render: cellFmts.text },
  { k: "volume_l", label: "Объём, л.", num: true, render: (v) => v == null || v === 0 ? "—" : fmt(v) },
  { k: "composition", label: "Состав", render: (v) => cellFmts.text(String(v || "").slice(0, 60)) },
  { k: "name", label: "Название", render: cellFmts.text },
];

function cardIds(name) {
  const apiName = name.split("-")[0];
  const tag = apiName === "oz" ? "oz" : "wb";
  const mp = apiName === "oz" ? "ozon" : "wb";
  return {
    apiName, mp, tag,
    prefix: apiName + "Cards",
    msgExcel: "#" + tag + "Msg-cards-excel",
    msgTable: "#" + tag + "Msg-cards-table",
  };
}

async function renderCards(name) {
  const ids = cardIds(name);
  const box = document.getElementById(ids.prefix + "Table");
  const likeEl = document.getElementById(ids.prefix + "Like");
  const q = likeEl ? likeEl.value.trim() : "";
  let data;
  try {
    data = await api("/cards" + qs({ marketplace: ids.mp, like: q || undefined, limit: 500 }));
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить карточки: ' + escapeHtml(err.message) + "</div>";
    return;
  }
  const msg = document.querySelector(ids.msgTable);
  if (msg) msg.textContent = data.count ? "Карточек: " + fmt(data.count) : "Нет загруженных карточек";
  pagedTable(box, cardsHeaders, data.rows || []);
}

async function uploadCardFiles(files, name) {
  if (!files || !files.length) return;
  const ids = cardIds(name);
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  const msg = document.querySelector(ids.msgExcel);
  msg.textContent = "Загружаю " + files.length + " файл(ов)…";
  try {
    const resp = await fetch("/api/import/cards?marketplace=" + ids.mp, { method: "POST", body: fd });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || resp.status);
    let m = "Карточек: " + fmt(data.imported) + ", в каталог: " + fmt(data.products);
    if (data.total != null && data.total !== data.imported) m += " (строк: " + fmt(data.total) + ")";
    if (data.errors && data.errors.length) m += "; с ошибками: " + fmt(data.errors.length);
    msg.textContent = m;
    const input = document.getElementById(ids.prefix + "File");
    if (input) input.value = "";
    pullsCache = null;
    updateLastPull(name);
    await renderCards(name);
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

function initCardsUpload() {
  ["wb-cards", "oz-cards"].forEach((name) => {
    const ids = cardIds(name);
    const file = document.getElementById(ids.prefix + "File");
    const btn = document.getElementById(ids.prefix + "Import");
    const drop = document.getElementById(ids.prefix + "Drop");
    const likeEl = document.getElementById(ids.prefix + "Like");
    if (btn && file) btn.addEventListener("click", () => uploadCardFiles(file.files, name));
    if (likeEl) {
      let timer;
      likeEl.addEventListener("input", () => {
        clearTimeout(timer);
        timer = setTimeout(() => { if (currentTab === name) renderCards(name); }, 400);
      });
    }
    if (drop) {
      drop.addEventListener("click", () => { if (file) file.click(); });
      drop.addEventListener("dragover", (e) => {
        e.preventDefault();
        drop.classList.add("over");
      });
      drop.addEventListener("dragleave", () => drop.classList.remove("over"));
      drop.addEventListener("drop", (e) => {
        e.preventDefault();
        drop.classList.remove("over");
        uploadCardFiles(e.dataTransfer.files, name);
      });
    }
  });
}

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

const REFRESH_STATUS = {
  running: "выполняется",
  queued: "в очереди",
  done: "завершено",
};
const STEP_STATUS = {
  pending: "—", running: "выполняется…", ok: "ок", failed: "ошибка",
};

let refreshPoll = null;
let refreshJobId = null;
let refreshApi = null;

function stepText(s) {
  if (s.status === "ok") {
    return "ок · строк: " + fmt(s.rows) + (s.db_rows ? " · в базу: " + fmt(s.db_rows) : "");
  }
  return STEP_STATUS[s.status] || s.status;
}

function renderRefresh(st) {
  $("#refreshSummary").textContent =
    (REFRESH_STATUS[st.status] || st.status) +
    " · ок: " + st.ok + ", ошибок: " + st.failed +
    " · " + (st.finished_at ? "завершено " + st.finished_at : "начато " + st.started_at);
  let h = "";
  for (const s of st.steps) {
    h += '<div class="refresh-step ' + s.status + '">' +
      '<span class="rs-name">' + escapeHtml(s.label) + "</span>" +
      '<span class="rs-status">' + escapeHtml(stepText(s)) + "</span></div>";
    if (s.error) h += '<div class="refresh-step-error">' + escapeHtml(s.error) + "</div>";
  }
  $("#refreshList").innerHTML = h;
}

async function loadRefreshHistory() {
  try {
    const data = await api("/refresh/history?limit=5");
    if (!data.runs.length) {
      $("#refreshHistory").innerHTML = '<div class="empty">Запусков ещё не было</div>';
      return;
    }
    let h = "";
    for (const r of data.runs) {
      const failed = r.results.filter((x) => x.status === "failed");
      h += '<div class="refresh-run ' + r.status + '">' +
        "<span>" + escapeHtml(MP_LABELS[r.api] || r.api) + "</span>" +
        "<span>" + escapeHtml(r.finished_at || r.started_at || "") + "</span>" +
        "<span>" + escapeHtml(r.status) +
        (failed.length ? " (" + failed.length + " с ошибкой)" : "") + "</span></div>";
    }
    $("#refreshHistory").innerHTML = h;
  } catch (err) {
    $("#refreshHistory").innerHTML = '<div class="empty">Не удалось загрузить историю</div>';
  }
}

async function openRefresh(api) {
  const modal = $("#refreshModal");
  modal.classList.remove("hidden");
  refreshApi = api;
  $("#refreshTitle").textContent = "Обновление " + (MP_LABELS[api] || api);
  const detail = $("#refreshDetail");
  detail.checked = false;
  detail.disabled = false;
  detail.parentElement.style.display = api === "wb" ? "" : "none";
  $("#refreshStart").disabled = false;
  $("#refreshSummary").textContent = "";
  $("#refreshList").innerHTML = "";
  loadRefreshHistory();
}

async function startRefreshJob() {
  const api = refreshApi;
  const startBtn = $("#refreshStart");
  startBtn.disabled = true;
  $("#refreshSummary").textContent = "Запуск…";
  const detail = $("#refreshDetail");
  const params = { api };
  if (api === "wb" && detail.checked) params.detail = 1;
  if ($("#fFrom").value) params.date_from = $("#fFrom").value;
  if ($("#fTo").value) params.date_to = $("#fTo").value;
  try {
    const resp = await fetch("/api/refresh" + qs(params), { method: "POST" });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || resp.status);
    if (!data.job_id) {
      $("#refreshSummary").textContent = "Отклонено: " + (data.rejected || "задание уже в очереди");
      startBtn.disabled = false;
      return;
    }
    refreshJobId = data.job_id;
    detail.disabled = true;
    pollRefresh(data.job_id);
  } catch (err) {
    $("#refreshSummary").textContent = "Ошибка: " + err.message;
    startBtn.disabled = false;
  }
}

function pollRefresh(jobId) {
  if (refreshPoll) clearInterval(refreshPoll);
  refreshPoll = setInterval(async () => {
    let st;
    try {
      st = await api("/refresh/" + jobId);
    } catch (err) {
      $("#refreshSummary").textContent = "Ошибка опроса: " + err.message;
      clearInterval(refreshPoll);
      refreshPoll = null;
      return;
    }
    renderRefresh(st);
    if (st.status === "done") {
      clearInterval(refreshPoll);
      refreshPoll = null;
      refreshJobId = null;
      pullsCache = null;
      updateLastPull(currentTab);
      loadRefreshHistory();
    }
  }, 1500);
}

function openTab(name, linkEl) {
  document.querySelectorAll(".dropdown").forEach((d) => d.classList.remove("open"));
  document.querySelectorAll(".nav-link").forEach((l) => l.classList.remove("active"));
  if (linkEl) linkEl.classList.add("active");
  document.querySelectorAll(".pane").forEach((p) => p.classList.remove("active"));
  const pane = document.getElementById("tab-" + name);
  if (pane) pane.classList.add("active");
  currentTab = name;
  loadTab(currentTab);
  updateLastPull(name);
}

// ------------------------------------------------------------- автопилот цен WB
let pricingDefaults = null;

const PRICING_LABELS = {
  window_days: "Окно скорости, дн",
  target_doc: "Целевой DOC, дн",
  doc_low: "Дефицит ≤, дн",
  doc_high: "Перезапас ≥, дн",
  floor_margin_pct: "Минимальная маржа, %",
  max_discount_pct: "Макс. скидка, %",
  max_raise_pct: "Макс. рост цены, %",
  max_drop_pct: "Макс. снижение цены, %",
  min_delta_pp: "Мин. дельта, п.п.",
  cooldown_days: "Кулдаун, дн",
  season_adj: "Учёт тренда",
  season_damp: "Ослабление тренда",
  min_days_with_sales: "Мин. дней с продажами",
  hot_conv_pct: "Горячий спрос: конверсия ≥, %",
  hot_backlog_factor: "Горячий спрос: в корзине ≥ заказов×",
  return_penalty: "Порог возвратов/отмен",
  dead_stock_days: "Мёртвый запас ≥, дн",
  low_conv_pct: "Низкая конверсия <, %",
  fallback_window_days: "Окно unit-экономики, дн",
  raise_pct_replenishable: "Рост для докупаемых, %",
};

const PRICING_HINTS = {
  window_days: "Скользящее окно (дней), за которое считаются скорость продаж (v = продажи за окно / окно) и DOC.",
  target_doc: "Желаемый запас в днях продаж (DOC). Если DOC выше цели — товар перезапасён, цена снижается.",
  doc_low: "Граница дефицита: если DOC меньше этого значения, запас считается дефицитным (повод поднять цену).",
  doc_high: "Граница перезапаса: если DOC больше этого значения, товар считается перезапасённым (повод снизить цену).",
  floor_margin_pct: "Запас маржи над точкой безубыточности: пол цены = себестоимость+логистика+комиссия+хранение, увеличенные на эту маржу. Цену ниже пола автопилот не опускает.",
  max_discount_pct: "Потолок скидки для любого товара, % от базовой цены. Дальше скидка не снижается ни при каком решении.",
  max_raise_pct: "Максимальное повышение витринной цены за один шаг, % (росту разрешён только при реальных продажах).",
  max_drop_pct: "Максимальное снижение витринной цены за один шаг, %.",
  min_delta_pp: "Минимальное изменение скидки, п.п. Если расчётная дельта меньше — товар не трогаем (защита от «флапа» цен).",
  cooldown_days: "Кулдаун: сколько дней после применения товар не меняется повторно, даже если правила снова что-то рекомендуют.",
  season_adj: "Учитывать тренд продаж (рост/падение как сезонность). Выкл. → скорость продаж берётся как есть, без экстраполяции.",
  season_damp: "Насколько ослабляем экстраполяцию тренда: 0 = полностью игнорировать, 1 = переносить тренд без смягчения. 0.5 — компромисс.",
  min_days_with_sales: "Минимум дней с ненулевыми продажами в окне, чтобы вообще применять поправку тренда.",
  hot_conv_pct: "Конверсия (покупки/просмотры) выше этого %, % — признак «горячего» спроса: товару можно поднимать цену даже при дефиците.",
  hot_backlog_factor: "Признак горячего спроса: число покупателей «в корзине» больше числа заказов в это число раз.",
  return_penalty: "Если доля возвратов/отмен больше этой величины (0..1), продажи считаются «шумными» и товар не трогаем.",
  dead_stock_days: "Если продаж не было столько дней подряд → мёртвый запас, цену снижаем.",
  low_conv_pct: "Много «в корзине», но конверсия ниже этого %, % — интерес без покупок; товар пропускаем.",
  fallback_window_days: "Окно (дней), за которое берутся фактические продажи для расчёта unit-экономики: реальная цена, комиссия, маржа, возвраты.",
  raise_pct_replenishable: "Потолок повышения цены для докупаемых товаров, % — их поднимаем аккуратно, чтобы не потерять выкупы.",
};

const PRICING_ACTION = {
  RAISE: { txt: "поднять цену", cls: "p-raise" },
  LOWER: { txt: "снизить цену", cls: "p-lower" },
  HOLD: { txt: "держать", cls: "p-hold" },
  SKIP: { txt: "пропустить", cls: "p-skip" },
};

function actionCell(v) {
  const a = PRICING_ACTION[v];
  return a ? `<span class="tag p-tag ${a.cls}">${a.txt}</span>` : (v || "—");
}

const pricingHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text },
  { k: "name", label: "Наименование", render: cellFmts.text },
  { k: "stock", label: "Остаток", num: true, render: cellFmts.int },
  { k: "doc", label: "DOC, дн", num: true, render: (v) => v == null ? "—" : fmt(v) },
  { k: "velocity", label: "v, шт/дн", num: true, render: (v) => v == null ? "—" : v.toFixed(1) },
  { k: "trend", label: "Тренд", num: true, render: (v) => v == null ? "—" : (v && v > 1 ? "<span class='pos'>▲ " : v && v < 1 ? "<span class='neg'>▼ " : "<span>") + (v || 0).toFixed(2) + "</span>" },
  { k: "conv_pct", label: "Конверсия, %", num: true, render: cellFmts.pct },
  { k: "backlog", label: "В корзине", num: true, render: cellFmts.int },
  { k: "current_discount", label: "Скидка сейчас, %", num: true, render: (v) => v == null ? "—" : fmt(v) + "%" },
  { k: "avg_price", label: "Ср. цена факт", num: true, render: cellFmts.money },
  { k: "action", label: "Решение", render: actionCell },
  { k: "target_discount", label: "Целевая скидка, %", num: true, render: (v) => v == null ? "—" : fmt(v) + "%" },
  { k: "target_vis", label: "Целевая цена", num: true, render: cellFmts.money },
  { k: "margin_pct_at_target", label: "Маржа при цели, %", num: true, render: cellFmts.pct },
  { k: "reason", label: "Причина", render: cellFmts.text },
];

function loadPricingSettings() {
  try {
    return JSON.parse(localStorage.getItem("pricing_settings") || "{}");
  } catch (e) {
    return {};
  }
}

function savePricingSettings(s) {
  localStorage.setItem("pricing_settings", JSON.stringify(s));
}

function collectPricingSettings() {
  const box = $("#pricingSettings");
  const s = loadPricingSettings();
  box.querySelectorAll("input[data-key]").forEach((i) => {
    const key = i.dataset.key;
    if (i.type === "checkbox") s[key] = i.checked;
    else {
      const v = parseFloat(i.value);
      if (!isNaN(v)) s[key] = v;
    }
  });
  savePricingSettings(s);
  return s;
}

async function buildPricingSettings() {
  if (!pricingDefaults) {
    try {
      const d = await api("/pricing/defaults");
      pricingDefaults = d.defaults;
    } catch (err) {
      $("#pricingMsg").textContent = "Ошибка загрузки настроек: " + err.message;
      return;
    }
  }
  const saved = loadPricingSettings();
  const box = $("#pricingSettings");
  box.innerHTML = "";
  for (const key of Object.keys(pricingDefaults)) {
    const cur = saved[key] !== undefined ? saved[key] : pricingDefaults[key];
    const lbl = document.createElement("label");
    lbl.className = "p-st";
    const head = document.createElement("span");
    head.className = "p-st-head";
    head.appendChild(document.createTextNode(PRICING_LABELS[key] || key));
    const hint = PRICING_HINTS[key];
    if (hint) {
      const tip = document.createElement("span");
      tip.className = "tip";
      tip.tabIndex = 0;
      tip.setAttribute("role", "tooltip");
      tip.appendChild(document.createTextNode("?"));
      const tipText = document.createElement("span");
      tipText.className = "tip-text";
      tipText.textContent = hint;
      tip.appendChild(tipText);
      head.appendChild(tip);
    }
    lbl.appendChild(head);
    const input = document.createElement("input");
    input.dataset.key = key;
    if (key === "season_adj") {
      input.type = "checkbox";
      input.checked = !!cur;
    } else {
      input.type = "number";
      input.step = (key === "return_penalty" || key === "season_damp") ? "0.1" : "1";
      input.value = cur;
    }
    lbl.appendChild(input);
    box.appendChild(lbl);
  }
}

async function renderPricingHistory() {
  const box = $("#pricingHistory");
  let data;
  try {
    data = await api("/pricing/history?limit=30");
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить журнал</div>';
    return;
  }
  if (!data.rows.length) {
    box.innerHTML = '<div class="empty">Журнал пуст — решения записываются здесь при применении через WB API (сейчас выключено)</div>';
    return;
  }
  const headers = [
    { k: "calculated_at", label: "Когда", render: cellFmts.text },
    { k: "article", label: "Артикул", render: cellFmts.text },
    { k: "action", label: "Решение", render: actionCell },
    { k: "status", label: "Статус", render: cellFmts.text },
    { k: "before_discount", label: "Было, %", num: true, render: (v) => v == null ? "—" : fmt(v) + "%" },
    { k: "after_discount", label: "Стало, %", num: true, render: (v) => v == null ? "—" : fmt(v) + "%" },
    { k: "applied_at", label: "Применено", render: cellFmts.text },
    { k: "reason", label: "Причина", render: cellFmts.text },
  ];
  box.innerHTML = table(headers, data.rows);
}

async function renderPricing(apply) {
  await buildPricingSettings();
  const s = collectPricingSettings();
  const msg = $("#pricingMsg");
  msg.textContent = "Считаю рекомендации…";
  try {
    const data = await apiPost("/pricing/recommendations", s);
    const rows = data.rows || [];
    const actionable = rows.filter((r) => r.action === "RAISE" || r.action === "LOWER").length;
    const underCooldown = rows.filter((r) => r.status === "skipped_cooldown").length;
    let summary = "Товаров: " + fmt(rows.length) + ", решений: " + fmt(actionable);
    if (underCooldown) summary += ", в кулдауне: " + fmt(underCooldown);
    if (data.as_of) summary += " · на " + data.as_of;
    $("#pricingSummary").textContent = summary;
    msg.textContent = data.note || "";
    pagedTable($("#pricingTable"), pricingHeaders, rows);
    await renderPricingHistory();
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

async function exportPricing() {
  await buildPricingSettings();
  const s = collectPricingSettings();
  const msg = $("#pricingMsg");
  msg.textContent = "Формирую Excel…";
  try {
    const resp = await fetch("/api/pricing/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(s),
    });
    if (!resp.ok) {
      let detail = "";
      try {
        const j = await resp.json();
        detail = j.detail || "";
      } catch (e) { /* не Json */ }
      throw new Error(resp.status + " " + detail);
    }
    const blob = await resp.blob();
    const count = resp.headers.get("X-Count");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filenameFromDisposition(resp.headers.get("Content-Disposition")) || "pricing.xlsx";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);
    msg.textContent = "Excel сохранён: строк " + (count == null ? "—" : count);
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  initDates();
  initWriteDb();
  document.querySelectorAll('input[data-date="from"]').forEach((i) => (i.value = $("#fFrom").value));
  document.querySelectorAll('input[data-date="to"]').forEach((i) => (i.value = $("#fTo").value));

  const menuBtn = $("#menuBtn");
  const navLinks = $("#navLinks");
  if (menuBtn && navLinks) {
    menuBtn.addEventListener("click", () => navLinks.classList.toggle("open"));
  }
  const brand = $(".brand");
  if (brand) {
    brand.addEventListener("click", () => {
      openTab("dashboard");
      if (navLinks) navLinks.classList.remove("open");
    });
  }
  document.querySelectorAll(".dropdown").forEach((dropdown) => {
    const dropBtn = dropdown.querySelector(".dropbtn");
    if (!dropBtn) return;
    dropBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const wasOpen = dropdown.classList.contains("open");
      document.querySelectorAll(".dropdown").forEach((d) => d.classList.remove("open"));
      if (!wasOpen) dropdown.classList.add("open");
    });
  });
  document.addEventListener("click", () => {
    document.querySelectorAll(".dropdown").forEach((d) => d.classList.remove("open"));
  });
  document.querySelectorAll(".nav-link").forEach((link) => {
    link.addEventListener("click", (e) => {
      e.preventDefault();
      if (navLinks) navLinks.classList.remove("open");
      openTab(link.dataset.tab, link);
    });
  });
  document.querySelectorAll(".wb-dl").forEach((btn) => {
    const api = btn.dataset.api || "wb";
    const tag = api === "ozon" ? "oz" : "wb";
    const kind = btn.dataset.kind;
    btn.addEventListener("click", () => apiDownload(api, kind, "#" + tag + "Msg-" + kind));
  });
  $("#btnApply").addEventListener("click", () => loadTab(currentTab));
  $("#btnRefreshWb").addEventListener("click", () => openRefresh("wb"));
  $("#btnRefreshOz").addEventListener("click", () => openRefresh("ozon"));
  $("#refreshStart").addEventListener("click", () => startRefreshJob());
  $("#refreshClose").addEventListener("click", () => {
    $("#refreshModal").classList.add("hidden");
    if (refreshPoll) { clearInterval(refreshPoll); refreshPoll = null; }
  });
  $("#refreshModal").addEventListener("click", (e) => {
    if (e.target.id === "refreshModal") $("#refreshModal").classList.add("hidden");
  });
  ["mWb", "mOzon"].forEach((id) => {
    document.getElementById(id).addEventListener("change", () => {
      if (currentTab === "margin") loadTab(currentTab);
    });
  });
  [["marginLike", "margin"], ["marginFunnelLike", "margin-funnel"], ["marginDetailLike", "margin-detail"]].forEach(([id, tab]) => {
    const el = $("#" + id);
    if (!el) return;
    let timer;
    el.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => { if (currentTab === tab) loadTab(currentTab); }, 400);
    });
  });
  $("#oursImport").addEventListener("click", () => uploadFile("/import/custom-stock", $("#oursFile"), "#oursMsg", "ours"));
  $("#productsImport").addEventListener("click", () => uploadFile("/import/products", $("#productsFile"), "#productsMsg", "products"));
  $("#netCostImport").addEventListener("click", () => uploadFile("/import/net-cost", $("#netCostFile"), "#netCostMsg", "products"));
  initCardsUpload();
  const pricingRecalc = $("#pricingRecalc");
  const pricingExport = $("#pricingExport");
  if (pricingRecalc) pricingRecalc.addEventListener("click", () => renderPricing(false));
  if (pricingExport) pricingExport.addEventListener("click", () => exportPricing());
  loadTab(currentTab);
});