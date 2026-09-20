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
function fmtMoney2(n) {
  return new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n || 0) + " \u20BD";
}
function fmtMoney4(n) {
  return new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 4, maximumFractionDigits: 4 }).format(n || 0) + " \u20BD";
}
function fmtVol(n) {
  return new Intl.NumberFormat("ru-RU", { minimumFractionDigits: 1, maximumFractionDigits: 3 }).format(n || 0);
}
function fmtFloat(n, d) {
  return new Intl.NumberFormat("ru-RU", { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 }).format(n || 0);
}
function fmtPct(n) {
  return (n == null || n === "" || isNaN(Number(n))) ? "—" : Number(n).toFixed(1) + "%";
}
function cls(n) { return Number(n) < 0 ? "neg" : "pos"; }

let busyDepth = 0;
function busyRun(fn) {
  const spin = document.getElementById("busySpinner");
  if (spin) spin.classList.remove("hidden");
  document.body.classList.add("busy");
  busyDepth++;
  return Promise.resolve(fn())
    .catch((err) => { console.error("busyRun:", err); throw err; })
    .finally(() => {
      busyDepth--;
      if (busyDepth <= 0) {
        busyDepth = 0;
        if (spin) spin.classList.add("hidden");
        document.body.classList.remove("busy");
      }
    });
}

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

function table(headers, rows, sort, footers) {
  if (!rows.length) return '<div class="empty">Нет данных за выбранный период</div>';
  let h = "<thead><tr>";
  for (const c of headers) {
    let arrow = "";
    if (sort && sort.k === c.k) arrow = sort.dir === "asc" ? " \u25B2" : " \u25BC";
    h += '<th data-k="' + c.k + '" class="' + (c.num ? "num sortable" : "sortable") +
      '" title="' + (c.tip ? c.tip : "Сортировать") + '">' + c.label + arrow + "</th>";
  }
  h += "</tr>";
  if (footers) {
    h += '<tr class="totals-row">';
    for (let i = 0; i < headers.length; i++) {
      const c = headers[i];
      const v = footers[c.k];
      if (i === 0) {
        h += '<th class="totals"><b>ИТОГО</b></th>';
      } else if (v == null) {
        h += "<th class=\"totals\"></th>";
      } else if (c.render === cellFmts.money) {
        h += '<th class="num totals"><b>' + fmtMoney(v) + "</b></th>";
      } else if (c.render === cellFmts.moneyZero) {
        h += '<th class="num totals"><b>' + (v ? fmtMoney(v) : "—") + "</b></th>";
      } else if (c.render === cellFmts.int) {
        h += '<th class="num totals"><b>' + fmt(Math.round(v)) + "</b></th>";
      } else if (c.render === cellFmts.intZero) {
        h += '<th class="num totals"><b>' + (v ? fmt(Math.round(v)) : "—") + "</b></th>";
      } else if (c.render === cellFmts.pct) {
        h += '<th class="num totals"><b>' + fmtPct(v) + "</b></th>";
      } else {
        h += '<th class="num totals"><b>' + fmtMoney(v) + "</b></th>";
      }
    }
    h += "</tr>";
  }
  h += "</thead><tbody>";
  for (const r of rows) {
    h += "<tr>";
    for (const c of headers) h += '<td class="' + (c.num ? "num" : "") + '">' + c.render(r[c.k], r) + "</td>";
    h += "</tr>";
  }
  return "<table>" + h + "</tbody></table>";
}

const cellFmts = {
  money: (v) => v == null ? "—" : fmtMoney(v),
  money2: (v) => v == null ? "—" : fmtMoney2(v),
  money4: (v) => v == null ? "—" : fmtMoney4(v),
  moneyCls: (v) => v == null ? "—" : `<span class="${cls(v)}">${fmtMoney(v)}</span>`,
  pct: (v) => v == null ? "—" : `<span class="${cls(v)}">${fmtPct(v)}</span>`,
  int: (v) => v == null ? "—" : fmt(v),
  intZero: (v) => !v ? "—" : fmt(v),
  moneyZero: (v) => !v ? "—" : fmtMoney(v),
  moneyEst: (v, r) => r && r.net_cost_est
    ? '<span class="est" title="Себестоимость не задана — оценка">~' + fmtMoney(v) + "</span>"
    : fmtMoney(v),
  text: (v) => (v == null || v === "") ? "—" : v,
  tag: (v) => `<span class="tag ${v}">${MP_LABELS[v] || v}</span>`,
};

function pagedTable(container, headers, rows, footers, pagerSel) {
  if (!container._pt) container._pt = { limit: 100, showAll: false, sort: null };
  const st = container._pt;
  st.headers = headers;
  st.rows = rows;
  st.footers = footers || null;
  st.pagerSel = pagerSel || null;
  if (!pagerSel && container.id) {
    const auto = document.getElementById(container.id + "Pager");
    if (auto) st.pagerSel = "#" + auto.id;
  }
  if (st.sort && !st.headers.some((x) => x.k === st.sort.k)) st.sort = null;
  if (!container._bound) {
    container._bound = true;
    container.addEventListener("click", (ev) => {
      const th = ev.target.closest("th[data-k]");
      if (!th || !container._pt) return;
      const k = th.dataset.k;
      const s = container._pt.sort;
      if (s && s.k === k) {
        s.dir = s.dir === "asc" ? "desc" : "asc";
      } else {
        container._pt.sort = { k, dir: "asc" };
      }
      paint();
    });
  }
  const paint = () => {
    container.innerHTML = "";
    const pagerHost = st.pagerSel ? document.querySelector(st.pagerSel) : null;
    if (pagerHost) pagerHost.innerHTML = "";
    if (!st.rows || !st.rows.length) {
      container.innerHTML = '<div class="empty">Нет данных за выбранный период</div>';
      return;
    }
    let list = st.rows;
    if (st.sort) {
      const hd = st.headers.find((x) => x.k === st.sort.k);
      if (hd) {
        const k = st.sort.k;
        const dir = st.sort.dir === "asc" ? 1 : -1;
        list = st.rows.slice().sort((a, b) => {
          let cmp;
          if (hd.num) {
            cmp = (Number(a[k]) || 0) - (Number(b[k]) || 0);
          } else {
            cmp = String(a[k] == null ? "" : a[k]).localeCompare(
              String(b[k] == null ? "" : b[k]), "ru");
          }
          return cmp * dir;
        });
      }
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
      btn.textContent = "Загрузить все (" + fmt(Math.max(0, st.rows.length - st.limit)) + ")";
      bar.appendChild(btn);
      input.addEventListener("change", () => {
        let v = parseInt(input.value, 10);
        if (!v || v < 1) v = 1;
        st.limit = Math.min(v, st.rows.length);
        paint();
      });
      btn.addEventListener("click", () => {
        st.showAll = true;
        paint();
      });
    }
    const visible = st.showAll ? list : list.slice(0, st.limit);
    span.textContent = "Показано " + fmt(visible.length) + " из " + fmt(st.rows.length);
    (pagerHost || container).appendChild(bar);
    const wrap = document.createElement("div");
    wrap.innerHTML = table(st.headers, visible, st.sort, st.footers);
    container.appendChild(wrap);
    const tr = wrap.querySelector("tr.totals-row");
    if (tr) {
      const hdr = wrap.querySelector("thead tr:not(.totals-row)");
      const h = hdr ? hdr.offsetHeight : 0;
      tr.querySelectorAll("th").forEach((th) => { th.style.top = h + "px"; });
    }
  };
  paint();
}

function tabLike(id) {
  return $("#" + id) ? $("#" + id).value.trim() : "";
}

async function loadTab(name) {
  const f = filters();
  await busyRun(() => loadTabInner(name, f));
}

async function loadTabInner(name, f) {

  try {
    if (name === "dashboard") await renderDashboard(qs(f));
    else if (name === "margin") {
      await renderMargin(qs({ marketplace: marginMarketplace(), date_from: f.date_from, date_to: f.date_to, article_like: tabLike("marginLike") || undefined }));
    } else if (name === "margin-funnel") {
      await renderMarginFunnel(qs({ date_from: f.date_from, date_to: f.date_to, article_like: tabLike("marginFunnelLike") || undefined }));
    } else if (name === "margin-detail") {
      const cmpEl = $("#marginDetailCompare");
      await renderMarginDetail(qs({ date_from: f.date_from, date_to: f.date_to, article_like: tabLike("marginDetailLike") || undefined, compare: cmpEl && cmpEl.checked ? 1 : undefined }));
    } else if (name === "sales") await renderSales(qs(f));
    else if (name === "stocks") await renderStocks(f.marketplace);
    else if (name === "ours") await renderOurs();
    else if (name === "products") await renderProducts();
    else if (name === "wh-cp") await renderWhCp();
    else if (name === "wh-receipt") await renderWhDocs("receipt", "whRTable", "whRMsg", "whRDetail");
    else if (name === "wh-shipment") await renderWhDocs("shipment", "whSTable", "whSMsg", "whSDetail");
    else if (name === "wh-stock") await renderWhStock();
    else if (name === "wh-turnover") await renderWhTurnover();
    else if (name === "pricing") await renderPricing(false);
    else if (name === "wb-cards" || name === "oz-cards") await renderCards(name);
    else if (name === "wb-funnel") await renderWbFunnel();
    else if (name === "wb-stock") await renderWbStocks();
    else if (name === "wb-prices") await renderWbPrices();
    else if (name === "wb-storage") await renderWbStorage();
    else if (name === "wb-sales") await renderWbSales();
    else if (name === "wb-detail") await renderWbDetail();
    else if (name === "oz-stock") await renderOzStocks();
    else if (name === "oz-prices") await renderOzPrices();
    else if (name === "oz-realization") await renderOzSales();
    else if (name === "oz-detail") await renderOzDetail();
    else if (name === "yandex") await renderYandexFiles();
    else if (name === "tickets") await renderTickets();
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

const marginTableHeaders = [
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
async function renderMargin(p) {
  const data = await api("/margin" + p);
  const headers = colViewHeaders("margin", marginTableHeaders);
  const draw = (rows) => { pagedTable($("#marginTable"), headers, rows); };
  draw(data.rows);
  $("#marginSearch").oninput = (e) => {
    const q = e.target.value.trim().toLowerCase();
    draw(data.rows.filter((r) => (r.article + " " + (r.name || "")).toLowerCase().includes(q)));
  };
  const cp = colViewParam("margin");
  $("#exportMargin").href = "/api/export/margin" + p + (cp ? (p ? "&" : "?") + cp : "");
}

const marginHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text },
  { k: "name",    label: "Наименование", render: cellFmts.text },
  { k: "sells",   label: "Продано, шт", num: true, render: cellFmts.int },
  { k: "returns_qty", label: "Возвращено, шт", num: true, render: cellFmts.int },
  { k: "revenue", label: "Выручка", num: true, render: cellFmts.money },
  { k: "commission", label: "Комиссия", num: true, render: cellFmts.moneyCls },
  { k: "logistics", label: "Логистика", num: true, render: cellFmts.moneyCls },
  { k: "logistics_out", label: "Логистика туда", num: true, render: cellFmts.moneyCls, tip: "Доставка покупателю (строки Продажа)" },
  { k: "logistics_in", label: "Логистика обратно", num: true, render: cellFmts.moneyCls, tip: "Обратная доставка (строки Возврат)" },
  { k: "storage", label: "Хранение (оц)", num: true, render: cellFmts.moneyCls, tip: "Безартикульные платы WB разнесены по «объём × тариф × остаток»" },
  { k: "services", label: "Услуги", num: true, render: cellFmts.moneyCls },
  { k: "income",  label: "К перечислению", num: true, render: cellFmts.money },
  { k: "net_cost", label: "Себестоимость", num: true, render: cellFmts.money },
  { k: "margin_gross", label: "Маржа, до себестоимости", num: true, render: cellFmts.moneyCls },
  { k: "margin",  label: "Прибыль", num: true, render: cellFmts.moneyCls },
  { k: "margin_per_one", label: "Прибыль на ед.", num: true, render: cellFmts.moneyCls },
  { k: "margin_pct", label: "Прибыль, %", num: true, render: cellFmts.pct },
  { k: "commission_per_one", label: "Комиссия/ед.", num: true, render: cellFmts.moneyCls },
  { k: "logistics_per_one", label: "Логистика/ед.", num: true, render: cellFmts.moneyCls },
  { k: "logistics_out_per_one", label: "Логистика туда/ед.", num: true, render: cellFmts.moneyCls },
  { k: "logistics_in_per_one", label: "Логистика обратно/ед.", num: true, render: cellFmts.moneyCls },
  { k: "storage_per_one", label: "Хранение/ед.", num: true, render: cellFmts.moneyCls },
  { k: "income_per_one", label: "К перечисл./ед.", num: true, render: cellFmts.moneyCls },
  { k: "revenue_per_one", label: "Средняя цена", num: true, render: cellFmts.moneyCls },
  { k: "margin_gross_per_one", label: "Маржа до себест./ед.", num: true, render: cellFmts.moneyCls },
  { k: "return_rate", label: "Доля возвратов, %", num: true, render: cellFmts.pct },
];
const MARGIN_DETAIL_OPTIONAL = [
  { k: "returns_qty", label: "Возвращено, шт" },
  { k: "logistics_out", label: "Логистика туда" },
  { k: "logistics_in", label: "Логистика обратно" },
  { k: "storage", label: "Хранение (оц)" },
  { k: "services", label: "Услуги" },
  { k: "net_cost", label: "Себестоимость" },
  { k: "margin_gross", label: "Маржа до себестоимости" },
  { k: "margin_per_one", label: "Прибыль на ед." },
  { k: "commission_per_one", label: "Комиссия/ед." },
  { k: "logistics_per_one", label: "Логистика/ед." },
  { k: "logistics_out_per_one", label: "Логистика туда/ед." },
  { k: "logistics_in_per_one", label: "Логистика обратно/ед." },
  { k: "storage_per_one", label: "Хранение/ед." },
  { k: "income_per_one", label: "К перечисл./ед." },
  { k: "revenue_per_one", label: "Средняя цена" },
  { k: "margin_gross_per_one", label: "Маржа до себест./ед." },
  { k: "return_rate", label: "Доля возвратов, %" },
];
const _OLD_OPTIONAL = new Set(["storage", "services", "net_cost", "margin_gross", "margin_per_one"]);
// ----------------------------------------------------- «Вид таблицы» — единый механизм
// Для каждого таба регистрируется набор настраиваемых колонок. Состояние живёт в
// localStorage под ключом `<storageKey>[_<mode>]` (mode — активный режим таба, напр.
// "rows"/"summary" у wb-detail). Значение — { колонка: true/false }. По умолчанию
// колонка видна (def=true), пока её не скрыли.
const _COLVIEWS = {};
function registerColView(tab, cfg) { _COLVIEWS[tab] = cfg; }
function ccTab(tab) {
  const [head, ...rest] = tab.split("-");
  return head + rest.map((s) => s[0].toUpperCase() + s.slice(1)).join("");
}
function viewBtnId(tab) { return "btn" + ccTab(tab) + "View"; }
function viewMenuId(tab) { return ccTab(tab) + "ViewMenu"; }
function viewPanelId(tab) { return ccTab(tab) + "ViewPanel"; }
function colViewSet(tab) {
  const c = _COLVIEWS[tab];
  if (!c) return null;
  const mode = c.mode ? c.mode() : "base";
  const set = c.sets ? c.sets[mode] : { headers: c.headers, optional: c.optional };
  if (!set) return null;
  return {
    key: c.storageKey + (c.mode ? "_" + mode : ""),
    headers: set.headers,
    optional: set.optional,
  };
}
function colViewState(tab) {
  const set = colViewSet(tab);
  if (!set) return null;
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(set.key)); } catch (e) { saved = null; }
  if (!saved || typeof saved !== "object") saved = {};
  const st = {};
  for (const o of set.optional) st[o.k] = (o.k in saved) ? !!saved[o.k] : !!o.def;
  return st;
}
function colViewSave(tab, state) {
  const set = colViewSet(tab);
  if (!set) return;
  localStorage.setItem(set.key, JSON.stringify(state));
}
function colViewHeaders(tab, headersList) {
  const set = colViewSet(tab);
  if (!set) return headersList;
  const st = colViewState(tab);
  return headersList.filter((h) => st[h.k] !== false);
}
// Параметр экспорта «cols» — только видимые колонки. Если видны все — не отправляем
// ничего (бэкенд отдаёт полный набор). extraKeys — всегда добавляемые ключи (напр.
// колонки сравнения у margin-detail). modeHint — принудительный режим (напр. wb-prices
// в свёрнутом режиме экспортируется по базовым колонкам, т.к. агрегат на сервере не считается).
function colViewParam(tab, extraKeys, modeHint) {
  const c = _COLVIEWS[tab];
  if (!c) return "";
  const mode = modeHint || (c.mode ? c.mode() : "base");
  const set = c.sets ? c.sets[mode] : { headers: c.headers, optional: c.optional };
  if (!set) return "";
  const st = colViewState(tab);
  const optKeys = new Set(set.optional.map((o) => o.k));
  const keys = [];
  for (const h of set.headers) {
    if (optKeys.has(h.k)) {
      if (st[h.k] !== false) keys.push(h.k);
    } else {
      keys.push(h.k);
    }
  }
  if (extraKeys) keys.push(...extraKeys);
  return keys.length ? "cols=" + keys.join(",") : "";
}
function buildColViewMenu(tab) {
  const set = colViewSet(tab);
  const panel = $("#" + viewPanelId(tab));
  if (!set || !panel) return;
  panel.innerHTML = "";
  const st = colViewState(tab);
  const mkChk = (k, label, checked, onChange) => {
    const lbl = document.createElement("label");
    lbl.className = "chk";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = checked;
    cb.addEventListener("change", onChange);
    lbl.appendChild(cb);
    lbl.appendChild(document.createTextNode(" " + label));
    panel.appendChild(lbl);
    return cb;
  };
  const allOn = set.optional.every((o) => !!st[o.k]);
  mkChk("__all", "Показать все", allOn, (e) => {
    for (const o of set.optional) st[o.k] = e.target.checked;
    colViewSave(tab, st);
    if (currentTab === tab) loadTab(tab);
    buildColViewMenu(tab);
  });
  const sep = document.createElement("hr");
  sep.style.margin = "4px 0";
  panel.appendChild(sep);
  for (const o of set.optional) {
    mkChk(o.k, o.label, !!st[o.k], (e) => {
      st[o.k] = e.target.checked;
      colViewSave(tab, st);
      if (currentTab === tab) loadTab(tab);
      buildColViewMenu(tab);
    });
  }
}
// Общий обработчик кнопки «Вид таблицы» (открыть/закрыть панель).
function initColViewMenu(tab) {
  const btn = $("#" + viewBtnId(tab));
  const menu = $("#" + viewMenuId(tab));
  if (!btn || !menu) return;
  menu.addEventListener("click", (e) => e.stopPropagation());
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const panel = $("#" + viewPanelId(tab));
    const wasHidden = !panel || panel.classList.contains("hidden");
    for (const t of Object.keys(_COLVIEWS)) {
      const p = $("#" + viewPanelId(t));
      if (p) p.classList.add("hidden");
    }
    if (panel && wasHidden) {
      buildColViewMenu(tab);
      panel.classList.remove("hidden");
    }
  });
}
document.addEventListener("click", () => {
  for (const t of Object.keys(_COLVIEWS)) {
    const p = $("#" + viewPanelId(t));
    if (p) p.classList.add("hidden");
  }
});

const wbDetailRowHeaders = [
  { k: "date", label: "Дата", num: true, render: cellFmts.text },
  { k: "article", label: "Артикул", render: cellFmts.text },
  { k: "title", label: "Наименование", render: cellFmts.text },
  { k: "doc_type", label: "Тип документа", render: cellFmts.text },
  { k: "quantity", label: "Кол-во", num: true, render: cellFmts.int },
  { k: "retail_amount", label: "Реализовано", num: true, render: cellFmts.money },
  { k: "commission", label: "КВВ", num: true, render: cellFmts.money },
  { k: "for_pay", label: "К перечислению", num: true, render: cellFmts.money },
  { k: "logistics", label: "Доставка", num: true, render: cellFmts.money },
  { k: "storage", label: "Хранение", num: true, render: cellFmts.money },
  { k: "office", label: "Склад", render: cellFmts.text },
  { k: "source", label: "Источник", render: cellFmts.tag },
];

const wbDetailSummaryHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text },
  { k: "title", label: "Наименование", render: cellFmts.text },
  { k: "sells", label: "Продано, шт", num: true, render: cellFmts.int },
  { k: "returns_qty", label: "Возвращено, шт", num: true, render: cellFmts.int },
  { k: "revenue", label: "Реализовано", num: true, render: cellFmts.money },
  { k: "commission", label: "Комиссия", num: true, render: cellFmts.money },
  { k: "for_pay", label: "К перечислению", num: true, render: cellFmts.money },
  { k: "logistics", label: "Доставка", num: true, render: cellFmts.money },
  { k: "delivery_count", label: "Доставок", num: true, render: cellFmts.int },
  { k: "return_delivery_count", label: "Возврат доставок", num: true, render: cellFmts.int },
  { k: "storage", label: "Хранение (оц)", num: true, render: cellFmts.money, tip: "Оценка: безартикульные платы WB разнесены по «объём × тариф × остаток»" },
  { k: "pvz_compensation", label: "ПВЗ-компенсации", num: true, render: cellFmts.money },
  { k: "payment_services", label: "Платёжные услуги", num: true, render: cellFmts.money },
  { k: "services", label: "Услуги/штрафы", num: true, render: cellFmts.money },
  { k: "ops_count", label: "Операций", num: true, render: cellFmts.int },
  { k: "sources", label: "Источник", render: cellFmts.tag },
];

const ozDetailRowHeaders = [
  { k: "date", label: "Дата", num: true, render: cellFmts.text },
  { k: "posting_number", label: "Постинг", render: cellFmts.text },
  { k: "offer_id", label: "Артикул", render: cellFmts.text },
  { k: "name", label: "Наименование", render: cellFmts.text },
  { k: "sku", label: "SKU", render: cellFmts.text },
  { k: "quantity", label: "Кол-во", num: true, render: cellFmts.int },
  { k: "seller_price", label: "Цена", num: true, render: cellFmts.money },
  { k: "amount", label: "Сумма", num: true, render: cellFmts.money },
  { k: "commission", label: "Комиссия", num: true, render: cellFmts.moneyCls },
  { k: "standard_fee", label: "Услуги", num: true, render: cellFmts.moneyCls },
  { k: "income", label: "К перечислению", num: true, render: cellFmts.money },
  { k: "return_qty", label: "Возврат, шт", num: true, render: cellFmts.int },
  { k: "return_total", label: "Возврат, руб", num: true, render: cellFmts.money },
  { k: "source", label: "Источник", render: cellFmts.tag },
];

const ozDetailSummaryHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text },
  { k: "name", label: "Наименование", render: cellFmts.text },
  { k: "sells", label: "Продано, шт", num: true, render: cellFmts.int },
  { k: "returns_qty", label: "Возвращено, шт", num: true, render: cellFmts.int },
  { k: "postings", label: "Постингов", num: true, render: cellFmts.int },
  { k: "seller_total", label: "Продажи (цена×кол-во)", num: true, render: cellFmts.money },
  { k: "amount", label: "Реализовано", num: true, render: cellFmts.money },
  { k: "commission", label: "Комиссия", num: true, render: cellFmts.moneyCls },
  { k: "services", label: "Услуги", num: true, render: cellFmts.moneyCls },
  { k: "income", label: "К перечислению", num: true, render: cellFmts.money },
  { k: "ops_count", label: "Операций", num: true, render: cellFmts.int },
  { k: "buyout_sum", label: "Сумма выкупов", num: true, render: cellFmts.money },
  { k: "buyout_percent", label: "Выкуп, %", num: true, render: cellFmts.pct },
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
  pagedTable($("#marginFunnelTable"), colViewHeaders("margin-funnel", funnelHeaders), data.rows || []);
  const cp = colViewParam("margin-funnel");
  $("#exportMarginFunnel").href = "/api/export/margin/funnel" + p + (cp ? (p ? "&" : "?") + cp : "");
  const msg = $("#marginFunnelMsg");
  if (data.snapshot_from && data.snapshot_to) {
    msg.textContent = "Срез воронки за " + data.snapshot_from + " … " + data.snapshot_to;
  } else {
    msg.textContent = "Нет данных. Сначала скачайте WB API ▸ Воронка продаж.";
  }
}

async function renderMarginDetail(p) {
  const compare = /compare=1/.test(p || "");
  const data = await api("/margin/detail" + p);
  let headers = colViewHeaders("margin-detail", marginHeaders);
  if (compare) {
    headers = headers.concat([
      { k: "sells_pp", label: "Пред. период: шт", num: true, render: cellFmts.int },
      { k: "margin_pp", label: "Пред. период: Прибыль", num: true, render: cellFmts.moneyCls },
      { k: "delta_ru", label: "Δ прибыли", num: true, render: cellFmts.moneyCls },
      { k: "delta_pct", label: "Δ, %", num: true, render: cellFmts.pct },
    ]);
  }
  pagedTable($("#marginDetailTable"), headers, data.rows || [], data.totals);
  const exportBtn = $("#exportMarginDetail");
  if (exportBtn) {
    const cp = colViewParam("margin-detail", compare ? ["sells_pp", "margin_pp", "delta_ru", "delta_pct"] : null);
    exportBtn.dataset.url = "/api/export/margin/detail" + p + (cp ? (p ? "&" : "?") + cp : "");
  }
  const msg = $("#marginDetailMsg");
  const cmpMsg = $("#marginDetailCompareMsg");
  if (cmpMsg) {
    cmpMsg.textContent = (compare && data.prev_window) ?
      "сравнение: " + data.prev_window.date_from + " … " + data.prev_window.date_to :
      (compare ? "для сравнения нужны даты «С» и «По»" : "");
  }
  if ((data.rows || []).length === 0) {
    msg.textContent =
      "Нет данных. Финансовый отчёт WB скачивается отдельным ключом (finance): WB API ▸ Детализация продаж. " +
      "Запрос редкий (1 в ~12 ч), отчёт формируется на вчерашний день.";
  } else {
    msg.textContent = "Строк: " + fmt((data.rows || []).length);
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
  pagedTable($("#stocksTable"), headers, aggregateStocks(data.rows || []));
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

// ─────────────────────────────── «Наш склад» ───────────────────────────────

let whCpLabels = {};

const whCpHeaders = [
  { k: "name", label: "Наименование", render: cellFmts.text },
  { k: "ctype", label: "Тип", render: (v) => v == null ? "—" : (whCpLabels[v] || v) },
  { k: "inn", label: "ИНН", render: cellFmts.text },
  { k: "phone", label: "Телефон", render: cellFmts.text },
  { k: "note", label: "Примечание", render: cellFmts.text },
];

async function renderWhCp() {
  const box = $("#whCpTable");
  let data;
  try {
    data = await api("/warehouse/counterparties");
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить контрагентов: ' + escapeHtml(err.message) + "</div>";
    return;
  }
  whCpLabels = data.labels || {};
  pagedTable(box, whCpHeaders, data.rows || []);
}

const whDocHeaders = [
  { k: "date", label: "Дата", render: cellFmts.text },
  { k: "doc_num", label: "№ документа", render: cellFmts.text },
  { k: "counterparty", label: "Контрагент", render: cellFmts.text },
  { k: "total", label: "Сумма", num: true, render: cellFmts.money },
  { k: "items_count", label: "Строк", num: true, render: cellFmts.int },
  { k: "source", label: "Источник", render: cellFmts.text },
  { k: "_d", label: "", render: (v, r) => r.id ? '<button class="btn small" data-doc-id="' + r.id + '">Строки</button>' : "" },
];

const whDocItemHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text },
  { k: "name", label: "Наименование", render: cellFmts.text },
  { k: "quantity", label: "Кол-во", num: true, render: (v) => v == null ? "—" : fmtFloat(v, 0) },
  { k: "price", label: "Цена", num: true, render: cellFmts.money2 },
  { k: "amount", label: "Сумма", num: true, render: cellFmts.money2 },
];

async function renderWhDocs(type, boxId, msgId, detailId) {
  const box = $("#" + boxId);
  const detail = $("#" + detailId);
  let data;
  try {
    data = await api("/warehouse/docs" + qs({ type }));
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить ' + (type === "receipt" ? "приход" : "отгрузки") + ": " + escapeHtml(err.message) + "</div>";
    return;
  }
  const msg = $("#" + msgId);
  if (msg) msg.textContent = "Документов: " + fmt(data.count);
  detail.innerHTML = "";
  pagedTable(box, whDocHeaders, data.rows || []);
}

const whStockHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text },
  { k: "name", label: "Наименование", render: cellFmts.text },
  { k: "start_qty", label: "Начальный", num: true, render: (v) => v == null ? "—" : fmtFloat(v, 0) },
  { k: "received", label: "Приход", num: true, render: (v) => v == null ? "—" : fmtFloat(v, 0) },
  { k: "shipped", label: "Отгрузка", num: true, render: (v) => v == null ? "—" : fmtFloat(v, 0) },
  { k: "balance", label: "Остаток", num: true, render: (v) => v == null ? "—" : fmtFloat(v, 0) },
  { k: "avg_cost", label: "Себестоимость ед.", num: true, render: cellFmts.money2 },
  { k: "stock_value", label: "Стоимость остатков", num: true, render: cellFmts.money },
];

async function renderWhStock() {
  const box = $("#whStockTable");
  const q = tabLike("whStockLike");
  let data;
  try {
    data = await api("/warehouse/stock" + qs({ article_like: q || undefined }));
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить остатки: ' + escapeHtml(err.message) + "</div>";
    return;
  }
  pagedTable(box, whStockHeaders, data.rows || []);
}

const whTurnoverHeaders = [
  { k: "counterparty", label: "Контрагент", render: cellFmts.text },
  { k: "in_n", label: "Приход, док.", num: true, render: cellFmts.int },
  { k: "in_sum", label: "Приход, сумма", num: true, render: cellFmts.money },
  { k: "out_n", label: "Отгрузка, док.", num: true, render: cellFmts.int },
  { k: "out_sum", label: "Отгрузка, сумма", num: true, render: cellFmts.money },
];

async function renderWhTurnover() {
  const box = $("#whTurnoverTable");
  let data;
  try {
    data = await api("/warehouse/turnover");
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить обороты: ' + escapeHtml(err.message) + "</div>";
    return;
  }
  pagedTable(box, whTurnoverHeaders, data.rows || []);
}

const productsBaseHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text },
  { k: "name", label: "Наименование", render: cellFmts.text },
  { k: "brand", label: "Бренд", render: cellFmts.text },
  { k: "barcode", label: "Баркод", render: cellFmts.text },
  { k: "sizes_count", label: "Размеров", num: true, render: cellFmts.int },
  { k: "net_cost", label: "Себестоимость", num: true, render: cellFmts.money },
  { k: "recommended_price", label: "Рекомендуемая цена", num: true, render: cellFmts.money },
  { k: "markup", label: "Наценка, %", num: true, render: (v) => v == null ? "—" : fmtPct(Number(v) * 100) },
  { k: "replenishable", label: "Докупаемый", render: replenishableCell },
];
const productsSizeHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text },
  { k: "size", label: "Размер", render: cellFmts.text },
  { k: "name", label: "Наименование", render: cellFmts.text },
  { k: "brand", label: "Бренд", render: cellFmts.text },
  { k: "barcode", label: "Баркод", render: cellFmts.text },
  { k: "net_cost", label: "Себестоимость", num: true, render: cellFmts.money },
  { k: "recommended_price", label: "Рекомендуемая цена", num: true, render: cellFmts.money },
  { k: "markup", label: "Наценка, %", num: true, render: (v) => v == null ? "—" : fmtPct(Number(v) * 100) },
  { k: "replenishable", label: "Докупаемый", render: replenishableCell },
];
const productsStockHeaders = [
  { k: "own_stock", label: "Остаток свой", num: true, render: (v) => v == null ? "—" : (Number.isInteger(Number(v)) ? fmt(v) : fmtFloat(Number(v), 1)) },
  { k: "mp_stock", label: "Остаток МП", num: true, render: cellFmts.int },
];

function productsQs() {
  const likeEl = document.getElementById("productsLike");
  const sizesEl = document.getElementById("productsSizes");
  const stocksEl = document.getElementById("productsStocks");
  return qs({
    like: (likeEl && likeEl.value.trim()) || undefined,
    sizes: sizesEl && sizesEl.checked ? 1 : undefined,
    stocks: stocksEl && stocksEl.checked ? 1 : undefined,
  });
}

function productsVisibleHeaders() {
  const sizesEl = document.getElementById("productsSizes");
  const stocksEl = document.getElementById("productsStocks");
  const sizes = sizesEl ? sizesEl.checked : false;
  const stocks = stocksEl ? stocksEl.checked : false;
  let headers = sizes ? productsSizeHeaders : productsBaseHeaders;
  if (stocks) headers = headers.concat(productsStockHeaders);
  return headers;
}

async function renderProducts() {
  const box = $("#productsTable");
  const msg = document.getElementById("productsMsg");
  let data;
  try {
    data = await api("/products" + productsQs());
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить каталог: ' + escapeHtml(err.message) + "</div>";
    if (msg) msg.textContent = "";
    return;
  }
  if (msg) {
    msg.textContent = data.count
      ? "Товаров: " + fmt(data.count)
      : "Каталог пуст — загрузите товары ниже или нажмите «Обновить базу» в шапке";
  }
  pagedTable(box, colViewHeaders("products", productsVisibleHeaders()), data.rows || [], null, "#productsTablePager");
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

function productsExportUrl() {
  const p = productsQs();
  const extra = [];
  const stocksEl = document.getElementById("productsStocks");
  if (stocksEl && stocksEl.checked) extra.push("own_stock", "mp_stock");
  const cp = colViewParam("products", extra);
  return "/api/export/products" + p + (cp ? (p ? "&" : "?") + cp : "");
}

async function downloadProductsExcel() {
  const msg = document.getElementById("productsMsg");
  if (!msg) return;
  msg.textContent = "Формирую Excel…";
  try {
    const resp = await fetch(productsExportUrl());
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
    msg.textContent = "Excel выгружен" + (count != null ? " · строк: " + fmt(count) : "");
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

async function uploadProductsToDisk() {
  const msg = document.getElementById("productsMsg");
  if (!msg) return;
  msg.textContent = "Формирую файл…";
  try {
    const resp = await fetch(productsExportUrl());
    if (!resp.ok) throw new Error(resp.status + " " + (await resp.text()));
    const blob = await resp.blob();
    const fd = new FormData();
    const name = "products_" + yandexStamp() + ".xlsx";
    fd.append("file", blob, name);
    msg.textContent = "Загружаю на Яндекс.Диск (" + name + ")…";
    const up = await fetch("/api/yandex/upload", { method: "POST", body: fd });
    const j = await up.json();
    if (!up.ok) throw new Error(j.detail || up.status);
    msg.textContent = "На Яндекс.Диске: /agent_market/" + j.name;
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

let productsPriceMeta = null;

function loadProductsPriceSettings() {
  try {
    return JSON.parse(localStorage.getItem("products_price_settings") || "{}");
  } catch (e) {
    return {};
  }
}

function saveProductsPriceSettings(s) {
  localStorage.setItem("products_price_settings", JSON.stringify(s));
}

function productsAnchorVal(el) {
  const out = [];
  const txt = (el.value || "").replace(/\r/g, "").split("\n");
  for (const line of txt) {
    const m = /([\d.,]+)\s*[×xх*]\s*([\d.,]+)/.exec(line.trim());
    if (m) {
      const a = parseFloat(m[1].replace(",", "."));
      const b = parseFloat(m[2].replace(",", "."));
      if (!isNaN(a) && !isNaN(b)) out.push([a, b]);
    }
  }
  return out;
}

function collectProductsPriceSettings() {
  const s = loadProductsPriceSettings();
  const box = document.getElementById("productsPriceFields");
  if (!box) return s;
  box.querySelectorAll("textarea[data-key]").forEach((el) => {
    const anchors = productsAnchorVal(el);
    if (anchors.length >= 2) s[el.dataset.key] = anchors;
  });
  const chk = box.querySelector('input[data-key="round_nice"]');
  if (chk) s.round_nice = chk.checked;
  saveProductsPriceSettings(s);
  return s;
}

async function buildProductsPricePanel() {
  if (!productsPriceMeta) {
    try {
      productsPriceMeta = await api("/products/price-settings");
    } catch (err) {
      const msgEl = document.getElementById("productsPriceMsg");
      if (msgEl) msgEl.textContent = "Ошибка загрузки настроек цены: " + err.message;
      return;
    }
  }
  const labels = productsPriceMeta.labels || {};
  const hints = productsPriceMeta.hints || {};
  const defaults = productsPriceMeta.defaults || {};
  const saved = loadProductsPriceSettings();
  const box = document.getElementById("productsPriceFields");
  box.innerHTML = "";
  for (const key of ["cost_anchors", "vol_anchors", "round_nice"]) {
    if (!(key in defaults)) continue;
    const cur = saved[key] !== undefined ? saved[key] : defaults[key];
    const field = document.createElement("div");
    field.className = "p-st";
    const head = document.createElement("span");
    head.className = "p-st-head";
    head.appendChild(document.createTextNode(labels[key] || key));
    const hint = hints[key];
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
    field.appendChild(head);
    if (key === "round_nice") {
      const chk = document.createElement("input");
      chk.type = "checkbox";
      chk.dataset.key = key;
      chk.checked = !!cur;
      field.appendChild(chk);
    } else {
      const ta = document.createElement("textarea");
      ta.dataset.key = key;
      ta.rows = 3;
      ta.value = (cur || []).map((a) => String(Number(a[0])) + " \u00D7" + String(Number(a[1]))).join("\n");
      field.appendChild(ta);
    }
    box.appendChild(field);
  }
}

async function previewProductsPrices() {
  const msgEl = document.getElementById("productsPriceMsg");
  if (!msgEl) return;
  const likeEl = document.getElementById("productsLike");
  const sizesEl = document.getElementById("productsSizes");
  const stocksEl = document.getElementById("productsStocks");
  const q = likeEl ? likeEl.value.trim() : "";
  msgEl.textContent = "Пересчитываю рекомендуемую цену…";
  try {
    const settings = collectProductsPriceSettings();
    const data = await apiPost("/products/preview", {
      like: q || undefined,
      sizes: sizesEl && sizesEl.checked ? 1 : 0,
      stocks: stocksEl && stocksEl.checked ? 1 : 0,
      price_settings: settings,
    });
    const rows = data.rows || [];
    const msg = document.getElementById("productsMsg");
    if (msg) {
      msg.textContent = "Предпросмотр цены (без записи): " + fmt(rows.length) +
        " — нажмите «Обновить базу» в шапке, чтобы применить на карточках";
    }
    pagedTable(
      $("#productsTable"),
      colViewHeaders("products", productsVisibleHeaders()),
      rows,
      null,
      "#productsTablePager"
    );
    msgEl.textContent = "Готово — пересчитано по вашим коэффициентам";
  } catch (err) {
    msgEl.textContent = "Ошибка: " + err.message;
  }
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

async function apiDownload(api, kind, msgSel, jsonMode) {
  const pane = document.querySelector(".pane.active");
  const from = pane.querySelector('input[data-date="from"]');
  const to = pane.querySelector('input[data-date="to"]');
  const days = pane.querySelector("input[data-days]");
  const month = pane.querySelector("input[data-month]");
  const year = pane.querySelector("input[data-year]");
  const writeDb = pane.querySelector(".write-db");
  const bySize = pane.querySelector(".by-size");
  const params = {};
  if (from) params.date_from = from.value;
  else if ($("#fFrom").value) params.date_from = $("#fFrom").value;
  if (to) params.date_to = to.value;
  else if ($("#fTo").value) params.date_to = $("#fTo").value;
  if (days) params.days = days.value;
  if (month) params.month = month.value;
  if (year) params.year = year.value;
  if (writeDb) params.write_db = writeDb.checked ? 1 : 0;
  if (bySize) params.by_size = bySize.checked ? 1 : 0;
  const overwriteEl = pane.querySelector("#productsOverwrite");
  if (overwriteEl) params.overwrite = overwriteEl.checked ? 1 : 0;
  if (jsonMode) params.excel = 0;
  const msg = document.querySelector(msgSel);
  msg.textContent = "Обновляю…";
  try {
    const resp = await fetch("/api/" + api + "/" + kind + qs(params), { method: "POST" });
    if (!resp.ok) throw new Error(resp.status + " " + (await resp.text()));
    if (jsonMode) {
      const j = await resp.json();
      let m;
      if (j.count != null) {
        m = "База обновлена: строк " + fmt(j.count);
        if (j.window) m += " · период: " + j.window;
      } else if (j.report) {
        m = "Каталог обновлён: карточек " + fmt(j.rows || 0) +
          ", создано товаров " + fmt(j.report.created_products || 0) +
          ", обновлено " + fmt(j.report.updated_products || 0);
        if (j.report.sizes_added) m += ", размеров добавлено " + fmt(j.report.sizes_added);
      } else {
        m = "Обновлено";
      }
      if (writeDb && !writeDb.checked) m += " (без записи в базу)";
      msg.textContent = m;
      pullsCache = null;
      updateLastPull(currentTab);
      if (currentTab === "products" || currentTab.startsWith("wb-") || currentTab.startsWith("oz-")) loadTab(currentTab);
      return;
    }
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
    if (currentTab.startsWith("wb-") || currentTab.startsWith("oz-")) loadTab(currentTab);
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

async function apiUploadDisk(api, kind, msgSel) {
  const pane = document.querySelector(".pane.active");
  const from = pane.querySelector('input[data-date="from"]');
  const to = pane.querySelector('input[data-date="to"]');
  const days = pane.querySelector("input[data-days]");
  const month = pane.querySelector("input[data-month]");
  const year = pane.querySelector("input[data-year]");
  const writeDb = pane.querySelector(".write-db");
  const bySize = pane.querySelector(".by-size");
  const params = {};
  if (from) params.date_from = from.value;
  if (to) params.date_to = to.value;
  if (days) params.days = days.value;
  if (month) params.month = month.value;
  if (year) params.year = year.value;
  if (writeDb) params.write_db = writeDb.checked ? 1 : 0;
  if (bySize) params.by_size = bySize.checked ? 1 : 0;
  const msg = document.querySelector(msgSel);
  msg.textContent = "Формирую файл…";
  try {
    const resp = await fetch("/api/" + api + "/" + kind + qs(params), { method: "POST" });
    if (!resp.ok) throw new Error(resp.status + " " + (await resp.text()));
    const blob = await resp.blob();
    const name = filenameFromDisposition(resp.headers.get("Content-Disposition"));
    const fd = new FormData();
    fd.append("file", blob, name);
    msg.textContent = "Загружаю на Яндекс.Диск (" + name + ")…";
    const up = await fetch("/api/yandex/upload", { method: "POST", body: fd });
    const j = await up.json();
    if (!up.ok) throw new Error(j.detail || up.status);
    msg.textContent = "На Яндекс.Диске: /agent_market/" + j.name;
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

async function wireWhDisk(btnSel, params, msgSel, tab, fromDisk) {
  const btn = $(btnSel);
  if (!btn) return;
  const msg = $(msgSel);
  btn.addEventListener("click", async () => {
    msg.textContent = fromDisk ? "Синхронизирую с Диска…" : "Формирую и загружаю на Диск…";
    try {
      if (fromDisk) {
        const r = await apiPost("/warehouse/fromdisk" + qs(params), {});
        const res = r.results || [];
        const ok = res.filter((x) => !x.error).length;
        const imports = res.map((x) => {
          const n = (x.docs_created || 0) + (x.docs_updated || 0);
          return x.file + (n ? " (" + n + " док.)" : "");
        }).join(", ");
        msg.textContent = "Импортировано с Диска: " + fmt(ok) + " из " + fmt(res.length) + (imports ? " — " + imports : "");
        await loadTab(tab);
      } else {
        const r = await apiPost("/warehouse/todisk" + qs(params), {});
        msg.textContent = "На Диске: " + (r.path || "ok");
      }
    } catch (err) {
      msg.textContent = "Ошибка: " + err.message;
    }
  });
}

function humanSize(n) {
  const v = Number(n) || 0;
  if (v < 1024) return v + " Б";
  if (v < 1024 * 1024) return (v / 1024).toFixed(1) + " КБ";
  return (v / (1024 * 1024)).toFixed(2) + " МБ";
}

async function renderYandexFiles() {
  const box = document.getElementById("yandexTable");
  const msg = document.getElementById("yandexMsg");
  if (msg) msg.textContent = "Загружаю список…";
  let data;
  try {
    data = await api("/yandex/list");
  } catch (err) {
    box.innerHTML = '<div class="empty">Ошибка: ' + escapeHtml(err.message) + "</div>";
    if (msg) msg.textContent = "";
    return;
  }
  const files = (data.files || []).slice().sort((a, b) => String(b.modified).localeCompare(String(a.modified)));
  if (msg) msg.textContent = "Файлов в /agent_market: " + fmt(files.length);
  if (!files.length) {
    box.innerHTML = '<div class="empty">В папке /agent_market пока нет файлов. Воспользуйтесь кнопкой «Загрузить на диск» в панелях отчётов.</div>';
    return;
  }
  let html = '<table><thead><tr><th>Файл</th><th>Изменён</th><th>Размер</th><th></th><th></th></tr></thead><tbody>';
  for (const f of files) {
    html += "<tr><td>" + escapeHtml(f.name) + "</td><td>"
      + (f.modified ? new Date(f.modified).toLocaleString("ru-RU") : "—")
      + "</td><td>" + humanSize(f.size)
      + '</td><td><a class="btn small" href="/api/yandex/download' + qs({ path: f.path }) + '">Скачать</a></td>'
      + '<td><button class="btn small danger" data-yandex-del="' + escapeHtml(f.path) + '">Удалить</button></td></tr>';
  }
  html += "</tbody></table>";
  box.innerHTML = html;
}

function initYandexTab() {
  const del = document.getElementById("yandexTable");
  if (del) {
    del.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("[data-yandex-del]");
      if (!btn) return;
      if (!confirm("Удалить файл с Яндекс.Диска?")) return;
      const path = btn.dataset.yandexDel;
      const msg = document.getElementById("yandexMsg");
      msg.textContent = "Удаляю…";
      try {
        const resp = await fetch("/api/yandex/delete" + qs({ path }), { method: "DELETE" });
        const j = await resp.json();
        if (!resp.ok) throw new Error(j.detail || resp.status);
        msg.textContent = "Удалено: " + path.split("/").pop();
        await renderYandexFiles();
      } catch (err) {
        msg.textContent = "Ошибка: " + err.message;
      }
    });
  }
  const ref = document.getElementById("yandexRefresh");
  if (ref) ref.addEventListener("click", () => renderYandexFiles());
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
  const [shortApi, kind] = name.split("-");
  const apiName = shortApi === "oz" ? "ozon" : "wb";
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

// ------------------------------------------------------------- тикеты
const TICKET_COLS = [
  ["open", "Открытые"],
  ["in_progress", "В работе"],
  ["blocked", "Заблокированные"],
  ["closed", "Закрытые"],
];

function ticketPriBadge(pri) {
  if (!pri) return "";
  return '<span class="ticket-pri pri-' + escapeHtml(pri) + '">' + escapeHtml(pri) + "</span>";
}

function ticketCard(key, t) {
  let body = "";
  if (t.body) body = '<div class="ticket-body">' + escapeHtml(t.body) + "</div>";
  let commit = "";
  if (t.commit) commit = '<span class="ticket-commit">' + escapeHtml(t.commit) + "</span>";
  const id = escapeHtml(t.id);
  const actions = [];
  if (key === "open") {
    actions.push('<button class="btn" data-act="start" data-id="' + id + '">Взять в работу</button>');
    actions.push('<button class="btn" data-act="block" data-id="' + id + '">Заблокировать</button>');
  } else if (key === "in_progress") {
    actions.push('<button class="btn" data-act="close" data-id="' + id + '">Закрыть</button>');
    actions.push('<button class="btn" data-act="decline" data-id="' + id + '">Отклонить</button>');
    actions.push('<button class="btn" data-act="block" data-id="' + id + '">Заблокировать</button>');
  } else if (key === "blocked") {
    actions.push('<button class="btn" data-act="start" data-id="' + id + '">В работу</button>');
    actions.push('<button class="btn" data-act="unblock" data-id="' + id + '">Вернуть в очередь</button>');
  } else {
    actions.push('<button class="btn" data-act="reopen" data-id="' + id + '">Открыть снова</button>');
  }
  let closeForm = "";
  if (key === "in_progress") {
    closeForm = '<div class="ticket-close-form hidden">'
      + '<input type="text" placeholder="хеш коммита (необязательно)">'
      + '<button class="btn btn-primary" data-act="confirm-close" data-id="' + id + '">Подтвердить</button></div>';
  }
  return '<div class="ticket-card state-' + key + '">'
    + '<div class="ticket-head"><b>' + id + "</b>" + ticketPriBadge(t.priority) + "</div>"
    + '<div class="ticket-title">' + escapeHtml(t.title) + "</div>"
    + body + commit
    + '<div class="ticket-actions">' + actions.join("") + "</div>"
    + closeForm
    + "</div>";
}

async function renderTickets() {
  const board = document.getElementById("ticketsBoard");
  const msg = document.getElementById("ticketsMsg");
  if (!board) return;
  board.innerHTML = '<div class="empty">Загружаю…</div>';
  let d;
  try {
    d = await api("/tickets");
  } catch (err) {
    board.innerHTML = '<div class="empty">Ошибка: ' + escapeHtml(err.message) + "</div>";
    if (msg) msg.textContent = "";
    return;
  }
  if (msg) msg.textContent = d.counter ? "Счётчик следующих номеров: T-" + d.counter : "Файл TICKETS.md ещё не создан";
  let html = '<div class="tickets-board">';
  for (const [key, label] of TICKET_COLS) {
    const list = (d.sections && d.sections[key]) || [];
    html += '<div class="ticket-col"><h3 class="ticket-col-h">' + label
      + ' <span class="ticket-count">' + fmt(list.length) + "</span></h3>";
    if (!list.length) {
      html += '<div class="empty">пусто</div>';
    } else {
      for (const t of list) html += ticketCard(key, t);
    }
    html += "</div>";
  }
  html += "</div>";
  board.innerHTML = html;
}

async function ticketAction(act, id, card) {
  const msg = document.getElementById("ticketsMsg");
  try {
    if (act === "close") {
      const form = card.querySelector(".ticket-close-form");
      if (!form) return;
      form.classList.remove("hidden");
      const inp = form.querySelector("input");
      if (inp) inp.focus();
      return;
    }
    let commit = "";
    if (act === "confirm-close") {
      const inp = card.querySelector(".ticket-close-form input");
      commit = inp ? inp.value.trim() : "";
      act = "close";
    }
    await apiPost("/tickets/" + encodeURIComponent(id) + "/" + act, act === "close" ? { commit: commit } : undefined);
    if (msg) { msg.textContent = "Тикет " + id + ": ok"; msg.classList.remove("error"); }
  } catch (err) {
    if (msg) { msg.textContent = err.message; msg.classList.add("error"); }
    return;
  }
  await renderTickets();
}

function initTicketsTab() {
  const board = document.getElementById("ticketsBoard");
  if (!board) return;
  board.addEventListener("click", async (ev) => {
    const btn = ev.target.closest("[data-act]");
    if (!btn) return;
    const id = btn.dataset.id;
    const act = btn.dataset.act;
    const card = btn.closest(".ticket-card");
    if (id && card && act) await ticketAction(act, id, card);
  });
  board.addEventListener("keydown", async (ev) => {
    if (ev.key === "Enter" && ev.target.matches(".ticket-close-form input")) {
      const btn = ev.target.closest(".ticket-card").querySelector('[data-act="confirm-close"]');
      if (btn) btn.click();
    }
  });
  const openModal = () => {
    document.getElementById("ticketModal").classList.remove("hidden");
    document.getElementById("ticketTitle").focus();
  };
  const closeModal = () => {
    document.getElementById("ticketModal").classList.add("hidden");
  };
  document.getElementById("ticketAdd").addEventListener("click", openModal);
  document.getElementById("ticketsRefresh").addEventListener("click", () => renderTickets());
  document.getElementById("ticketModalClose").addEventListener("click", closeModal);
  document.getElementById("ticketModalCancel").addEventListener("click", closeModal);
  document.getElementById("ticketModal").addEventListener("click", (e) => {
    if (e.target.id === "ticketModal") closeModal();
  });
  document.getElementById("ticketCreate").addEventListener("click", async () => {
    const msg = document.getElementById("ticketsMsg");
    const title = document.getElementById("ticketTitle").value.trim();
    if (!title) {
      if (msg) { msg.textContent = "Заголовок обязателен"; msg.classList.add("error"); }
      document.getElementById("ticketTitle").focus();
      return;
    }
    const priority = document.getElementById("ticketPriority").value;
    const body = document.getElementById("ticketBody").value;
    try {
      await apiPost("/tickets", { title, body, priority });
      closeModal();
      document.getElementById("ticketTitle").value = "";
      document.getElementById("ticketBody").value = "";
      document.getElementById("ticketPriority").value = "medium";
      if (msg) { msg.textContent = ""; msg.classList.remove("error"); }
      await renderTickets();
    } catch (err) {
      if (msg) { msg.textContent = err.message; msg.classList.add("error"); }
    }
  });
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
  const msg = document.querySelector(ids.msgTable);
  const st = { rows: [], total: 0, limit: 500, loading: false, fetching: null };
  box._st = st;

  const paint = () => {
    box.innerHTML = "";
    if (!st.total && !st.loading) {
      box.innerHTML = '<div class="empty">Нет загруженных карточек</div>';
      return;
    }
    const bar = document.createElement("div");
    bar.className = "pager";
    const span = document.createElement("span");
    span.className = "pager-lbl";
    const from = st.rows.length ? 1 : 0;
    span.textContent = "Показано " + fmt(from) + "…" + fmt(st.rows.length) + " из " + fmt(st.total);
    bar.appendChild(span);
    if (st.rows.length < st.total) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn pager-all";
      btn.textContent = st.loading ? "Загрузка…" : "Загрузить ещё (" + fmt(Math.min(st.limit, st.total - st.rows.length)) + ")";
      btn.disabled = st.loading;
      btn.addEventListener("click", () => st.fetching());
      bar.appendChild(btn);
    }
    box.appendChild(bar);
    const wrap = document.createElement("div");
    wrap.innerHTML = table(colViewHeaders(name, cardsHeaders), st.rows);
    box.appendChild(wrap);
  };

  st.fetching = async () => {
    if (st.loading) return;
    st.loading = true;
    const q = likeEl ? likeEl.value.trim() : "";
    try {
      const data = await api("/cards" + qs({
        marketplace: ids.mp,
        like: q || undefined,
        limit: st.limit,
        offset: st.rows.length,
      }));
      st.total = data.total || 0;
      if (st.rows.length === 0) st.rows = data.rows || [];
      else st.rows = st.rows.concat(data.rows || []);
      if (msg) msg.textContent = st.total ? "Карточек: " + fmt(st.total) : "Нет загруженных карточек";
    } catch (err) {
      box.innerHTML = '<div class="empty">Не удалось загрузить карточки: ' + escapeHtml(err.message) + "</div>";
    } finally {
      st.loading = false;
      paint();
    }
  };

  await st.fetching();
}

function numDec(d) {
  return (v) => v == null || v === "" || !Number(v) ? "—" : Number(v).toFixed(d).replace(".", ",");
}

function fmtMinutes(v) {
  const n = Number(v || 0);
  if (!n) return "—";
  const d = Math.floor(n / 1440);
  const h = Math.floor((n % 1440) / 60);
  const m = n % 60;
  let s = d ? d + " дн " : "";
  s += h ? h + " ч " : "";
  return s + m + " мин";
}

const wbFunnelCompact = [
  { k: "article", label: "Артикул", render: cellFmts.text },
  { k: "name", label: "Название", render: cellFmts.text },
  { k: "orders", label: "Заказы", num: true, render: cellFmts.int },
  { k: "revenue", label: "Выручка", num: true, render: cellFmts.money },
  { k: "avg_price", label: "Ср. цена", num: true, render: cellFmts.money },
];

const wbFunnelHeaders = [
  { k: "date_from", label: "С", render: cellFmts.text },
  { k: "date_to", label: "По", render: cellFmts.text },
  { k: "article", label: "Артикул", render: cellFmts.text },
  { k: "nm_id", label: "Артикул WB", render: cellFmts.text },
  { k: "name", label: "Название", render: cellFmts.text },
  { k: "subject_name", label: "Предмет", render: cellFmts.text },
  { k: "brand_name", label: "Бренд", render: cellFmts.text },
  { k: "product_rating", label: "Рейтинг карточки", num: true, render: numDec(1) },
  { k: "feedback_rating", label: "Рейтинг по отзывам", num: true, render: numDec(2) },
  { k: "stock_wb", label: "Остатки WB", num: true, render: cellFmts.intZero },
  { k: "stock_mp", label: "Остатки свой склад", num: true, render: cellFmts.intZero },
  { k: "stock_balance_sum", label: "Сумма остатков", num: true, render: cellFmts.moneyZero },
  { k: "views", label: "Просмотры", num: true, render: cellFmts.int },
  { k: "opens", label: "Открытия", num: true, render: cellFmts.intZero },
  { k: "adds", label: "В корзину", num: true, render: cellFmts.int },
  { k: "orders", label: "Заказы", num: true, render: cellFmts.int },
  { k: "buyouts", label: "Выкупы", num: true, render: cellFmts.intZero },
  { k: "cancelled", label: "Отмены", num: true, render: cellFmts.int },
  { k: "cancel_sum", label: "Сумма отмен", num: true, render: cellFmts.moneyZero },
  { k: "avg_price", label: "Ср. цена", num: true, render: cellFmts.moneyZero },
  { k: "revenue", label: "Выручка", num: true, render: cellFmts.money },
  { k: "buyout_sum", label: "Сумма выкупа", num: true, render: cellFmts.moneyZero },
  { k: "avg_orders_per_day", label: "Заказов в день", num: true, render: numDec(2) },
  { k: "share_order_percent", label: "Доля в выручке, %", num: true, render: cellFmts.pct },
  { k: "add_to_wishlist", label: "В отложенные", num: true, render: cellFmts.intZero },
  { k: "time_to_ready_min", label: "Доставка, средн.", num: true, render: fmtMinutes },
  { k: "localization_percent", label: "Локальные, %", num: true, render: cellFmts.pct },
  { k: "conv_to_cart_percent", label: "Просмотр→Корзина, %", num: true, render: cellFmts.pct },
  { k: "conv_cart_to_order_percent", label: "Корзина→Заказ, %", num: true, render: cellFmts.pct },
  { k: "conv_buyout_percent", label: "Заказ→Выкуп, %", num: true, render: cellFmts.pct },
  { k: "wb_club_order_count", label: "WB Клуб: заказы", num: true, render: cellFmts.intZero },
  { k: "wb_club_order_sum", label: "WB Клуб: заказы, ₽", num: true, render: cellFmts.moneyZero },
  { k: "wb_club_buyout_count", label: "WB Клуб: выкупы", num: true, render: cellFmts.intZero },
  { k: "wb_club_buyout_sum", label: "WB Клуб: выкупы, ₽", num: true, render: cellFmts.moneyZero },
  { k: "wb_club_cancel_count", label: "WB Клуб: отмены", num: true, render: cellFmts.intZero },
  { k: "wb_club_cancel_sum", label: "WB Клуб: отмены, ₽", num: true, render: cellFmts.moneyZero },
  { k: "wb_club_avg_price", label: "WB Клуб: ср. цена", num: true, render: cellFmts.moneyZero },
  { k: "wb_club_buyout_percent", label: "WB Клуб: % выкупа", num: true, render: cellFmts.pct },
  { k: "wb_club_avg_orders_per_day", label: "WB Клуб: заказов/день", num: true, render: numDec(2) },
];

function paneDates() {
  const pane = document.querySelector(".pane.active");
  function val(sel) {
    const el = pane ? pane.querySelector(sel) : null;
    return el ? el.value : "";
  }
  return { date_from: val('input[data-date="from"]'), date_to: val('input[data-date="to"]') };
}

async function renderWbFunnel() {
  const box = document.getElementById("wbFunnelTable");
  const likeEl = document.getElementById("wbFunnelLike");
  const expandedEl = document.getElementById("wbFunnelExpanded");
  const f = filters();
  const p = { date_from: f.date_from, date_to: f.date_to };
  const like = likeEl ? likeEl.value.trim() : "";
  let data;
  try {
    data = await api("/funnel" + qs({
      date_from: p.date_from || undefined,
      date_to: p.date_to || undefined,
      article_like: like || undefined,
    }));
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить воронку: ' + escapeHtml(err.message) + "</div>";
    return;
  }
  const msg = document.querySelector("#wbMsg-funnel-table");
  if (msg) msg.textContent = data.count ? "По артикулам: " + fmt(data.count) : "Нет данных в базе";
  const note = document.getElementById("wbFunnelBuyoutNote");
  if (note) {
    const total = (data.rows || []).reduce((s, r) => s + Number(r.buyouts || 0), 0);
    note.style.display = total ? "none" : "block";
  }
  const tipEl = document.getElementById("wbFunnelTip");
  if (tipEl) tipEl.classList.toggle("hidden", !(data.rows && data.rows.length));
  const tipNote = document.getElementById("wbFunnelTipNote");
  if (tipNote) {
    const wantFull = p.date_from && p.date_to && data.snapshot_from;
    const partial = wantFull && (data.snapshot_from !== p.date_from || data.snapshot_to !== p.date_to);
    tipNote.textContent = partial
      ? "В базе срез только за " + data.snapshot_from + " — " + data.snapshot_to + ". Полный период загрузите через «Обновить базу» в шапке."
      : "";
    tipNote.classList.toggle("hidden", !partial);
  }
  const headers = colViewHeaders("wb-funnel", expandedEl && expandedEl.checked ? wbFunnelHeaders : wbFunnelCompact);
  pagedTable(box, headers, data.rows || [], data.totals || null, "#wbFunnelTablePager");
}

function aggregateStocks(rows) {
  const map = new Map();
  for (const r of rows) {
    const key = r.marketplace + "|" + r.article + "|" + r.warehouse;
    const a = map.get(key);
    if (a) { a.quantity += r.quantity; a.quantity_full += r.quantity_full; a.in_way += r.in_way; continue; }
    map.set(key, {
      date: r.date, marketplace: r.marketplace, article: r.article, name: r.name,
      warehouse: r.warehouse, quantity: r.quantity,
      quantity_full: r.quantity_full || 0, in_way: r.in_way || 0,
    });
  }
  return Array.from(map.values());
}

function stockSummary(rows, date, label) {
  if (!date || !rows.length) return "Нет данных";
  let q = 0, qf = 0, iw = 0;
  const arts = new Set();
  for (const r of rows) {
    q += Number(r.quantity || 0);
    qf += Number(r.quantity_full || 0);
    iw += Number(r.in_way || 0);
    if (r.article) arts.add(String(r.article));
  }
  let s = label + " на " + date + " · всего: " + fmt(q) + " шт";
  if (qf !== q) s += " · полн: " + fmt(qf) + " шт";
  s += " · в пути: " + fmt(iw) + " шт · позиций: " + fmt(rows.length)
     + " · артикулов: " + fmt(arts.size);
  return s;
}

const wbStockHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text },
  { k: "name", label: "Наименование", render: cellFmts.text },
  { k: "chrt_id", label: "Код размера", render: cellFmts.text },
  { k: "size", label: "Размер", render: cellFmts.text },
  { k: "barcode", label: "Баркод", render: cellFmts.text },
  { k: "warehouse", label: "Склад", render: cellFmts.text },
  { k: "quantity", label: "Доступно", num: true, render: cellFmts.int },
  { k: "quantity_full", label: "Всего на складах", num: true, render: cellFmts.int },
  { k: "in_way", label: "В пути", num: true, render: cellFmts.int },
];

const wbStockAggHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text },
  { k: "name", label: "Наименование", render: cellFmts.text },
  { k: "warehouse", label: "Склад", render: cellFmts.text },
  { k: "quantity", label: "Доступно", num: true, render: cellFmts.int },
  { k: "quantity_full", label: "Всего на складах", num: true, render: cellFmts.int },
  { k: "in_way", label: "В пути", num: true, render: cellFmts.int },
];

async function renderWbStocks() {
  const box = document.getElementById("wbStockTable");
  const likeEl = document.getElementById("wbStockLike");
  const agg = document.getElementById("wbStockAgg");
  const q = likeEl ? likeEl.value.trim() : "";
  let data;
  try {
    data = await api("/stocks" + qs({ marketplace: "wb" }));
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить остатки: ' + escapeHtml(err.message) + "</div>";
    return;
  }
  let rows = data.rows || [];
  if (agg && agg.checked) {
    rows = aggregateStocks(rows);
  }
  if (q) {
    const needle = q.toLowerCase();
    rows = rows.filter((r) => (r.article + " " + (r.name || "") + " " + (r.size || "")).toLowerCase().includes(needle));
  }
  const head = colViewHeaders("wb-stock", agg && agg.checked ? wbStockAggHeaders : wbStockHeaders);
  const msg = document.querySelector("#wbMsg-stock-table");
  if (msg) msg.textContent = stockSummary(rows, data.date, "Остатки");
  pagedTable(box, head, rows);
}

function aggregatePrices(rows) {
  const map = new Map();
  for (const r of rows) {
    let a = map.get(r.article);
    if (!a) {
      map.set(r.article, {
        article: r.article, name: r.name || "", sizes: 1,
        disc_min: r.discounted_price, disc_max: r.discounted_price,
        price_min: r.price, price_max: r.price,
        disc_min_p: r.discount, disc_max_p: r.discount,
      });
      continue;
    }
    a.sizes += 1;
    for (const [mn, mx, v] of [
      ["disc_min", "disc_max", r.discounted_price],
      ["price_min", "price_max", r.price],
      ["disc_min_p", "disc_max_p", r.discount],
    ]) {
      if (v < a[mn]) a[mn] = v;
      if (v > a[mx]) a[mx] = v;
    }
  }
  return Array.from(map.values());
}

function rangeLabel(mn, mx, fmtFn) {
  if (mn == null) return "—";
  return mn === mx ? fmtFn(mn) : fmtFn(Math.min(mn, mx)) + "–" + fmtFn(Math.max(mn, mx));
}

const wbPricesHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text },
  { k: "name", label: "Наименование", render: cellFmts.text },
  { k: "size", label: "Размер", render: cellFmts.text },
  { k: "discounted_price", label: "Цена со скид.", num: true, render: cellFmts.money },
  { k: "price", label: "Цена без скид.", num: true, render: cellFmts.money },
  { k: "discount", label: "Скидка, %", num: true, render: (v) => v == null ? "—" : fmt(v, 1) + "%" },
];

const wbPricesAggHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text },
  { k: "name", label: "Наименование", render: cellFmts.text },
  { k: "sizes", label: "Размеров", num: true, render: cellFmts.int },
  { k: "disc_min", label: "Цена со скид.", num: true, render: (v, r) => rangeLabel(r.disc_min, r.disc_max, fmtMoney) },
  { k: "price_min", label: "Цена без скид.", num: true, render: (v, r) => rangeLabel(r.price_min, r.price_max, fmtMoney) },
  { k: "disc_min_p", label: "Скидка, %", num: true, render: (v, r) => rangeLabel(r.disc_min_p, r.disc_max_p, (x) => fmt(x, 1) + "%") },
];

async function renderWbPrices() {
  const box = document.getElementById("wbPricesTable");
  const likeEl = document.getElementById("wbPricesLike");
  const aggEl = document.getElementById("wbPriceAgg");
  const q = likeEl ? likeEl.value.trim() : "";
  let data;
  try {
    data = await api("/prices" + qs({ article_like: q || undefined }));
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить цены: ' + escapeHtml(err.message) + "</div>";
    return;
  }
  let rows = data.rows || [];
  const agg = aggEl ? aggEl.checked : false;
  if (agg) rows = aggregatePrices(rows);
  const msg = document.querySelector("#wbMsg-prices-table");
  if (msg) {
    const when = data.updated_at ? " · срез: " + data.updated_at : "";
    msg.textContent = data.count ? "Позиций: " + fmt(data.count) + when : "Нет данных в базе";
  }
  pagedTable(box, colViewHeaders("wb-prices", agg ? wbPricesAggHeaders : wbPricesHeaders), rows);
}

async function renderOzStocks() {
  const box = document.getElementById("ozStockTable");
  const likeEl = document.getElementById("ozStockLike");
  const agg = document.getElementById("ozStockAgg");
  const q = likeEl ? likeEl.value.trim() : "";
  let data;
  try {
    data = await api("/stocks" + qs({ marketplace: "ozon" }));
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить остатки: ' + escapeHtml(err.message) + "</div>";
    return;
  }
  let rows = data.rows || [];
  if (agg && agg.checked) {
    rows = aggregateStocks(rows);
  }
  if (q) {
    const needle = q.toLowerCase();
    rows = rows.filter((r) => (r.article + " " + (r.name || "") + " " + (r.size || "")).toLowerCase().includes(needle));
  }
  const head = colViewHeaders("oz-stock", agg && agg.checked ? wbStockAggHeaders : wbStockHeaders);
  const msg = document.querySelector("#ozMsg-stock-table");
  if (msg) msg.textContent = stockSummary(rows, data.date, "Остатки");
  pagedTable(box, head, rows);
}

async function renderOzPrices() {
  const box = document.getElementById("ozPricesTable");
  const likeEl = document.getElementById("ozPricesLike");
  const aggEl = document.getElementById("ozPriceAgg");
  const q = likeEl ? likeEl.value.trim() : "";
  let data;
  try {
    data = await api("/prices" + qs({ marketplace: "ozon", article_like: q || undefined }));
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить цены: ' + escapeHtml(err.message) + "</div>";
    return;
  }
  let rows = data.rows || [];
  const agg = aggEl ? aggEl.checked : false;
  if (agg) rows = aggregatePrices(rows);
  const msg = document.querySelector("#ozMsg-prices-table");
  if (msg) {
    const when = data.updated_at ? " · срез: " + data.updated_at : "";
    msg.textContent = data.count ? "Позиций: " + fmt(data.count) + when : "Нет данных в базе";
  }
  pagedTable(box, colViewHeaders("oz-prices", agg ? wbPricesAggHeaders : wbPricesHeaders), rows);
}

async function renderOzSales() {
  const box = document.getElementById("ozRealTable");
  const likeEl = document.getElementById("ozRealLike");
  const p = paneDates();
  let data;
  try {
    data = await api("/sales" + qs({
      marketplace: "ozon",
      date_from: p.date_from || undefined,
      date_to: p.date_to || undefined,
    }));
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить реализацию: ' + escapeHtml(err.message) + "</div>";
    return;
  }
  let rows = data.rows || [];
  if (likeEl) {
    const q = likeEl.value.trim().toLowerCase();
    if (q) rows = rows.filter((r) => (r.article + " " + (r.name || "")).toLowerCase().includes(q));
  }
  const msg = document.querySelector("#ozMsg-realization-table");
  if (msg) msg.textContent = data.count ? "Строк: " + fmt(data.count) : "Нет данных за период";
  pagedTable(box, colViewHeaders("oz-realization", salesHeaders), rows);
}

const wbStorageHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text },
  { k: "name", label: "Наименование", render: cellFmts.text },
  { k: "barcodes_count", label: "Баркодов", num: true, render: cellFmts.int },
  { k: "volume", label: "Объём, л", num: true, render: (v) => v == null ? "—" : fmtVol(v) },
  { k: "storage_price", label: "Хранение за баркод", num: true, render: cellFmts.money4 },
  { k: "warehouse_price", label: "Сумма хранения", num: true, render: cellFmts.money4 },
];

async function renderWbStorage() {
  const box = document.getElementById("wbStorageTable");
  const likeEl = document.getElementById("wbStorageLike");
  const q = likeEl ? likeEl.value.trim() : "";
  let data;
  try {
    data = await api("/storage-cost" + qs({ article_like: q || undefined }));
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить хранение: ' + escapeHtml(err.message) + "</div>";
    return;
  }
  const msg = document.querySelector("#wbMsg-storage-table");
  if (msg) {
    const when = data.updated_at ? " · срез: " + data.updated_at : "";
    msg.textContent = data.count ? "Позиций: " + fmt(data.count) + when : "Нет данных в базе";
  }
  pagedTable(box, colViewHeaders("wb-storage", wbStorageHeaders), data.rows || []);
}

const salesHeaders = [
  { k: "date", label: "Дата", render: cellFmts.text },
  { k: "marketplace", label: "Маркетплейс", render: cellFmts.tag },
  { k: "article", label: "Артикул", render: cellFmts.text },
  { k: "name", label: "Наименование", render: cellFmts.text },
  { k: "quantity", label: "Продано, шт", num: true, render: cellFmts.int },
  { k: "revenue", label: "Выручка", num: true, render: cellFmts.money },
  { k: "income", label: "К перечислению", num: true, render: cellFmts.money },
];

async function renderWbSales() {
  const box = document.getElementById("wbSalesTable");
  const likeEl = document.getElementById("wbSalesLike");
  const p = paneDates();
  let data;
  try {
    data = await api("/sales" + qs({
      marketplace: "wb",
      date_from: p.date_from || undefined,
      date_to: p.date_to || undefined,
    }));
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить продажи: ' + escapeHtml(err.message) + "</div>";
    return;
  }
  let rows = data.rows || [];
  if (likeEl) {
    const q = likeEl.value.trim().toLowerCase();
    if (q) rows = rows.filter((r) => (r.article + " " + (r.name || "")).toLowerCase().includes(q));
  }
  const msg = document.querySelector("#wbMsg-sales-table");
  if (msg) msg.textContent = data.count ? "Строк: " + fmt(data.count) : "Нет данных за период";
  pagedTable(box, colViewHeaders("wb-sales", salesHeaders), rows);
}

async function renderWbDetail() {
  const box = document.getElementById("wbDetailTable");
  const likeEl = document.getElementById("wbDetailLike");
  const rawEl = document.getElementById("wbDetailRaw");
  const f = filters();
  const p = { date_from: f.date_from, date_to: f.date_to };
  const q = likeEl ? likeEl.value.trim() : "";
  const raw = rawEl ? rawEl.checked : false;
  const msg = document.querySelector("#wbMsg-detail-table");
  const tipEl = document.getElementById("wbDetailTip");
  try {
    if (raw) {
      const data = await api("/wb/detail-rows" + qs({
        date_from: p.date_from || undefined,
        date_to: p.date_to || undefined,
        article_like: q || undefined,
        limit: 500,
      }));
      const rows = data.rows || [];
      tipEl.classList.add("hidden");
      if (msg) msg.textContent = "Строк в базе: " + fmt(data.total || 0) +
        (rows.length < (data.total || 0) ? " (показаны первые " + fmt(rows.length) + " — меняйте период или поиск)" : "");
      pagedTable(box, colViewHeaders("wb-detail", wbDetailRowHeaders), rows, null, "#wbDetailTablePager");
    } else {
      const data = await api("/wb/detail-summary" + qs({
        date_from: p.date_from || undefined,
        date_to: p.date_to || undefined,
        article_like: q || undefined,
      }));
      const rows = data.rows || [];
      tipEl.classList.remove("hidden");
      if (msg) msg.textContent = "По артикулам: " + fmt(data.count || 0);
      pagedTable(box, colViewHeaders("wb-detail", wbDetailSummaryHeaders), rows, data.totals, "#wbDetailTablePager");
    }
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить детализацию: ' + escapeHtml(err.message) + "</div>";
  }
}

function wbDetailExportUrl() {
  const rawEl = document.getElementById("wbDetailRaw");
  const likeEl = document.getElementById("wbDetailLike");
  const f = filters();
  const raw = rawEl ? rawEl.checked : false;
  const q = likeEl ? likeEl.value.trim() : "";
  const path = raw ? "/api/export/wb/detail-rows" : "/api/export/wb/detail-summary";
  const p = qs({
    date_from: f.date_from || undefined,
    date_to: f.date_to || undefined,
    article_like: q || undefined,
  });
  const cp = colViewParam("wb-detail");
  return path + p + (cp ? (p ? "&" : "?") + cp : "");
}

async function downloadWbDetailExcel() {
  const msg = document.querySelector("#wbMsg-detail-table");
  msg.textContent = "Формирую Excel…";
  try {
    const resp = await fetch(wbDetailExportUrl());
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
    msg.textContent = "Excel выгружен" + (count != null ? " · строк: " + fmt(count) : "");
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

async function uploadWbDetailToDisk() {
  const msg = document.querySelector("#wbMsg-detail-table");
  msg.textContent = "Формирую файл…";
  try {
    const resp = await fetch(wbDetailExportUrl());
    if (!resp.ok) throw new Error(resp.status + " " + (await resp.text()));
    const blob = await resp.blob();
    const now = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    const stamp = now.getFullYear() + pad(now.getMonth() + 1) + pad(now.getDate()) + "-" + pad(now.getHours()) + pad(now.getMinutes());
    const f = filters();
    const rawEl = document.getElementById("wbDetailRaw");
    const kind = rawEl && rawEl.checked ? "rows" : "summary";
    const name = "wb_detail_" + kind + "_" + (f.date_from || "na") + "_" + (f.date_to || "na") + "_" + stamp + ".xlsx";
    const fd = new FormData();
    fd.append("file", blob, name);
    msg.textContent = "Загружаю на Яндекс.Диск (" + name + ")…";
    const up = await fetch("/api/yandex/upload", { method: "POST", body: fd });
    const j = await up.json();
    if (!up.ok) throw new Error(j.detail || up.status);
    msg.textContent = "На Яндекс.Диске: /agent_market/" + j.name;
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

const WB_VIEW_MSG = {
  "wb-cards": "#wbMsg-cards-table",
  "wb-stock": "#wbMsg-stock-table",
  "wb-funnel": "#wbMsg-funnel-table",
  "wb-sales": "#wbMsg-sales-table",
  "wb-prices": "#wbMsg-prices-table",
  "wb-storage": "#wbMsg-storage-table",
  "wb-detail": "#wbMsg-detail-table",
  "oz-detail": "#ozMsg-detail-table",
  "oz-cards": "#ozMsg-cards-table",
  "oz-stock": "#ozMsg-stock-table",
  "oz-prices": "#ozMsg-prices-table",
  "oz-realization": "#ozMsg-realization-table",
};

async function renderOzDetail() {
  const box = document.getElementById("ozDetailTable");
  const likeEl = document.getElementById("ozDetailLike");
  const rawEl = document.getElementById("ozDetailRaw");
  const f = filters();
  const q = likeEl ? likeEl.value.trim() : "";
  const raw = rawEl ? rawEl.checked : false;
  const msg = document.querySelector("#ozMsg-detail-table");
  const tipEl = document.getElementById("ozDetailTip");
  try {
    if (raw) {
      const data = await api("/ozon/detail-rows" + qs({
        date_from: f.date_from || undefined,
        date_to: f.date_to || undefined,
        article_like: q || undefined,
        limit: 500,
      }));
      const rows = data.rows || [];
      tipEl.classList.add("hidden");
      if (msg) msg.textContent = "Строк в базе: " + fmt(data.total || 0) +
        (rows.length < (data.total || 0) ? " (показаны первые " + fmt(rows.length) + " — меняйте период или поиск)" : "");
      pagedTable(box, colViewHeaders("oz-detail", ozDetailRowHeaders), rows, null, "#ozDetailTablePager");
    } else {
      const data = await api("/ozon/detail-summary" + qs({
        date_from: f.date_from || undefined,
        date_to: f.date_to || undefined,
        article_like: q || undefined,
      }));
      const rows = data.rows || [];
      tipEl.classList.remove("hidden");
      if (msg) msg.textContent = "По артикулам: " + fmt(data.count || 0);
      pagedTable(box, colViewHeaders("oz-detail", ozDetailSummaryHeaders), rows, data.totals, "#ozDetailTablePager");
    }
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить детализацию Ozon: ' + escapeHtml(err.message) + "</div>";
  }
}

function ozDetailExportUrl() {
  const rawEl = document.getElementById("ozDetailRaw");
  const likeEl = document.getElementById("ozDetailLike");
  const f = filters();
  const raw = rawEl ? rawEl.checked : false;
  const q = likeEl ? likeEl.value.trim() : "";
  const path = raw ? "/api/export/ozon/detail-rows" : "/api/export/ozon/detail-summary";
  const p = qs({
    date_from: f.date_from || undefined,
    date_to: f.date_to || undefined,
    article_like: q || undefined,
  });
  const cp = colViewParam("oz-detail");
  return path + p + (cp ? (p ? "&" : "?") + cp : "");
}

async function downloadOzDetailExcel() {
  const msg = document.querySelector("#ozMsg-detail-table");
  msg.textContent = "Формирую Excel…";
  try {
    const resp = await fetch(ozDetailExportUrl());
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
    msg.textContent = "Excel выгружен" + (count != null ? " · строк: " + fmt(count) : "");
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

async function uploadOzDetailToDisk() {
  const msg = document.querySelector("#ozMsg-detail-table");
  msg.textContent = "Формирую файл…";
  try {
    const resp = await fetch(ozDetailExportUrl());
    if (!resp.ok) throw new Error(resp.status + " " + (await resp.text()));
    const blob = await resp.blob();
    const now = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    const stamp = now.getFullYear() + pad(now.getMonth() + 1) + pad(now.getDate()) + "-" + pad(now.getHours()) + pad(now.getMinutes());
    const f = filters();
    const rawEl = document.getElementById("ozDetailRaw");
    const kind = rawEl && rawEl.checked ? "rows" : "summary";
    const name = "oz_detail_" + kind + "_" + (f.date_from || "na") + "_" + (f.date_to || "na") + "_" + stamp + ".xlsx";
    const fd = new FormData();
    fd.append("file", blob, name);
    msg.textContent = "Загружаю на Яндекс.Диск (" + name + ")…";
    const up = await fetch("/api/yandex/upload", { method: "POST", body: fd });
    const j = await up.json();
    if (!up.ok) throw new Error(j.detail || up.status);
    msg.textContent = "На Яндекс.Диске: /agent_market/" + j.name;
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

function wbLikeVal(id) {
  const el = document.getElementById(id);
  return el ? el.value.trim() : "";
}

function wbViewExportUrl() {
  const f = filters();
  const p = paneDates();
  switch (currentTab) {
    case "wb-cards":
      return "/api/export/wb/cards" + qs({ marketplace: "wb", like: wbLikeVal("wbCardsLike") || undefined })
        + (colViewParam("wb-cards") ? "&" + colViewParam("wb-cards") : "");
    case "wb-stock": {
      const agg = document.getElementById("wbStockAgg");
      const q = qs({ marketplace: "wb", by_size: agg && agg.checked ? 0 : 1 });
      const cp = colViewParam("wb-stock");
      return "/api/export/wb/stock" + q + (cp ? "&" + cp : "");
    }
    case "wb-funnel": {
      const q = qs({
        date_from: f.date_from || undefined,
        date_to: f.date_to || undefined,
        article_like: wbLikeVal("wbFunnelLike") || undefined,
      });
      const cp = colViewParam("wb-funnel");
      return "/api/export/wb/funnel" + q + (cp ? "&" + cp : "");
    }
    case "wb-sales": {
      const q = qs({
        marketplace: "wb",
        date_from: p.date_from || undefined,
        date_to: p.date_to || undefined,
        article_like: wbLikeVal("wbSalesLike") || undefined,
      });
      const cp = colViewParam("wb-sales");
      return "/api/export/sales" + q + (cp ? "&" + cp : "");
    }
    case "wb-prices": {
      const q = qs({ article_like: wbLikeVal("wbPricesLike") || undefined });
      const cp = colViewParam("wb-prices", null, "base");
      return "/api/export/wb/prices" + q + (cp ? "&" + cp : "");
    }
    case "wb-storage": {
      const q = qs({ article_like: wbLikeVal("wbStorageLike") || undefined });
      const cp = colViewParam("wb-storage");
      return "/api/export/wb/storage" + q + (cp ? "&" + cp : "");
    }
    case "wb-detail":
      return wbDetailExportUrl();
    case "oz-cards":
      return "/api/export/wb/cards" + qs({ marketplace: "ozon", like: wbLikeVal("ozCardsLike") || undefined })
        + (colViewParam("oz-cards") ? "&" + colViewParam("oz-cards") : "");
    case "oz-stock": {
      const agg = document.getElementById("ozStockAgg");
      const q = qs({ marketplace: "ozon", by_size: agg && agg.checked ? 0 : 1 });
      const cp = colViewParam("oz-stock");
      return "/api/export/wb/stock" + q + (cp ? "&" + cp : "");
    }
    case "oz-prices": {
      const q = qs({ marketplace: "ozon", article_like: wbLikeVal("ozPricesLike") || undefined });
      const cp = colViewParam("oz-prices", null, "base");
      return "/api/export/wb/prices" + q + (cp ? "&" + cp : "");
    }
    case "oz-realization": {
      const q = qs({
        marketplace: "ozon",
        date_from: p.date_from || undefined,
        date_to: p.date_to || undefined,
        article_like: wbLikeVal("ozRealLike") || undefined,
      });
      const cp = colViewParam("oz-realization");
      return "/api/export/sales" + q + (cp ? "&" + cp : "");
    }
    default:
      return "";
  }
}

async function downloadViewExcel() {
  const msg = document.querySelector(WB_VIEW_MSG[currentTab] || "");
  const url = wbViewExportUrl();
  if (!url || !msg) return;
  msg.textContent = "Формирую Excel…";
  try {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(resp.status + " " + (await resp.text()));
    const blob = await resp.blob();
    const count = resp.headers.get("X-Count");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filenameFromDisposition(resp.headers.get("Content-Disposition")) || (currentTab + ".xlsx");
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);
    msg.textContent = "Excel выгружен" + (count != null ? " · строк: " + fmt(count) : "");
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

async function uploadViewToDisk() {
  const msg = document.querySelector(WB_VIEW_MSG[currentTab] || "");
  const url = wbViewExportUrl();
  if (!url || !msg) return;
  msg.textContent = "Формирую файл…";
  try {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(resp.status + " " + (await resp.text()));
    const blob = await resp.blob();
    const now = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    const stamp = now.getFullYear() + pad(now.getMonth() + 1) + pad(now.getDate()) + "-" + pad(now.getHours()) + pad(now.getMinutes());
    const f = filters();
    const p = paneDates();
    const mp = currentTab.startsWith("oz-") ? "oz" : "wb";
    const kind = currentTab.replace(/^(wb|oz)-/, "");
    const name = mp + "_" + kind + "_" + (p.date_from || f.date_from || "na") + "_" + (p.date_to || f.date_to || "na") + "_" + stamp + ".xlsx";
    const fd = new FormData();
    fd.append("file", blob, name);
    msg.textContent = "Загружаю на Яндекс.Диск (" + name + ")…";
    const up = await fetch("/api/yandex/upload", { method: "POST", body: fd });
    const j = await up.json();
    if (!up.ok) throw new Error(j.detail || up.status);
    msg.textContent = "На Яндекс.Диске: /agent_market/" + j.name;
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
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

async function uploadDetailFiles(files) {
  if (!files || !files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append("files", f);
  const msg = document.querySelector("#wbMsg-detail-excel");
  msg.textContent = "Загружаю " + files.length + " файл(ов)…";
  try {
    const resp = await fetch("/api/wb/detail-upload", { method: "POST", body: fd });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || resp.status);
    let m = "Детализация: строк " + fmt(data.rows || 0);
    if (data.imported != null) m += ", записано в БД: " + fmt(data.imported);
    if (data.errors && data.errors.length) m += "; с ошибками: " + fmt(data.errors.length);
    msg.textContent = m;
    const input = document.getElementById("wbDetailFile");
    if (input) input.value = "";
    pullsCache = null;
    updateLastPull("wb-detail");
    if (currentTab === "wb-detail") await renderWbDetail();
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

function initDetailUpload() {
  const file = document.getElementById("wbDetailFile");
  const drop = document.getElementById("wbDetailDrop");
  const rawEl = document.getElementById("wbDetailRaw");
  if (file) file.addEventListener("change", () => busyRun(() => uploadDetailFiles(file.files)));
  if (rawEl) rawEl.addEventListener("change", () => loadTab("wb-detail"));
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
      if (e.dataTransfer && e.dataTransfer.files) busyRun(() => uploadDetailFiles(e.dataTransfer.files));
    });
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
    if (file) file.addEventListener("change", () => busyRun(() => uploadCardFiles(file.files, name)));
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
let refreshJobs = [];   // [{api, jobId, rejected, msg, state, done}]
let refreshApis = [];

function stepText(s) {
  if (s.status === "ok") {
    return "ок · строк: " + fmt(s.rows) + (s.db_rows ? " · в базу: " + fmt(s.db_rows) : "");
  }
  return STEP_STATUS[s.status] || s.status;
}

function renderRefresh() {
  let h = "";
  let summary = "";
  for (const j of refreshJobs) {
    const lbl = MP_LABELS[j.api] || j.api;
    h += '<div class="refresh-api">' + escapeHtml(lbl) + "</div>";
    if (j.rejected) {
      h += '<div class="refresh-step failed"><span class="rs-name">Отклонено</span><span class="rs-status">' + escapeHtml(j.msg) + "</span></div>";
      summary += (summary ? "; " : "") + lbl + ": " + j.msg;
      continue;
    }
    if (!j.state) {
      h += '<div class="refresh-step running"><span class="rs-name">—</span><span class="rs-status">ожидание…</span></div>';
      continue;
    }
    for (const s of j.state.steps) {
      h += '<div class="refresh-step ' + s.status + '">' +
        '<span class="rs-name">' + escapeHtml(s.label) + "</span>" +
        '<span class="rs-status">' + escapeHtml(stepText(s)) + "</span></div>";
      if (s.error) h += '<div class="refresh-step-error">' + escapeHtml(s.error) + "</div>";
    }
    const ok = j.state.steps.filter((s) => s.status === "ok").length;
    summary += (summary ? "; " : "") + lbl + ": " + ok + "/" + j.state.steps.length;
  }
  $("#refreshList").innerHTML = h;
  if (summary) $("#refreshSummary").textContent = summary;
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

async function openRefresh(apis) {
  const modal = $("#refreshModal");
  modal.classList.remove("hidden");
  if (refreshPoll) { clearInterval(refreshPoll); refreshPoll = null; }
  refreshApis = Array.isArray(apis) ? apis : [apis];
  refreshJobs = [];
  $("#refreshTitle").textContent =
    "Обновление " + refreshApis.map((a) => MP_LABELS[a] || a).join(" + ");
  const detail = $("#refreshDetail");
  detail.checked = false;
  detail.disabled = false;
  detail.parentElement.style.display =
    (refreshApis.includes("wb") || refreshApis.includes("ozon")) ? "" : "none";
  $("#refreshStart").disabled = false;
  $("#refreshSummary").textContent = "";
  $("#refreshList").innerHTML = "";
  loadRefreshHistory();
}

async function startRefreshJob() {
  const apis = refreshApis;
  const startBtn = $("#refreshStart");
  startBtn.disabled = true;
  $("#refreshSummary").textContent = "Запуск…";
  const detail = $("#refreshDetail");
  refreshJobs = [];
  const started = [];
  for (const api of apis) {
    const params = { api };
    if (detail.checked) params.detail = 1;
    if ($("#fFrom").value) params.date_from = $("#fFrom").value;
    if ($("#fTo").value) params.date_to = $("#fTo").value;
    try {
      const resp = await fetch("/api/refresh" + qs(params), { method: "POST" });
      const data = await resp.json();
      if (!resp.ok) {
        refreshJobs.push({ api, rejected: true, msg: data.detail || resp.status });
        continue;
      }
      if (!data.job_id) {
        refreshJobs.push({ api, rejected: true, msg: data.rejected || "задание уже в очереди" });
        continue;
      }
      refreshJobs.push({ api, jobId: data.job_id, state: null });
      started.push(data.job_id);
    } catch (err) {
      refreshJobs.push({ api, rejected: true, msg: err.message });
    }
  }
  renderRefresh();
  if (!started.length) {
    startBtn.disabled = false;
    return;
  }
  detail.disabled = true;
  pollRefresh();
}

function pollRefresh() {
  if (refreshPoll) clearInterval(refreshPoll);
  refreshPoll = setInterval(async () => {
    for (const j of refreshJobs) {
      if (j.rejected || j.done) continue;
      try {
        j.state = await api("/refresh/" + j.jobId);
      } catch (err) {
        j.state = {
          status: "done",
          steps: [{ status: "failed", label: "ошибка опроса", error: err.message }],
        };
      }
      if (j.state.status === "done") j.done = true;
    }
    renderRefresh();
    if (refreshJobs.every((j) => j.rejected || j.done)) {
      clearInterval(refreshPoll);
      refreshPoll = null;
      pullsCache = null;
      updateLastPull(currentTab);
      loadRefreshHistory();
      $("#refreshStart").disabled = false;
      $("#refreshDetail").disabled = false;
    }
  }, 1500);
}

function updateCrumb(name) {
  const el = document.getElementById("crumb");
  if (!el) return;
  const link = document.querySelector('.nav-link[data-tab="' + name + '"]');
  if (!link) { el.textContent = name === "dashboard" ? "Обзор" : ""; return; }
  const dd = link.closest(".dropdown");
  let group = "";
  if (dd && dd.querySelector(".dropbtn")) {
    group = dd.querySelector(".dropbtn").textContent.replace(/\s*\u25BE\s*/g, "").trim();
  }
  el.textContent = (group ? group + " \u2192 " : "") + link.textContent.trim();
}

function syncHeaderForTab(name) {
  const mp = document.getElementById("fMarketplaceWrap");
  const upd = document.getElementById("btnUpdateWbDetail");
  const isApi = !!name && (name.startsWith("wb-") || name.startsWith("oz-") || name === "products");
  if (mp) mp.classList.toggle("hidden", isApi);
  if (upd) upd.classList.toggle("hidden", !isApi);
  const tip = document.getElementById("hintTip");
  const tipText = document.getElementById("hintTipText");
  if (tip && tipText) {
    tipText.textContent = TAB_HINTS[name] || "";
    tip.classList.toggle("hidden", !TAB_HINTS[name]);
  }
}

function openTab(name, linkEl) {
  document.querySelectorAll(".dropdown").forEach((d) => d.classList.remove("open"));
  document.querySelectorAll(".nav-link").forEach((l) => l.classList.remove("active"));
  if (linkEl) linkEl.classList.add("active");
  document.querySelectorAll(".pane").forEach((p) => p.classList.remove("active"));
  const pane = document.getElementById("tab-" + name);
  if (pane) pane.classList.add("active");
  currentTab = name;
  syncHeaderForTab(name);
  updateCrumb(name);
  loadTab(currentTab);
  updateLastPull(name);
}

const TAB_HINTS = {};
function initHelp() {
  document.querySelectorAll(".pane .hint").forEach((p) => {
    const pane = p.closest(".pane");
    if (pane) {
      const name = pane.id.replace(/^tab-/, "");
      const txt = p.textContent.replace(/\s+/g, " ").trim();
      if (txt) TAB_HINTS[name] = txt;
    }
    const parent = p.parentNode;
    p.remove();
    if (parent.classList.contains("toolbar") && !parent.textContent.trim()) parent.remove();
  });
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
  min_rating_for_raise: "Мин. рейтинг для роста, балл",
  min_conv_buyout_for_raise: "Мин. конв. выкупа для роста, %",
  max_cancel_ratio_for_raise: "Макс. доля отмен, 0..1",
  max_return_rate_for_raise: "Макс. возвраты, %",
  strong_rating: "Сильный рейтинг ≥, балл",
  strong_buyout_conv: "Сильная конв. выкупа ≥, %",
  strong_return_rate: "Сильные возвраты ≤, %",
  strong_margin_pct: "Сильная факт. маржа ≥, %",
  raise_boost_pct: "Uplift при сильных сигналах, %",
  dead_min_discount: "Мёртвые: мин. скидка, %",
  prefer_raise: "Противовес: поднимать охотнее",
  prefer_raise_bias: "Противовес: сдвиг порогов",
  use_inventory: "Остаток",
  use_sales: "Продажи (кол-во)",
  use_orders: "Заказы и конверсия",
  use_margin: "Прибыль/убыток (маржа)",
  use_replenishable: "Докупаемость",
  use_season: "Сезонность/тренд",
  use_quality: "Рейтинг и выкупы",
  use_returns: "Возвраты/отмены",
  use_min_price: "Мин. цена WB",
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
  min_rating_for_raise: "Повышение цены блокируется, если рейтинг магазина ниже этого значения (сигналы с 0 трактуются как «нет данных» и не блокируют).",
  min_conv_buyout_for_raise: "Повышение блокируется, если конверсия выкупа ниже этого %, % — низкая выкупаемость = цена на пределе.",
  max_cancel_ratio_for_raise: "Повышение блокируется, если отмены/заказы выше этой доли (0..1) — спрос «мыльный».",
  max_return_rate_for_raise: "Повышение блокируется, если возвраты из детализации выше этого %, % — брак/неликвид.",
  strong_rating: "Сигнал «качества» для смелого повышения: рейтинг ≥ этого значения.",
  strong_buyout_conv: "Сигнал «качества»: конверсия выкупа ≥ этого значения, %.",
  strong_return_rate: "Сигнал «качества»: возвраты из детализации ≤ этого значения, %.",
  strong_margin_pct: "Сигнал «качества»: фактическая маржа из детализации ≥ этого значения, %.",
  raise_boost_pct: "Если ≥2 сильных сигналов одновременно — рост цены за шаг увеличивается на эту долю, %. Умеренный uplift, потолок цен не ломает.",
  dead_min_discount: "Порог остановки деления скидки пополам у мёртвых товаров (по умолчанию 1%): скидка 50→25→12→6→3→1 дольше не делится. Цена потихоньку «выздоравливает» и возвращается к базовой.",
  prefer_raise: "Противостояние автоскидкам WB: при прочих равных поднимать цену, а не опускать. Расширяет зону «дефицит→рост» и ужесточает зону «перезапас→снижение». Включено по умолчанию.",
  prefer_raise_bias: "Насколько сдвинуть границы DOC (доля от порогов): 0 = симметричные правила, 0.15 = дефицит ≤ 16 вместо 14, перезапас ≥ 69 вместо 60, шаг снижения ~ ×0.85.",
  use_inventory: "Остаток и DOC: дефицит → повод поднять цену, перезапас → снизить. Выключено — остаток не влияет на решение.",
  use_sales: "Скорость и динамика продаж: нет продаж → снижаем «мёртвый» запас. Выключено — продажи не влияют на решение.",
  use_orders: "Воронка (заказы, просмотры, корзины): «горячий спрос» и низкая конверсия корзин. Выключено — воронка не влияет на решение.",
  use_margin: "Выручка и маржа: порог безубыточности (пол цены). Выключено — нехватка данных о доходах не блокирует решение, но цена может уйти в убыток.",
  use_replenishable: "Пополняемый ли товар: докупаемые поднимаем умеренно, последние единицы — смелее. Выключено — различие игнорируется.",
  use_season: "Сезонность/тренд продаж: экстраполяция скорости и корректировка глубины снижения. Выключено — тренд не влияет.",
  use_quality: "Рейтинг магазина, конверсия выкупа, отмены, возвраты из детализации. Выключено — повышение не блокируется качеством (по умолчанию выкл.).",
  use_returns: "Защита от «мыльного» спроса: высокая доля возвратов/отмен → товар не трогаем. Выключено — защита не действует (по умолчанию выкл.).",
  use_min_price: "Клампинг рекомендаций к минимальной витринной цене WB. Выключено — автопилот может предлагать цену ниже минимума, WB API её не пропустит (по умолчанию выкл.).",
};

const PRICING_ACTION = {
  RAISE: { txt: "поднять цену", cls: "p-raise" },
  LOWER: { txt: "снизить цену", cls: "p-lower" },
  HALVE: { txt: "скидка ÷2", cls: "p-halve" },
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
  { k: "product_rating", label: "Рейтинг", num: true, render: (v) => v == null ? "—" : Number(v).toFixed(1) },
  { k: "buyouts", label: "Выкупы, шт", num: true, render: cellFmts.int },
  { k: "conv_buyout_percent", label: "Конв. выкупа, %", num: true, render: cellFmts.pct },
  { k: "cancel_sum", label: "Отмены, руб", num: true, render: cellFmts.money },
  { k: "add_to_wishlist", label: "В избранное", num: true, render: cellFmts.int },
  { k: "stock_wb", label: "Остаток WB", num: true, render: cellFmts.int },
  { k: "return_rate", label: "Возвраты, %", num: true, render: cellFmts.pct },
  { k: "margin_pct", label: "Маржа факт, %", num: true, render: cellFmts.pct },
  { k: "margin_per_one", label: "Маржа/шт, руб", num: true, render: cellFmts.money },
  { k: "revenue_per_one", label: "Ср. чек, руб", num: true, render: cellFmts.money },
  { k: "income_per_one", label: "К переч./шт, руб", num: true, render: cellFmts.money },
  { k: "commission_per_one", label: "Комиссия/шт, руб", num: true, render: cellFmts.money },
  { k: "logistics_per_one", label: "Логистика/шт, руб", num: true, render: cellFmts.money },
  { k: "storage_per_one", label: "Хранение/шт, руб", num: true, render: cellFmts.money },
  { k: "min_price", label: "Мин. цена WB, руб", num: true, render: cellFmts.money },
  { k: "action", label: "Решение", render: actionCell },
  { k: "target_discount", label: "Целевая скидка, %", num: true, render: (v) => v == null ? "—" : fmt(v) + "%" },
  { k: "target_vis", label: "Целевая цена", num: true, render: cellFmts.money },
  { k: "margin_pct_at_target", label: "Маржа при цели, %", num: true, render: cellFmts.pct },
  { k: "reason", label: "Причина", render: cellFmts.text },
];

// ----------------------------------------------------- Регистрация «Вида таблицы» по разделам
const mkOpt = (list) => list.map((h) => ({ k: h.k, label: h.label, def: true }));
registerColView("wb-detail", {
  storageKey: "wbDetailCols",
  mode: () => { const rawEl = document.getElementById("wbDetailRaw"); return rawEl && rawEl.checked ? "rows" : "summary"; },
  sets: {
    rows: { headers: wbDetailRowHeaders, optional: mkOpt(wbDetailRowHeaders) },
    summary: { headers: wbDetailSummaryHeaders, optional: mkOpt(wbDetailSummaryHeaders) },
  },
});
registerColView("oz-detail", {
  storageKey: "ozDetailCols",
  mode: () => { const rawEl = document.getElementById("ozDetailRaw"); return rawEl && rawEl.checked ? "rows" : "summary"; },
  sets: {
    rows: { headers: ozDetailRowHeaders, optional: mkOpt(ozDetailRowHeaders) },
    summary: { headers: ozDetailSummaryHeaders, optional: mkOpt(ozDetailSummaryHeaders) },
  },
});
registerColView("wb-cards", { storageKey: "wbCardsCols", headers: cardsHeaders, optional: mkOpt(cardsHeaders) });
registerColView("wb-stock", {
  storageKey: "wbStockCols",
  mode: () => { const agg = document.getElementById("wbStockAgg"); return agg && agg.checked ? "agg" : "base"; },
  sets: {
    base: { headers: wbStockHeaders, optional: mkOpt(wbStockHeaders) },
    agg: { headers: wbStockAggHeaders, optional: mkOpt(wbStockAggHeaders) },
  },
});
registerColView("wb-funnel", {
  storageKey: "wbFunnelCols",
  mode: () => { const exp = document.getElementById("wbFunnelExpanded"); return exp && exp.checked ? "expanded" : "compact"; },
  sets: {
    compact: { headers: wbFunnelCompact, optional: mkOpt(wbFunnelCompact) },
    expanded: { headers: wbFunnelHeaders, optional: mkOpt(wbFunnelHeaders) },
  },
});
registerColView("wb-sales", { storageKey: "wbSalesCols", headers: salesHeaders, optional: mkOpt(salesHeaders) });
registerColView("wb-prices", {
  storageKey: "wbPricesCols",
  mode: () => { const agg = document.getElementById("wbPriceAgg"); return agg && agg.checked ? "agg" : "base"; },
  sets: {
    base: { headers: wbPricesHeaders, optional: mkOpt(wbPricesHeaders) },
    agg: { headers: wbPricesAggHeaders, optional: mkOpt(wbPricesAggHeaders) },
  },
});
registerColView("wb-storage", { storageKey: "wbStorageCols", headers: wbStorageHeaders, optional: mkOpt(wbStorageHeaders) });
registerColView("oz-cards", { storageKey: "ozCardsCols", headers: cardsHeaders, optional: mkOpt(cardsHeaders) });
registerColView("oz-stock", {
  storageKey: "ozStockCols",
  mode: () => { const agg = document.getElementById("ozStockAgg"); return agg && agg.checked ? "agg" : "base"; },
  sets: {
    base: { headers: wbStockHeaders, optional: mkOpt(wbStockHeaders) },
    agg: { headers: wbStockAggHeaders, optional: mkOpt(wbStockAggHeaders) },
  },
});
registerColView("oz-prices", {
  storageKey: "ozPricesCols",
  mode: () => { const agg = document.getElementById("ozPriceAgg"); return agg && agg.checked ? "agg" : "base"; },
  sets: {
    base: { headers: wbPricesHeaders, optional: mkOpt(wbPricesHeaders) },
    agg: { headers: wbPricesAggHeaders, optional: mkOpt(wbPricesAggHeaders) },
  },
});
registerColView("oz-realization", { storageKey: "ozRealCols", headers: salesHeaders, optional: mkOpt(salesHeaders) });
registerColView("products", {
  storageKey: "productsCols",
  mode: () => { const s = document.getElementById("productsSizes"); return s && s.checked ? "sizes" : "agg"; },
  sets: {
    agg: { headers: productsBaseHeaders, optional: mkOpt(productsBaseHeaders) },
    sizes: { headers: productsSizeHeaders, optional: mkOpt(productsSizeHeaders) },
  },
});
registerColView("margin", { storageKey: "marginCols", headers: marginTableHeaders, optional: mkOpt(marginTableHeaders) });
registerColView("margin-funnel", { storageKey: "marginFunnelCols", headers: funnelHeaders, optional: mkOpt(funnelHeaders) });
registerColView("margin-detail", {
  storageKey: "marginDetailCols",
  headers: marginHeaders,
  optional: MARGIN_DETAIL_OPTIONAL.map((c) => ({ k: c.k, label: c.label, def: _OLD_OPTIONAL.has(c.k) })),
});
// Необязательные колонки автопилота (базовые — article/name/stock/doc/action/target_discount/target_vis/reason — видны всегда).
const PRICING_OPTIONAL = [
  { k: "velocity", label: "v, шт/дн", def: true },
  { k: "trend", label: "Тренд", def: true },
  { k: "conv_pct", label: "Конверсия, %", def: true },
  { k: "backlog", label: "В корзине", def: true },
  { k: "current_discount", label: "Скидка сейчас, %", def: true },
  { k: "avg_price", label: "Ср. цена факт", def: true },
  { k: "margin_pct_at_target", label: "Маржа при цели, %", def: true },
  { k: "product_rating", label: "Рейтинг", def: true },
  { k: "return_rate", label: "Возвраты, %", def: true },
  { k: "margin_pct", label: "Маржа факт, %", def: true },
  { k: "revenue_per_one", label: "Ср. чек, руб", def: false },
  { k: "margin_per_one", label: "Маржа/шт, руб", def: false },
  { k: "income_per_one", label: "К переч./шт, руб", def: false },
  { k: "commission_per_one", label: "Комиссия/шт, руб", def: false },
  { k: "logistics_per_one", label: "Логистика/шт, руб", def: false },
  { k: "storage_per_one", label: "Хранение/шт, руб", def: false },
  { k: "buyouts", label: "Выкупы, шт", def: false },
  { k: "conv_buyout_percent", label: "Конв. выкупа, %", def: false },
  { k: "cancel_sum", label: "Отмены, руб", def: false },
  { k: "add_to_wishlist", label: "В избранное", def: false },
  { k: "stock_wb", label: "Остаток WB", def: false },
  { k: "min_price", label: "Мин. цена WB, руб", def: true },
];
registerColView("pricing", { storageKey: "pricingCols", headers: pricingHeaders, optional: PRICING_OPTIONAL });

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
  const enumKeys = Object.keys(pricingDefaults);
  // Панель «какие параметры влияют на цену» — в начале настроек;
  // выключенный фактор не участвует в правилах.
  const factorKeys = enumKeys.filter((k) => k.startsWith("use_"));
  if (factorKeys.length) {
    const panel = document.createElement("div");
    panel.className = "p-influence";
    const title = document.createElement("div");
    title.className = "p-influence-title";
    title.textContent = "Какие параметры влияют на цену:";
    panel.appendChild(title);
    const wrap = document.createElement("span");
    wrap.className = "p-influence-wrap";
    for (const key of factorKeys) {
      const cur = saved[key] !== undefined ? saved[key] : pricingDefaults[key];
      const lbl = document.createElement("label");
      lbl.className = "p-inf";
      const input = document.createElement("input");
      input.dataset.key = key;
      input.type = "checkbox";
      input.checked = !!cur;
      lbl.appendChild(input);
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
      wrap.appendChild(lbl);
    }
    panel.appendChild(wrap);
    box.appendChild(panel);
  }
  for (const key of enumKeys) {
    if (key === "show_zero") continue; // чекбокс в шапке вкладки, не в панели
    if (key.startsWith("use_")) continue; // они — в панели факторов
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
    if (typeof cur === "boolean") {
      input.type = "checkbox";
      input.checked = !!cur;
    } else {
      input.type = "number";
      input.step = (key === "return_penalty" || key === "season_damp" || key === "prefer_raise_bias") ? "0.1" : "1";
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
    box.innerHTML = '<div class="empty">Журнал пуст — решения записываются здесь при применении скидок через WB API</div>';
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

function pricingWithDates(s) {
  const f = filters();
  if (f.date_from) s.date_from = f.date_from;
  if (f.date_to) s.date_to = f.date_to;
  return s;
}

let _pricingRows = [];
let _pricingResp = null;

function refreshApplyButton() {
  const btn = $("#pricingApply");
  if (!btn) return;
  const actives = _pricingRows.filter((r) => r.action === "RAISE" || r.action === "LOWER" || r.action === "HALVE");
  const lowers = _pricingRows.filter((r) => r.action === "LOWER");
  const noMin = lowers.filter((r) => !(Number(r.min_price) > 0));
  const hiddenDead = _pricingResp && Number(_pricingResp.hidden_dead || 0) > 0;
  const canApply = (actives.length > 0 || hiddenDead) && noMin.length === 0;
  btn.disabled = !canApply;
  btn.title = canApply
    ? "Применить рекомендованные скидки через WB API" + (hiddenDead ? " (включая деление скидки у скрытых мёртвых)" : " (мин. цена WB учтена)")
    : noMin.length > 0
      ? "Нужен расчёт минимальной цены WB для всех рекомендаций на снижение — публичный Price API не вернул данных"
      : "Нет рекомендаций на изменение — включите «С нулевыми товарами», чтобы применить и деление скидки у мёртвых";
}

async function renderPricing(apply) {
  await buildPricingSettings();
  const s = pricingWithDates(collectPricingSettings());
  const showZeroEl = $("#pricingShowZero");
  if (showZeroEl) s.show_zero = showZeroEl.checked;
  const msg = $("#pricingMsg");
  const likeEl = $("#pricingLike") || { value: "" };
  const q = likeEl.value.trim().toLowerCase();
  msg.textContent = "Считаю рекомендации…";
  try {
    const data = await apiPost("/pricing/recommendations", s);
    _pricingResp = data;
    const allRows = data.rows || [];
    const rows = allRows.filter((r) =>
      !q || (String(r.article || "") + " " + (r.name || "")).toLowerCase().includes(q)
    );
    _pricingRows = rows;
    const actionable = rows.filter((r) => r.action === "RAISE" || r.action === "LOWER" || r.action === "HALVE").length;
    const underCooldown = rows.filter((r) => r.status === "skipped_cooldown").length;
    let summary = "Товаров: " + fmt(rows.length);
    if (allRows.length !== rows.length) summary += " из " + fmt(allRows.length);
    summary += ", решений: " + fmt(actionable);
    const hiddenDead = Number(data.hidden_dead || 0);
    if (hiddenDead > 0) summary += ", скрыто нулевых: " + fmt(hiddenDead) + " (вкл. «С нулевыми товарами»)";
    const nonWb = Number(data.non_wb || 0);
    if (nonWb > 0) summary += ", артикулов не из WB-карточек: " + fmt(nonWb) + " (исключены)";
    if (underCooldown) summary += ", в кулдауне: " + fmt(underCooldown);
    if (data.date_from && data.date_to) summary += " · окно " + data.date_from + ".." + data.date_to;
    if (data.as_of) summary += " · на " + data.as_of;
    $("#pricingSummary").textContent = summary;
    msg.textContent = data.note || "";
    const footers = {
      stock: rows.reduce((a, r) => a + (Number(r.stock) || 0), 0),
      target_vis: rows.reduce((a, r) => a + (Number(r.target_vis) || 0), 0),
    };
    pagedTable($("#pricingTable"), colViewHeaders("pricing", pricingHeaders), rows, footers);
    await renderPricingHistory();
    refreshApplyButton();
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

async function applyPricing() {
  const msg = $("#pricingMsg");
  await buildPricingSettings();
  const s = pricingWithDates(collectPricingSettings());
  const showZeroEl = $("#pricingShowZero");
  if (showZeroEl) s.show_zero = showZeroEl.checked;
  const lowers = _pricingRows.filter((r) => r.action === "LOWER");
  const halve = _pricingRows.filter((r) => r.action === "HALVE");
  const hiddenDead = _pricingResp ? Number(_pricingResp.hidden_dead || 0) : 0;
  const parts = [];
  if (lowers.length) {
    const list = lowers.slice(0, 5).map((r) => r.article).join(", ") + (lowers.length > 5 ? "…" : "");
    parts.push("снизить цену у " + lowers.length + " артикулов (" + list + ")");
  }
  if (halve.length) parts.push("разделить скидку пополам у " + halve.length + " мёртвых");
  if (hiddenDead > 0) parts.push("разделить скидку пополам у " + hiddenDead + " скрытых мёртвых (скидка ÷2)");
  const confirmText = parts.length
    ? "Применить?\n• " + parts.join("\n• ") + "\nМинимальная цена WB учтена для снижений."
    : "Рекомендаций на изменение нет — применить всё равно?";
  if (!confirm(confirmText)) return;
  msg.textContent = "Применяю через WB API…";
  try {
    const resp = await fetch("/api/pricing/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(s),
    });
    const j = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error((j.detail || resp.status) || "WB API не принял изменения");
    msg.textContent = j.note || "Применено: " + (j.applied || []).length + " артикулов";
    await renderPricing(false);
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

async function exportPricing() {
  await buildPricingSettings();
  const s = pricingWithDates(collectPricingSettings());
  const showZeroEl = $("#pricingShowZero");
  if (showZeroEl) s.show_zero = showZeroEl.checked;
  const cp = colViewParam("pricing").replace(/^cols=/, "");
  if (cp) s.cols = cp;
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

function yandexStamp() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return now.getFullYear() + pad(now.getMonth() + 1) + pad(now.getDate()) + "-" + pad(now.getHours()) + pad(now.getMinutes());
}
async function uploadBlobToYandex(blob, name, msg) {
  const fd = new FormData();
  fd.append("file", blob, name);
  msg.textContent = "Загружаю на Яндекс.Диск (" + name + ")…";
  const up = await fetch("/api/yandex/upload", { method: "POST", body: fd });
  const j = await up.json();
  if (!up.ok) throw new Error(j.detail || up.status);
  msg.textContent = "На Яндекс.Диске: /agent_market/" + j.name;
}

async function uploadMarginToDisk() {
  const msg = $("#marginMsg");
  const url = $("#exportMargin") ? $("#exportMargin").href : "";
  if (!url || !msg) return;
  msg.textContent = "Формирую файл…";
  try {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(resp.status + " " + (await resp.text()));
    const blob = await resp.blob();
    const f = filters();
    const name = "margin_" + (f.date_from || "na") + "_" + (f.date_to || "na") + "_" + yandexStamp() + ".xlsx";
    await uploadBlobToYandex(blob, name, msg);
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

async function uploadMarginFunnelToDisk() {
  const msg = $("#marginFunnelMsg");
  const url = $("#exportMarginFunnel") ? $("#exportMarginFunnel").href : "";
  if (!url || !msg) return;
  msg.textContent = "Формирую файл…";
  try {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(resp.status + " " + (await resp.text()));
    const blob = await resp.blob();
    const f = filters();
    const name = "margin_funnel_" + (f.date_from || "na") + "_" + (f.date_to || "na") + "_" + yandexStamp() + ".xlsx";
    await uploadBlobToYandex(blob, name, msg);
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

async function uploadMarginDetailToDisk() {
  const msg = $("#marginDetailMsg");
  const url = $("#exportMarginDetail") ? $("#exportMarginDetail").dataset.url : "";
  if (!url || !msg) return;
  msg.textContent = "Формирую файл…";
  try {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(resp.status + " " + (await resp.text()));
    const blob = await resp.blob();
    const f = filters();
    const name = "margin_detail_" + (f.date_from || "na") + "_" + (f.date_to || "na") + "_" + yandexStamp() + ".xlsx";
    await uploadBlobToYandex(blob, name, msg);
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

async function uploadPricingToDisk() {
  await buildPricingSettings();
  const s = pricingWithDates(collectPricingSettings());
  const showZeroEl = $("#pricingShowZero");
  if (showZeroEl) s.show_zero = showZeroEl.checked;
  const cp = colViewParam("pricing").replace(/^cols=/, "");
  if (cp) s.cols = cp;
  const msg = $("#pricingMsg");
  msg.textContent = "Формирую файл…";
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
    const name = "pricing_" + yandexStamp() + ".xlsx";
    await uploadBlobToYandex(blob, name, msg);
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

document.addEventListener("DOMContentLoaded", () => {
  initDates();
  initWriteDb();
  initHelp();
  initYandexTab();
  updateCrumb("dashboard");
  document.querySelectorAll('input[data-date="from"]').forEach((i) => (i.value = $("#fFrom").value));
  document.querySelectorAll('input[data-date="to"]').forEach((i) => (i.value = $("#fTo").value));

  const menuBtn = $("#menuBtn");
  const navLinks = $("#navLinks");
  const closeNav = () => { if (navLinks) navLinks.classList.remove("open"); };
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
      if (!link.dataset.tab) return;
      if (navLinks) navLinks.classList.remove("open");
      openTab(link.dataset.tab, link);
    });
  });
  document.querySelectorAll(".wb-dl").forEach((btn) => {
    const api = btn.dataset.api || "wb";
    const tag = api === "ozon" ? "oz" : "wb";
    const kind = btn.dataset.kind;
    btn.addEventListener("click", () => busyRun(() => apiDownload(api, kind, "#" + tag + "Msg-" + kind)));
    const yd = document.createElement("button");
    yd.type = "button";
    yd.className = "btn";
    yd.textContent = "Загрузить на диск";
    yd.title = "Заливает свежий Excel-отчёт в папку /agent_market на Яндекс.Диске";
    yd.addEventListener("click", () => busyRun(() => apiUploadDisk(api, kind, "#" + tag + "Msg-" + kind)));
    btn.parentNode.insertBefore(yd, btn.nextSibling);
    if (api !== "wb") return;
    const upd = document.createElement("button");
    upd.type = "button";
    upd.className = "btn";
    upd.textContent = "Обновить базу";
    upd.title = "Тянет данные из WB API и пишет в БД, файл не скачивается";
    upd.addEventListener("click", () => busyRun(() => apiDownload(api, kind, "#" + tag + "Msg-" + kind, true)));
    btn.parentNode.insertBefore(upd, yd.nextSibling);
    btn.title = "Тянет данные из WB API → пишет в БД (если «в БД») → скачивает Excel";
  });
  const btnExportWbDetail = document.getElementById("btnExportWbDetail");
  if (btnExportWbDetail) btnExportWbDetail.addEventListener("click", () => busyRun(downloadWbDetailExcel));
  const btnDiskWbDetail = document.getElementById("btnDiskWbDetail");
  if (btnDiskWbDetail) btnDiskWbDetail.addEventListener("click", () => busyRun(uploadWbDetailToDisk));
  const btnExportOzDetail = document.getElementById("btnExportOzDetail");
  if (btnExportOzDetail) btnExportOzDetail.addEventListener("click", () => busyRun(downloadOzDetailExcel));
  const btnDiskOzDetail = document.getElementById("btnDiskOzDetail");
  if (btnDiskOzDetail) btnDiskOzDetail.addEventListener("click", () => busyRun(uploadOzDetailToDisk));
  const ozDetailRawEl = document.getElementById("ozDetailRaw");
  if (ozDetailRawEl) ozDetailRawEl.addEventListener("change", () => loadTab("oz-detail"));
  [["wb-cards", "Cards"], ["wb-stock", "Stock"], ["wb-funnel", "Funnel"], ["wb-sales", "Sales"], ["wb-prices", "Prices"], ["wb-storage", "Storage"]].forEach(([tab, pfx]) => {
    const ex = document.getElementById("btnExportWb" + pfx);
    if (ex) ex.addEventListener("click", () => busyRun(() => { currentTab = tab; return downloadViewExcel(); }));
    const ds = document.getElementById("btnDiskWb" + pfx);
    if (ds) ds.addEventListener("click", () => busyRun(() => { currentTab = tab; return uploadViewToDisk(); }));
  });
  [["oz-cards", "OzCards"], ["oz-stock", "OzStock"], ["oz-prices", "OzPrices"], ["oz-realization", "OzReal"]].forEach(([tab, pfx]) => {
    const ex = document.getElementById("btnExport" + pfx);
    if (ex) ex.addEventListener("click", () => busyRun(() => { currentTab = tab; return downloadViewExcel(); }));
    const ds = document.getElementById("btnDisk" + pfx);
    if (ds) ds.addEventListener("click", () => busyRun(() => { currentTab = tab; return uploadViewToDisk(); }));
  });

  const apiPullByTab = {
    "wb-cards": ["wb", "cards", "#wbMsg-cards-table"],
    "wb-stock": ["wb", "stock", "#wbMsg-stock-table"],
    "wb-funnel": ["wb", "funnel", "#wbMsg-funnel-table"],
    "wb-sales": ["wb", "sales", "#wbMsg-sales-table"],
    "wb-prices": ["wb", "prices", "#wbMsg-prices-table"],
    "wb-storage": ["wb", "storage", "#wbMsg-storage-table"],
    "wb-detail": ["wb", "detail", "#wbMsg-detail-table"],
    "oz-cards": ["ozon", "cards", "#ozMsg-cards-table"],
    "oz-stock": ["ozon", "stock", "#ozMsg-stock-table"],
    "oz-prices": ["ozon", "prices", "#ozMsg-prices-table"],
    "oz-realization": ["ozon", "realization", "#ozMsg-realization-table"],
    "oz-detail": ["ozon", "detail", "#ozMsg-detail-table"],
    "oz-cashflow": ["ozon", "cashflow", "#ozMsg-cashflow"],
    "products": ["products", "refresh", "#productsMsg"],
  };
  const btnUpdateWbDetail = document.getElementById("btnUpdateWbDetail");
  if (btnUpdateWbDetail) btnUpdateWbDetail.addEventListener("click", () => busyRun(() => {
    const spec = apiPullByTab[currentTab];
    if (!spec) return Promise.resolve();
    return apiDownload(spec[0], spec[1], spec[2], true);
  }));
  const funnelExp = document.getElementById("wbFunnelExpanded");
  if (funnelExp) funnelExp.addEventListener("change", () => loadTab("wb-funnel"));
  $("#btnApply").addEventListener("click", () => loadTab(currentTab));
  $("#magicRefreshWb").addEventListener("click", (e) => { e.preventDefault(); closeNav(); openRefresh("wb"); });
  $("#magicRefreshOz").addEventListener("click", (e) => { e.preventDefault(); closeNav(); openRefresh("ozon"); });
  $("#magicRefreshAll").addEventListener("click", (e) => { e.preventDefault(); closeNav(); openRefresh(["wb", "ozon"]); });
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
  const marginDetailCompare = $("#marginDetailCompare");
  if (marginDetailCompare) {
    marginDetailCompare.addEventListener("change", () => {
      if (currentTab === "margin-detail") loadTab(currentTab);
    });
  }
  const funnelLike = $("#wbFunnelLike");
  if (funnelLike) {
    let timer;
    funnelLike.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => { if (currentTab === "wb-funnel") loadTab(currentTab); }, 400);
    });
  }
  const wbStockLike = $("#wbStockLike");
  if (wbStockLike) {
    let timer;
    wbStockLike.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => { if (currentTab === "wb-stock") loadTab(currentTab); }, 400);
    });
  }
  const wbStockAgg = $("#wbStockAgg");
  if (wbStockAgg) {
    wbStockAgg.addEventListener("change", () => { if (currentTab === "wb-stock") loadTab(currentTab); });
  }
  const wbPriceAgg = $("#wbPriceAgg");
  if (wbPriceAgg) {
    wbPriceAgg.addEventListener("change", () => { if (currentTab === "wb-prices") loadTab(currentTab); });
  }
  [["wbPricesLike", "wb-prices"], ["wbStorageLike", "wb-storage"]].forEach(([id, tab]) => {
    const el = $("#" + id);
    if (!el) return;
    let timer;
    el.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => { if (currentTab === tab) loadTab(currentTab); }, 400);
    });
  });
  [["wbSalesLike", "wb-sales"], ["wbDetailLike", "wb-detail"]].forEach(([id, tab]) => {
    const el = $("#" + id);
    if (!el) return;
    let timer;
    el.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => { if (currentTab === tab) loadTab(currentTab); }, 400);
    });
  });
  const ozDetailLike = $("#ozDetailLike");
  if (ozDetailLike) {
    let timer;
    ozDetailLike.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => { if (currentTab === "oz-detail") loadTab(currentTab); }, 400);
    });
  }
  [["ozStockLike", "oz-stock"], ["ozPricesLike", "oz-prices"], ["ozRealLike", "oz-realization"]].forEach(([id, tab]) => {
    const el = $("#" + id);
    if (!el) return;
    let timer;
    el.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => { if (currentTab === tab) loadTab(currentTab); }, 400);
    });
  });
  const ozStockAgg = $("#ozStockAgg");
  if (ozStockAgg) ozStockAgg.addEventListener("change", () => { if (currentTab === "oz-stock") loadTab(currentTab); });
  const ozPriceAgg = $("#ozPriceAgg");
  if (ozPriceAgg) ozPriceAgg.addEventListener("change", () => { if (currentTab === "oz-prices") loadTab(currentTab); });
  $("#oursImport").addEventListener("click", () => uploadFile("/import/custom-stock", $("#oursFile"), "#oursMsg", "ours"));
  $("#productsImport").addEventListener("click", () => uploadFile("/import/products", $("#productsFile"), "#productsMsg", "products"));
  $("#netCostImport").addEventListener("click", () => uploadFile("/import/net-cost", $("#netCostFile"), "#netCostMsg", "products"));
  const productsLikeEl = $("#productsLike");
  if (productsLikeEl) {
    let productsLikeTimer;
    productsLikeEl.addEventListener("input", () => {
      clearTimeout(productsLikeTimer);
      productsLikeTimer = setTimeout(() => { if (currentTab === "products") loadTab(currentTab); }, 400);
    });
  }
  const productsSizesEl = $("#productsSizes");
  if (productsSizesEl) productsSizesEl.addEventListener("change", () => { if (currentTab === "products") loadTab(currentTab); });
  const productsStocksEl = $("#productsStocks");
  if (productsStocksEl) productsStocksEl.addEventListener("change", () => { if (currentTab === "products") loadTab(currentTab); });
  const productsOverwriteEl = $("#productsOverwrite");
  if (productsOverwriteEl) {
    productsOverwriteEl.addEventListener("change", () => {
      const msg = $("#productsMsg");
      if (msg) msg.textContent = "Перезапись: " + (productsOverwriteEl.checked ? "включена" : "выключена");
    });
  }
  const exportProductsBtn = $("#btnExportProducts");
  if (exportProductsBtn) exportProductsBtn.addEventListener("click", () => busyRun(downloadProductsExcel));
  const diskProductsBtn = $("#btnDiskProducts");
  if (diskProductsBtn) diskProductsBtn.addEventListener("click", () => busyRun(uploadProductsToDisk));
  const priceProductsBtn = $("#btnPriceProducts");
  if (priceProductsBtn) {
    priceProductsBtn.addEventListener("click", () => {
      const panel = $("#productsPricePanel");
      if (!panel) return;
      if (panel.classList.contains("hidden")) {
        busyRun(buildProductsPricePanel);
        panel.classList.remove("hidden");
      } else {
        panel.classList.add("hidden");
      }
    });
  }
  const productsPreviewBtn = $("#productsPreview");
  if (productsPreviewBtn) productsPreviewBtn.addEventListener("click", () => busyRun(previewProductsPrices));
  const productsPriceClose = $("#productsPriceClose");
  if (productsPriceClose) {
    productsPriceClose.addEventListener("click", () => {
      const panel = $("#productsPricePanel");
      if (panel) panel.classList.add("hidden");
    });
  }
  // ── «Наш склад» — импорт/экспорт/диск ──
  $("#whCpImport").addEventListener("click", () => uploadFile("/warehouse/import/counterparties", $("#whCpFile"), "#whCpMsg", "wh-cp"));
  $("#whRImport").addEventListener("click", () => uploadFile("/warehouse/import/docs?type=receipt", $("#whRFile"), "#whRMsg", "wh-receipt"));
  $("#whSImport").addEventListener("click", () => uploadFile("/warehouse/import/docs?type=shipment", $("#whSFile"), "#whSMsg", "wh-shipment"));
  wireWhDisk("#whCpToDisk", { kind: "counterparties" }, "#whCpMsg", "wh-cp", false);
  wireWhDisk("#whCpFromDisk", { type: "counterparties" }, "#whCpMsg", "wh-cp", true);
  wireWhDisk("#whRToDisk", { kind: "docs", type: "receipt" }, "#whRMsg", "wh-receipt", false);
  wireWhDisk("#whRFromDisk", { type: "receipt" }, "#whRMsg", "wh-receipt", true);
  wireWhDisk("#whSToDisk", { kind: "docs", type: "shipment" }, "#whSMsg", "wh-shipment", false);
  wireWhDisk("#whSFromDisk", { type: "shipment" }, "#whSMsg", "wh-shipment", true);
  [["whRTable", "whRDetail"], ["whSTable", "whSDetail"]].forEach(([t, d]) => {
    const box = $("#" + t);
    const detail = $("#" + d);
    if (!box || !detail) return;
    box.addEventListener("click", async (ev) => {
      const btn = ev.target.closest("[data-doc-id]");
      if (!btn) return;
      detail.innerHTML = '<div class="empty">Загружаю…</div>';
      try {
        const data = await api("/warehouse/docs/" + btn.dataset.docId + "/items");
        pagedTable(detail, whDocItemHeaders, data.rows || []);
      } catch (err) {
        detail.innerHTML = '<div class="empty">Ошибка: ' + escapeHtml(err.message) + "</div>";
      }
    });
  });
  const whStockLike = $("#whStockLike");
  if (whStockLike) {
    let timer;
    whStockLike.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => { if (currentTab === "wh-stock") loadTab(currentTab); }, 400);
    });
  }
  initCardsUpload();
  initDetailUpload();
  const pricingExport = $("#pricingExport");
  if (pricingExport) pricingExport.addEventListener("click", () => exportPricing());
  const pricingApply = $("#pricingApply");
  if (pricingApply) pricingApply.addEventListener("click", () => applyPricing());
  const pricingLike = $("#pricingLike");
  if (pricingLike) {
    let pricingLikeTimer;
    pricingLike.addEventListener("input", () => {
      clearTimeout(pricingLikeTimer);
      pricingLikeTimer = setTimeout(() => { if (currentTab === "pricing") renderPricing(false); }, 350);
    });
  }
  const pricingShowZero = $("#pricingShowZero");
  if (pricingShowZero) {
    pricingShowZero.addEventListener("change", () => {
      const ss = loadPricingSettings();
      ss.show_zero = pricingShowZero.checked;
      savePricingSettings(ss);
      if (currentTab === "pricing") renderPricing(false);
    });
  }
  syncHeaderForTab(currentTab);
  for (const t of Object.keys(_COLVIEWS)) initColViewMenu(t);
  const btnExportMarginDetail = $("#exportMarginDetail");
  if (btnExportMarginDetail) {
    btnExportMarginDetail.addEventListener("click", () => {
      const url = btnExportMarginDetail.dataset.url;
      if (url) window.open(url, "_blank");
    });
  }
  const uploadMargin = $("#uploadMargin");
  if (uploadMargin) uploadMargin.addEventListener("click", () => uploadMarginToDisk());
  const uploadMarginFunnel = $("#uploadMarginFunnel");
  if (uploadMarginFunnel) uploadMarginFunnel.addEventListener("click", () => uploadMarginFunnelToDisk());
  const uploadMarginDetail = $("#uploadMarginDetail");
  if (uploadMarginDetail) uploadMarginDetail.addEventListener("click", () => uploadMarginDetailToDisk());
  const pricingUpload = $("#pricingUpload");
  if (pricingUpload) pricingUpload.addEventListener("click", () => uploadPricingToDisk());
  initTicketsTab();
  loadTab(currentTab);
});