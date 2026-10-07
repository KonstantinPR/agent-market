"use strict";

const $ = (s) => document.querySelector(s);
const MP_LABELS = { wb: "Wildberries", ozon: "Ozon", yandex: "Яндекс.Маркет" };
const MP_COLORS = { wb: "#6f4bff", ozon: "#3b6cff", yandex: "#b59a3e" };
let currentTab = "dashboard";
const charts = {};

const UI_VERSION = "75";
if (document.title) document.title = "Agent Market \u00B7 UI v" + UI_VERSION;

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
  // fn() вызываем уже внутри цепочки промисов. Иначе синхронный throw
  // вылетел бы до .finally(), и спиннер остался бы гореть навсегда.
  return Promise.resolve().then(fn)
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

// --------------------------------------------------- реестр localStorage
// Что приложение хранит в браузере пользователя. Единый список для людей и
// агентов: тест tests/unit/test_local_storage_manifest.py сверяет его с
// фактическими вызовами localStorage и storageKey в этом файле. Новый ключ —
// добавить с описанием (иначе тест упадёт), старый — не удалать, пока не
// вырезан код.
// Значение: точный ключ либо префикс с «*» (ключи вида base + суффикс).
const LS_KEYS = {
  "write_db_*": "«в БД» у кнопок скачивания: write_db_<api>_<kind>",
  "ozBySize:*": "режим «в разрезе размеров» Ozon-вкладок: ozBySize:<tab>",
  "dash-collapsed": "свёрнутые секции дашборда",
  "dash-order": "порядок секций дашборда",
  "replenishPdf": "параметры PDF вкладки «Потребность в товаре»",
  "products_price_settings": "настройки цен в прайсе товаров",
  "pricing_filters": "колоночные фильтры автопилота (JSON)",
  "pricing_filters_on": "панель колоночных фильтров автопилота включена",
  "pricing_settings": "настройки автопилота цен (JSON)",
  "pricing_hide_skip": "скрыть «держать»/«пропустить» в автопилоте",
  "pricing_settings_visible": "меню «Настройки» автопилота развёрнуто",
  "pricing_open_groups": "раскрытые группы настроек автопилота (JSON)",
};
// «Вид таблицы»: storageKey каждой вкладки из registerColView (плюс "_<mode>"
// у режимных, например wbDetailCols_rows) — видимость, порядок, закрепление
// колонок. Ручное дублировение управляемое: тест сверяет список с registerColView.
const LS_COLVIEW_KEYS = [
  "dashLossCols", "dashPrefixCols", "dashPriceCols", "dashProfitCols",
  "marginDetailCols", "marginFunnelCols", "marginOzonDetailCols",
  "ozAccrualCols", "ozCardsCols", "ozCashflowCols", "ozDetailCols",
  "ozPlacementCols", "ozPricesCols", "ozRealCols", "ozStockCols",
  "pricingCols", "productsCols", "replenishCols",
  "wbCardsCols", "wbDetailCols", "wbFunnelCols", "wbPricesCols",
  "wbSalesCols", "wbStockCols", "wbStorageCols",
];

// «в разрезе размеров» OZON: по умолчанию отчёты сворачивают размеры в строку
// товара (базовый артикул). Переключатель стоит в тулбаре каждого раздела, где
// есть свод, и у каждого раздела своё состояние: маржа может быть по размерам,
// а размещение — по товарам. Сырые строки («Строками») всегда по карточкам,
// поэтому в этом режиме переключатель приглушается.
const OZ_BY_SIZE = {
  "margin-ozon-detail": "marginOzBySize",
  "oz-detail": "ozDetailBySize",
  "oz-placement": "ozPlacementBySize",
};
const ozBySizeKey = (tab) => "ozBySize:" + tab;
function ozBySize(tab) {
  return localStorage.getItem(ozBySizeKey(tab)) === "1";
}
function setOzBySize(tab, on) {
  localStorage.setItem(ozBySizeKey(tab), on ? "1" : "0");
}
// Добавляет by_size=1 к строке запроса, если у раздела включён режим по размерам.
function ozBySizeParam(tab, p) {
  return ozBySize(tab) ? (p ? p + "&by_size=1" : "?by_size=1") : p;
}
// Переключатель не действует в режиме «Строками» — гасим его вместе с label.
const OZ_BY_SIZE_RAW = {
  "oz-detail": "ozDetailRaw",
  "oz-placement": "ozPlacementRaw",
};
function syncOzBySizeDisabled() {
  document.querySelectorAll("[data-oz-size-for]").forEach((label) => {
    const tab = label.dataset.ozSizeFor;
    const el = document.getElementById(OZ_BY_SIZE[tab]);
    if (!el) return;
    const rawEl = document.getElementById(OZ_BY_SIZE_RAW[tab] || "");
    const off = !!(rawEl && rawEl.checked);
    el.disabled = off;
    label.classList.toggle("off", off);
  });
}
function initOzBySize() {
  Object.keys(OZ_BY_SIZE).forEach((tab) => {
    const el = document.getElementById(OZ_BY_SIZE[tab]);
    if (!el) return;
    el.checked = ozBySize(tab);
    el.addEventListener("change", () => {
      setOzBySize(tab, el.checked);
      if (currentTab === tab) loadTab(tab);
    });
  });
  syncOzBySizeDisabled();
}

function filters() {
  return {
    marketplace: $("#fMarketplace").value,
    date_from: $("#fFrom").value,
    date_to: $("#fTo").value,
  };
}

// Расширенный поиск: «*» — любая последовательность символов (в т.ч. пустая),
// всё остальное — буквально, регистр не важен. «abc*12*xyz» ищет именно в этом
// порядке. Семантика совпадает с серверной (app/services/common.py).
const _LIKE_RE_CACHE = new Map();
function likeRe(query) {
  const q = String(query || "").trim().toLowerCase();
  if (_LIKE_RE_CACHE.has(q)) return _LIKE_RE_CACHE.get(q);
  // экранируем всё (включая «*»), затем возвращаем «*» к любой последовательности
  const src = q
    .replace(/[.*+?^${}()|[\]\\]/g, "\\$&")
    .replace(/\\\*/g, ".*");
  const re = new RegExp(src);
  if (_LIKE_RE_CACHE.size > 200) _LIKE_RE_CACHE.clear();
  _LIKE_RE_CACHE.set(q, re);
  return re;
}

function likeMatch(haystack, query) {
  const q = String(query || "").trim();
  if (!q) return true;
  return likeRe(q).test(String(haystack == null ? "" : haystack).toLowerCase());
}

function qs(params) {
  const usp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v !== undefined && v !== null && v !== "") usp.set(k, v);
  }
  const s = usp.toString();
  return s ? "?" + s : "";
}

// Любой запрос к API ограничен по времени. Без этого зависшее соединение
// держит «Применить» в состоянии «думает» неограниченно долго: счётчик
// busyDepth уменьшается только в finally(), который не наступит никогда.
const API_TIMEOUT_MS = 120000;

function apiSignal() {
  if (typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function") {
    return AbortSignal.timeout(API_TIMEOUT_MS);
  }
  return undefined;
}

function apiErr(e) {
  if (e && e.name === "AbortError") {
    return new Error("Сервер не ответил за " + Math.round(API_TIMEOUT_MS / 1000) + " с");
  }
  return e;
}

async function api(path) {
  let resp;
  try {
    resp = await fetch("/api" + path, { signal: apiSignal() });
  } catch (e) {
    throw apiErr(e);
  }
  if (!resp.ok) throw new Error(resp.status + " " + (await resp.text()));
  return resp.json();
}

async function apiPost(path, body) {
  let resp;
  try {
    resp = await fetch("/api" + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
      signal: apiSignal(),
    });
  } catch (e) {
    throw apiErr(e);
  }
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
    const tip = c.tip ? ' data-tip="' + escapeHtml(c.tip) + '"' : "";
    h += '<th data-k="' + c.k + '" class="' + (c.num ? "num sortable" : "sortable") +
      '"' + tip + (c.tip ? "" : ' title="Сортировать"') + ">" + c.label + arrow + "</th>";
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
      } else {
        h += totCell(c, v);
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

// Ячейка строки «Итого»: v — число (сумма) или {avg: n, dp: decimals} (среднее;
// dp — знаков после запятой, если рендер колонки кастомный и fmtFloat не ясен).
function totCell(c, v) {
  const avg = v && typeof v === "object" && typeof v.avg === "number" ? v.avg : null;
  const dp = v && typeof v === "object" && typeof v.dp === "number" ? v.dp : null;
  const n = avg !== null ? avg : v;
  const cls = "num totals" + (avg !== null ? " avg" : "");
  const attr = avg !== null ? ' title="Среднее по видимым строкам (по строкам, где значение есть)"' : "";
  const pre = avg !== null ? "≈ " : "";
  let s;
  if (c.render === cellFmts.money) s = fmtMoney(n);
  else if (c.render === cellFmts.moneyCls) s = fmtMoney(n);
  else if (c.render === cellFmts.moneyZero) s = n ? fmtMoney(n) : "—";
  else if (c.render === cellFmts.money2) s = fmtMoney2(n);
  else if (c.render === cellFmts.money4) s = fmtMoney4(n);
  else if (c.render === cellFmts.int) s = fmt(Math.round(n));
  else if (c.render === cellFmts.intZero) s = n ? fmt(Math.round(n)) : "—";
  else if (c.render === cellFmts.pct) s = fmtPct(n);
  else if (v && typeof v === "object" && v.pct) s = fmtPct(n);
  else if (dp == null) s = n == null ? "—" : fmt(Math.round(Number(n)));
  else s = n == null ? "—" : dp === 0 ? fmt(Math.round(n)) : fmtFloat(n, dp);
  return '<th class="' + cls + '"' + attr + '><b>' + pre + s + "</b></th>";
}

const cellFmts = {
  money: (v) => v == null ? "—" : fmtMoney(v),
  money2: (v) => v == null ? "—" : fmtMoney2(v),
  money4: (v) => v == null ? "—" : fmtMoney4(v),
  moneyCls: (v) => v == null ? "—" : `<span class="${cls(v)}">${fmtMoney(v)}</span>`,
  pct: (v) => v == null ? "—" : `<span class="${cls(v)}">${fmtPct(v)}</span>`,
  signedPct: (v) => v == null || v === "" || isNaN(Number(v)) ? "—"
    : `<span class="${Number(v) < 0 ? "neg" : "pos"}">${Number(v) > 0 ? "+" : ""}${fmtPct(v)}</span>`,
  int: (v) => v == null ? "—" : fmt(v),
  intZero: (v) => !v ? "—" : fmt(v),
  moneyZero: (v) => !v ? "—" : fmtMoney(v),
  moneyEst: (v, r) => r && r.net_cost_est
    ? '<span class="est" title="Себестоимость не задана — оценка">~' + fmtMoney(v) + "</span>"
    : fmtMoney(v),
  text: (v) => (v == null || v === "") ? "—" : v,
  tag: (v) => `<span class="tag ${v}">${MP_LABELS[v] || v}</span>`,
};

function pagedTable(container, headers, rows, footers, pagerSel, pinned) {
  if (!container._pt) container._pt = { limit: 100, showAll: false, sort: null };
  const st = container._pt;
  st.headers = headers;
  st.rows = rows;
  st.footers = footers || null;
  st.pagerSel = pagerSel || null;
  st.pinned = pinned || null;
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
    // Сохраняем позицию прокрутки (горизонт. и вертик.), чтобы после
    // сортировки/перерисовки таблица не «улетала» в начало.
    const prevSc = container._pt && container._pt._sc ? container._pt._sc : { left: 0, top: 0 };
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
    container.classList.add("paged");
    const tscroll = document.createElement("div");
    tscroll.className = "tscroll";
    const wrap = document.createElement("div");
    wrap.innerHTML = table(st.headers, visible, st.sort, st.footers);
    tscroll.appendChild(wrap);
    container.appendChild(tscroll);
    const tr = wrap.querySelector("tr.totals-row");
    if (tr) {
      const hdr = wrap.querySelector("thead tr:not(.totals-row)");
      const h = hdr ? hdr.offsetHeight : 0;
      tr.querySelectorAll("th").forEach((th) => { th.style.top = h + "px"; });
    }
    applyStickyCols(wrap, st.headers, st.pinned);
    // Возвращаем позицию прокрутки после пересборки DOM (до mountXBar,
    // чтобы нижний скроллбар синхронизировался с восстановленной позицией).
    const tsc = container.querySelector(".tscroll");
    if (tsc && prevSc.left) tsc.scrollLeft = prevSc.left;
    if (prevSc.top) container.scrollTop = prevSc.top;
    container._pt._sc = { left: tsc ? tsc.scrollLeft : 0, top: container.scrollTop };
    mountXBar(container, tscroll);
  };
  paint();
}

function applyStickyCols(wrap, headers, pinned) {
  if (!pinned || !pinned.length) return;
  const tableEl = wrap.querySelector("table");
  if (!tableEl) return;
  const idxs = pinned.map((k) => headers.findIndex((h) => h.k === k)).filter((i) => i >= 0);
  if (!idxs.length) return;
  tableEl.classList.add("stck");
  const firstRow = tableEl.querySelector("tbody tr");
  const headRow = tableEl.querySelector("thead tr:not(.totals-row)");
  let left = 0;
  for (const i of idxs) {
    const hc = headRow ? headRow.cells[i] : null;
    const bc = firstRow ? firstRow.cells[i] : null;
    const w = Math.max(hc ? hc.getBoundingClientRect().width : 0, bc ? bc.getBoundingClientRect().width : 0);
    for (const tr of tableEl.rows) {
      const cell = tr.cells[i];
      if (cell) {
        cell.classList.add("stck-col");
        cell.style.left = left + "px";
      }
    }
    left += Math.max(w, 8);
  }
}

function mountXBar(container, scroll) {
  let bar = container.querySelector(".t-xbar");
  if (!bar) {
    bar = document.createElement("div");
    bar.className = "t-xbar";
    bar.appendChild(document.createElement("div"));
    bar.addEventListener("scroll", () => { scroll.scrollLeft = bar.scrollLeft; });
    container.appendChild(bar);
  }
  const inner = bar.firstChild;
  const fit = () => {
    const over = scroll.scrollWidth > scroll.clientWidth + 1;
    bar.classList.toggle("x-null", !over);
    inner.style.width = Math.max(scroll.scrollWidth, scroll.clientWidth, 1) + "px";
  };
  bar.scrollLeft = scroll.scrollLeft;
  scroll.onscroll = () => { bar.scrollLeft = scroll.scrollLeft; };
  fit();
}

function tabLike(id) {
  return $("#" + id) ? $("#" + id).value.trim() : "";
}

// Отложенный запуск (debounce) — единая реализация вместо копий
// `clearTimeout(timer); timer = setTimeout(...)` в десятках обработчиков.
function debounceRun(fn, ms) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => { timer = null; fn(...args); }, ms || 400);
  };
}
// Ввод в фильтр вкладки: перезагружает её только пока вкладка активна.
// run не задан → обычная loadTab(tab).
function onTabInput(id, tab, run, ms) {
  const el = $("#" + id);
  if (!el) return;
  el.addEventListener("input", debounceRun(() => {
    if (currentTab === tab) (run ? run() : loadTab(tab));
  }, ms));
}
// Смена контрола (селект/чекбокс) вкладки — сразу, без задержки.
function onTabChange(id, tab, run) {
  const el = $("#" + id);
  if (!el) return;
  el.addEventListener("change", () => {
    if (currentTab === tab) (run ? run() : loadTab(tab));
  });
}

// Статус раздела — одна строка в шапке рядом с «Применить» (эталон: «Потребность
// в товаре»). Раньше зелёный текст жил в тулбаре каждой панели, из-за чего при
// переключении вкладок он оставался висеть уже в чужом разделе. Писатели статуса
// получают один элемент (statusEl/setStatus), длинный текст обрезается
// многоточием, полный — во всплывающей подсказке по data-tip.
const HEADER_MSG_MAX = 150;
function statusEl() {
  return document.getElementById("headerMsg");
}
function normalizeStatus() {
  const el = statusEl();
  if (!el) return null;
  const s = el.textContent || "";
  if (s.length > HEADER_MSG_MAX) {
    const cut = s.slice(0, HEADER_MSG_MAX) + "…";
    // Обрезанный текст длиной HEADER_MSG_MAX + 1 сам не проходит проверку
    // выше, поэтому писать надо только при реальном изменении. Без этой
    // проверки замена textNode снова будит наблюдатель, и normalizeStatus
    // зацикливается сам на себе — вкладка «Потребность» с длинным статусом
    // висела в «не отвечает» (статус длиннее 150 символов).
    if (el.textContent !== cut) {
      el.dataset.tip = s;
      el.textContent = cut;
    }
  } else if (el.dataset.tip) {
    delete el.dataset.tip;
  }
  return el;
}
function setStatus(text, opts) {
  const el = statusEl();
  if (!el) return null;
  el.textContent = String(text == null ? "" : text);
  el.classList.toggle("error", !!(opts && opts.error));
  return normalizeStatus();
}
function clearStatus() {
  const el = statusEl();
  if (!el) return;
  el.textContent = "";
  delete el.dataset.tip;
  el.classList.remove("error");
}
// Писатели работают и через statusEl(), и прямыми msg.textContent = … —
// наблюдатель уравнивает обрезку и подсказку для обоих способов.
function initStatusWatch() {
  const el = statusEl();
  if (!el || typeof MutationObserver === "undefined") return;
  new MutationObserver(normalizeStatus).observe(el, {
    childList: true, characterData: true, subtree: true,
  });
}
// Почему в разделе пусто. Раньше текст был зашит в JS («период с данными:
// 2026-02-21 … 2026-08-30») и не зависел ни от окна, ни от базы — при смене
// периодов он продолжал называть одни и те же даты. Теперь API отдаёт факты:
// запрошенное окно (window) и реальное покрытие таблицы (detail_range).
function emptyPeriodReason(data, what, hint) {
  const w = data.window || {};
  const win = w.date_from && w.date_to ? w.date_from + " … " + w.date_to : "";
  const r = data.detail_range || {};
  const cover = r.date_from && r.date_to ? r.date_from + " … " + r.date_to : "";
  const tail = hint ? " " + hint : "";
  if (!r.rows || !cover) return "Пусто: " + what + " в базе нет" + tail;
  if (!win) return "Нет данных: " + what + ". Укажите период в шапке и нажмите «Применить».";
  return "Нет данных: " + what + " за " + win + " · в базе покрыто " + cover + "." + tail;
}
function ozEmptyReason(data) {
  return emptyPeriodReason(data, "детализации продаж Ozon",
    "Обновите детализацию: «Обновить базу» → OZON API ▸ Детализация продаж.");
}
// Период, который реально анализирует раздел: даты из шапки. Отдельных полей
// «С/По» внутри панелей больше нет — иначе «Применить» менял шапку, а раздел
// продолжал считать по своему старому периоду (такое было в размещении, начислениях
// и движении средств Ozon).
function paneDates() {
  const f = filters();
  return { date_from: f.date_from || "", date_to: f.date_to || "" };
}

let _hdrTipEl = null, _hdrTipTimer = null, _hdrTipNode = null;
function initHeaderTip() {
  document.addEventListener("mouseover", (ev) => {
    const n = ev.target.closest && ev.target.closest("[data-tip]");
    if (!n) { hideHeaderTip(); return; }
    if (_hdrTipNode === n) return;
    _hdrTipNode = n;
    clearTimeout(_hdrTipTimer);
    const txt = n.dataset.tip || "";
    if (!txt.trim()) return;
    _hdrTipTimer = setTimeout(() => {
      if (!_hdrTipEl) {
        _hdrTipEl = document.createElement("div");
        _hdrTipEl.id = "hdrTip";
        document.body.appendChild(_hdrTipEl);
      }
      _hdrTipEl.textContent = txt;
      _hdrTipEl.style.display = "block";
      const r = n.getBoundingClientRect();
      const iw = _hdrTipEl.offsetWidth || 360;
      const ih = _hdrTipEl.offsetHeight || 40;
      let x = r.left + r.width / 2 - iw / 2;
      x = Math.max(8, Math.min(x, window.innerWidth - iw - 8));
      let y = r.bottom + 6;
      if (y + ih > window.innerHeight - 8) y = r.top - ih - 6;
      _hdrTipEl.style.left = x + "px";
      _hdrTipEl.style.top = Math.max(8, y) + "px";
    }, 260);
  });
  document.addEventListener("mouseout", (ev) => {
    const n = ev.target.closest && ev.target.closest("[data-tip]");
    if (n && n === _hdrTipNode) hideHeaderTip();
  });
  document.addEventListener("scroll", () => hideHeaderTip(), true);
  window.addEventListener("resize", () => hideHeaderTip());
}
function hideHeaderTip() {
  clearTimeout(_hdrTipTimer);
  _hdrTipNode = null;
  if (_hdrTipEl) _hdrTipEl.style.display = "none";
}

async function loadTab(name) {
  const f = filters();
  await busyRun(() => loadTabInner(name, f));
}

// Ошибка загрузки вкладки — в статус шапки с классом error. Раньше в catch
// оставался только console.error: пользователь видел пустую/старую таблицу
// без единого объяснения.
function showLoadError(err) {
  const detail = err && err.message ? err.message : String(err);
  setStatus("Ошибка загрузки: " + detail, { error: true });
}

async function loadTabInner(name, f) {

  // Статус общий для всех разделов — гасим при каждом переключении вкладки,
  // иначе зелёная строка предыдущего раздела остаётся висеть в новом.
  clearStatus();
  try {
    if (name === "dashboard") await renderDashboard(qs(f));
    else if (name === "margin-funnel") {
      await renderMarginFunnel(qs({ date_from: f.date_from, date_to: f.date_to, article_like: tabLike("marginFunnelLike") || undefined }));
    } else if (name === "margin-detail") {
      const cmpEl = $("#marginDetailCompare");
      await renderMarginDetail(qs({ date_from: f.date_from, date_to: f.date_to, article_like: tabLike("marginDetailLike") || undefined, compare: cmpEl && cmpEl.checked ? 1 : undefined }));
    } else if (name === "margin-ozon-detail") {
      const cmpEl = $("#marginOzonDetailCompare");
      await renderMarginOzonDetail(qs({ date_from: f.date_from, date_to: f.date_to, article_like: tabLike("marginOzonDetailLike") || undefined, compare: cmpEl && cmpEl.checked ? 1 : undefined }));
    } else if (name === "sales") await renderSales(qs(f));
    else if (name === "stocks") await renderStocks(f.marketplace);
    else if (name === "ours") await renderOurs();
    else if (name === "products") await renderProducts();
    else if (name === "wh-cp") await renderWhCp();
    else if (name === "wh-receipt") await renderWhDocs("receipt", "whRTable", "headerMsg", "whRDetail");
    else if (name === "wh-shipment") await renderWhDocs("shipment", "whSTable", "headerMsg", "whSDetail");
    else if (name === "wh-stock") await renderWhStock();
    else if (name === "wh-turnover") await renderWhTurnover();
    else if (name === "replenish") await renderReplenish();
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
    else if (name === "oz-placement") await renderOzPlacement();
    else if (name === "oz-cashflow") await renderOzCashflow();
    else if (name === "oz-accrual") await renderOzAccrual();
    else if (name === "yandex") await renderYandexFiles();
    else if (name === "tickets") await renderTickets();
  } catch (err) {
    console.error("loadTab error:", err);
    showLoadError(err);
  }
}

function setChart(id, cfg) {
  if (charts[id]) { charts[id].destroy(); }
  charts[id] = new Chart($("#" + id), cfg);
}

let dashData = null;

function dashQuery() {
  const from = $("#dashFrom") && $("#dashFrom").value
    ? $("#dashFrom").value : $("#fFrom").value;
  const to = $("#dashTo") && $("#dashTo").value
    ? $("#dashTo").value : $("#fTo").value;
  const q = { date_from: from, date_to: to };
  const mp = $("#dashMarketplace").value;
  if (mp) q.marketplace = mp;
  if ($("#dashCompare").checked) q.compare = 1;
  return q;
}

const DASH_COLLAPSED_KEY = "dash-collapsed";
const DASH_ORDER_KEY = "dash-order";

function dashCollapsedIds() {
  try {
    return (localStorage.getItem(DASH_COLLAPSED_KEY) || "").split(",").filter(Boolean);
  } catch (e) { return []; }
}

function initDashCollapse() {
  const saved = dashCollapsedIds();
  document.querySelectorAll("[data-csec]").forEach((sec) => {
    const on = saved.includes(sec.dataset.csec);
    sec.classList.toggle("collapsed", on);
    const head = sec.querySelector(".dash-sec-title");
    if (head) head.setAttribute("aria-expanded", on ? "false" : "true");
  });
  restoreDashOrder();
}

function toggleDashSection(sec) {
  const id = sec.dataset.csec;
  const collapsed = !sec.classList.contains("collapsed");
  sec.classList.toggle("collapsed", collapsed);
  const head = sec.querySelector(".dash-sec-title");
  if (head) head.setAttribute("aria-expanded", String(!collapsed));
  let saved = dashCollapsedIds();
  if (collapsed) {
    if (!saved.includes(id)) saved.push(id);
  } else {
    saved = saved.filter((x) => x !== id);
  }
  try { localStorage.setItem(DASH_COLLAPSED_KEY, saved.join(",")); } catch (e) {}
}

function dashOrderIds() {
  try {
    return (localStorage.getItem(DASH_ORDER_KEY) || "").split(",").filter(Boolean);
  } catch (e) { return []; }
}

function saveDashOrder() {
  const order = Array.from(document.querySelectorAll("#tab-dashboard [data-csec]"))
    .map((s) => s.dataset.csec);
  try { localStorage.setItem(DASH_ORDER_KEY, order.join(",")); } catch (e) {}
}

function restoreDashOrder() {
  const want = dashOrderIds();
  if (!want.length) return;
  const pane = document.querySelector("#tab-dashboard");
  if (!pane) return;
  const secs = Array.from(pane.querySelectorAll("[data-csec]"));
  const by = {};
  secs.forEach((s) => { by[s.dataset.csec] = s; });
  let prev = document.querySelector("#tab-dashboard .dash-hero") || null;
  for (const id of want) {
    if (!by[id]) continue;
    pane.insertBefore(by[id], prev ? prev.nextSibling : pane.firstChild);
    prev = by[id];
  }
}

function initDashDrag() {
  const pane = document.querySelector("#tab-dashboard");
  if (!pane) return;
  let dragged = null;
  pane.addEventListener("dragstart", (ev) => {
    const h = ev.target.closest(".dash-drag");
    if (!h) return;
    const sec = h.closest("[data-csec]");
    if (!sec) return;
    dragged = sec;
    ev.dataTransfer.effectAllowed = "move";
    ev.dataTransfer.setData("text/plain", sec.dataset.csec || "");
    sec.classList.add("dragging");
  });
  pane.addEventListener("dragend", () => {
    if (dragged) dragged.classList.remove("dragging");
    dragged = null;
    pane.querySelectorAll("[data-csec]").forEach((s) => s.classList.remove("drag-over"));
  });
  pane.addEventListener("dragover", (ev) => {
    if (!dragged) return;
    ev.preventDefault();
    ev.dataTransfer.dropEffect = "move";
    const target = ev.target.closest("[data-csec]");
    if (target && target !== dragged) target.classList.add("drag-over");
  });
  pane.addEventListener("drop", (ev) => {
    if (!dragged) return;
    ev.preventDefault();
    const target = ev.target.closest("[data-csec]");
    if (target && target !== dragged) {
      const rect = target.getBoundingClientRect();
      const before = ev.clientY < rect.top + rect.height / 2;
      pane.insertBefore(dragged, before ? target : target.nextSibling);
      saveDashOrder();
    }
    dragged.classList.remove("dragging");
    dragged = null;
    pane.querySelectorAll("[data-csec]").forEach((s) => s.classList.remove("drag-over"));
  });
}

const dashTag = (v) => (v == null || v === "")
  ? "—"
  : `<span class="tag">${String(v).split(",").map((x) => MP_LABELS[x.trim()] || x.trim()).join(" + ")}</span>`;
const dashSigned = (v) => v == null ? "—"
  : `<span class="${cls(v)}">${v > 0 ? "+" : ""}${fmtMoney(v)}</span>`;
const dashSignedPct = (v) => v == null || v === "" || isNaN(Number(v)) ? "—"
  : `<span class="${cls(v)}">${v > 0 ? "+" : ""}${fmtPct(v)}</span>`;

const dashHeaders = {
  tops: [
    { k: "article", label: "Артикул", render: cellFmts.text , tip: "Артикул поставщика. Строка = артикул по всем выбранным МП сразу: если товар продавался и на WB, и на Ozon, будет одна строка."},
    { k: "name", label: "Наименование", render: cellFmts.text , tip: "Наименование из каталога товаров; если оно пустое — берётся из строки детализации продаж."},
    { k: "marketplace", label: "МП", num: true, render: dashTag , tip: "Список маркетплейсов, где артикул продавался в периоде. У Ozon строка — это товар, артикулы размеров свёрнуты в один."},
    { k: "sells", label: "Продано, шт", num: true, render: cellFmts.int , tip: "Количество проданных за период. WB — за вычетом возвратов (со знаком минус), Ozon — гросс-продажи, возвраты здесь не вычтены."},
    { k: "revenue", label: "Выручка", num: true, render: cellFmts.money , tip: "Сумма реализации за период: WB — retailAmount, Ozon — цена продавца × количество. Возвраты вычитаются со знаком минус."},
    { k: "income", label: "Доход", num: true, render: cellFmts.money , tip: "К перечислению от маркетплейса. У WB это forPay, у Ozon — income: у Ozon комиссия и услуги уже вычтены, у WB нет."},
    { k: "margin", label: "Прибыль", num: true, render: cellFmts.moneyCls , tip: "Прибыль = к перечислению − расходы МП (логистика, хранение, услуги) − себестоимость × продано. Сумма по всем МП артикула."},
    { k: "margin_per_one", label: "Прибыль/шт", num: true, render: cellFmts.moneyCls , tip: "Прибыль ÷ проданное количество, ₽/шт. Если продаж не было — 0."},
    { k: "margin_pct", label: "Рентаб., %", num: true, render: cellFmts.pct , tip: "Рентабельность: прибыль ÷ к перечислению × 100. База — доход, а не выручка (отличается от детализации WB)."},
  ],
  price: [
    { k: "article", label: "Артикул", render: cellFmts.text , tip: "Артикул поставщика. Сравниваются только те артикулы, которые продавались и в текущем, и в предыдущем окне."},
    { k: "name", label: "Наименование", render: cellFmts.text , tip: "Наименование товара из детализации текущего периода."},
    { k: "avg", label: "Цена сейчас, ₽", num: true, render: cellFmts.money2 , tip: "Средняя цена продажи за текущее окно: сумма реализации ÷ количество проданных. У Ozon считается по базовому артикулу товара."},
    { k: "avg_prev", label: "Цена прош. пер., ₽", num: true, render: cellFmts.money2 , tip: "Та же средняя цена, но за предыдущее окно той же длительности."},
    { k: "delta_ru", label: "Δ, ₽", num: true, render: dashSigned , tip: "Текущая средняя цена минус прошлая, ₽. Плюс — подорожал, минус — подешевел."},
    { k: "delta_pct", label: "Δ, %", num: true, render: dashSignedPct , tip: "Изменение цены к прошлому окну в процентах. По этой величине формируются оба топа: подорожавшие и подешевевшие."},
    { k: "margin", label: "Прибыль", num: true, render: cellFmts.moneyCls , tip: "Прибыль артикула за текущий период из детализации: показывает, что даёт новая цена. Пусто, если артикула нет в сводке."},
  ],
  prefix: [
    { k: "prefix", label: "Группа", render: cellFmts.text , tip: "Группа товара по карте префиксов артикулов (SHK→SH, SOHO-FRNT отдельно от SOHO и т.п.). Без совпадения — «остальные»."},
    { k: "articles", label: "Товаров", num: true, render: cellFmts.int , tip: "Сколько артикулов попало в группу. Товары без продаж в периоде в подсчёт не идут."},
    { k: "sells", label: "Продано, шт", num: true, render: cellFmts.int , tip: "Сумма проданных единиц по всем артикулам группы за период."},
    { k: "revenue", label: "Выручка", num: true, render: cellFmts.money , tip: "Сумма реализации по артикулам группы за период."},
    { k: "income", label: "Доход", num: true, render: cellFmts.money , tip: "Сумма «к перечислению» по артикулам группы."},
    { k: "margin", label: "Прибыль", num: true, render: cellFmts.moneyCls , tip: "Суммарная прибыль группы. Группы отсортированы по ней по убыванию."},
    { k: "margin_per_one", label: "Прибыль/шт", num: true, render: cellFmts.moneyCls , tip: "Прибыль группы ÷ проданное количество, ₽/шт."},
    { k: "margin_pct", label: "Рентаб., %", num: true, render: cellFmts.pct , tip: "Рентабельность группы: прибыль ÷ к перечислению × 100."},
  ],
};

function dashTotals(rows) {
  if (!rows || !rows.length) return null;
  const sum = (k) => rows.reduce((a, r) => a + (Number(r[k]) || 0), 0);
  const sells = sum("sells");
  const income = sum("income");
  const margin = sum("margin");
  const t = {
    sells,
    returns_qty: sum("returns_qty"),
    revenue: sum("revenue"),
    income,
    margin,
    articles: sum("articles"),
    margin_per_one: sells ? { avg: margin / sells } : null,
    margin_pct: income ? { avg: margin / income * 100 } : null,
  };
  Object.keys(t).forEach((k) => { if (t[k] == null) delete t[k]; });
  return t;
}

function paintDashColviewTable(containerId, baseHeaders, rows, count, tab, showTotals) {
  const headers = colViewHeaders(tab, baseHeaders);
  const footers = showTotals === false ? null : dashTotals(rows);
  pagedTable($("#" + containerId), headers, rows, footers, null, colViewPinKeys(tab));
  const cnt = $("#" + containerId + "Count");
  if (cnt) cnt.textContent = count ? "· " + fmt(count) : "";
}

function paintDashKpis(k) {
  const showCompare = $("#dashCompare").checked && k.compare;
  const cards = [k.total, ...k.per_mp];
  $("#dashKpis").innerHTML = cards.map((c) => {
    const mpc = c.marketplace || "";
    const label = mpc ? MP_LABELS[mpc] : "Итого";
    const ret = c.returns_qty || 0;
    const retRate = (ret + (c.sells || 0)) > 0
      ? Math.round(ret / (ret + (c.sells || 0)) * 1000) / 10 : 0;
    const cell = (lbl, val, extraCls, valCls) => `
      <div class="kpi-cell">
        <div class="kpi-label">${lbl}</div>
        <div class="kpi-val ${extraCls || ""} ${valCls || ""}">${val}</div>
      </div>`;
    let html = `
      <div class="kpi-card ${mpc}" ${mpc ? `style="border-left-color:${MP_COLORS[mpc]}"` : ""}>
        <div class="kpi-card-title">${label}</div>
        <div class="kpi-card-grid">
          ${cell("Выручка", fmtMoney(c.revenue))}
          ${cell("Доход", fmtMoney(c.income))}
          ${cell("Прибыль", fmtMoney(c.margin), cls(c.margin))}
          ${cell("Рентабельность", fmtPct(c.margin_pct), cls(c.margin_pct))}
          ${cell("Маржа до себестоимости", fmtMoney(c.margin_gross != null ? c.margin_gross : 0))}
          ${cell("Прибыль / шт", fmtMoney(c.margin_per_one), cls(c.margin_per_one), "small")}
          ${cell("Продано, шт", fmt(c.sells))}
          ${cell("Возвраты", ret ? `${fmt(ret)} (${fmtPct(retRate)})` : "0")}
          ${cell("Товаров", fmt(c.articles))}
        </div>`;
    if (showCompare && !mpc) {
      const cmp = k.compare;
      html += `<div class="kpi-delta">
        <span class="kpi-delta-label">Δ к прошл. периоду</span>
        <span class="kvalue ${cls(cmp.delta_ru)}">${dashSigned(cmp.delta_ru)}</span>
        <span class="kvalue ${cls(cmp.delta_pct)}">${cmp.delta_pct == null ? "" : "(" + (cmp.delta_pct > 0 ? "+" : "") + fmtPct(cmp.delta_pct) + ")"}</span>
      </div>`;
    }
    return html + `</div>`;
  }).join("");
}

const DASH_METRICS = {
  revenue: { label: "Выручка", color: "#3b6cff" },
  income: { label: "Доход", color: "#1fae64" },
  profit: { label: "Прибыль", color: "#d18f00" },
};

// График А «Показатели»: разрез × показатель. Списки расширяемы — новые
// опции добавляются этим словарям, ничего в коде рендера менять не нужно.
const DASH_A_METRICS = {
  income: { label: "Доход", color: "#1fae64" },
  margin: { label: "Прибыль", color: "#6f4bff" },
  margin_gross: { label: "Маржа до себестоимости", color: "#d18f00" },
};

const DASH_PREFIX_COLORS = [
  "#6f4bff", "#3b6cff", "#1fae64", "#d18f00", "#e05cb6", "#00a5b8",
  "#8a5a2b", "#7f7f7f", "#b59a3e", "#e05252", "#2e7d32", "#1565c0",
];

function dashChartA() {
  const dim = $("#chartADim") ? $("#chartADim").value : "mp";
  const metric = $("#chartAMetric") ? $("#chartAMetric").value : "income";
  const m = DASH_A_METRICS[metric] || DASH_A_METRICS.income;
  if (dim === "prefix") {
    const rows = (dashData && dashData.prefixes
      ? dashData.prefixes.rows : []).slice();
    const labels = rows.map((r) => r.prefix);
    const values = rows.map((r) => r[metric] || 0);
    const colors = rows.map((_, i) =>
      DASH_PREFIX_COLORS[i % DASH_PREFIX_COLORS.length]);
    return { labels, values, colors, m };
  }
  const rows = dashData && dashData.kpis ? dashData.kpis.per_mp : [];
  const labels = rows.map((x) => MP_LABELS[x.marketplace] || x.marketplace);
  const values = rows.map((x) => x[metric] || 0);
  const colors = rows.map((x) => MP_COLORS[x.marketplace] || "#999");
  return { labels, values, colors, m };
}

function paintDashChartA() {
  const a = dashChartA();
  setChart("chartMpIncome", {
    type: "bar",
    data: {
      labels: a.labels,
      datasets: [{
        label: a.m.label,
        data: a.values,
        backgroundColor: a.colors,
        maxBarThickness: 56,
      }],
    },
    options: chartOpts(a, a.labels.length > 6),
  });
  const cap = $("#dashChartACap");
  if (cap) cap.textContent =
    a.m.label + ($("#chartADim") && $("#chartADim").value === "prefix"
      ? " по группам товаров" : " по маркетплейсам");
}

function chartOpts(a, horizontal) {
  const opts = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: { legend: { display: false } },
    scales: {
      x: { ticks: { autoSkip: false, maxRotation: horizontal ? 30 : 45 } },
      y: { ticks: { callback: (v) => fmtMoney(v) } },
    },
  };
  if (horizontal) {
    opts.indexAxis = "y";
    opts.scales.y.ticks.callback = undefined;
    opts.scales.x.ticks.callback = (v) => fmtMoney(v);
  }
  return opts;
}

function paintDashDailyChart(d) {
  const metric = $("#chartMetric") ? $("#chartMetric").value : "revenue";
  const gran = $("#chartGran") ? $("#chartGran").value : "day";
  const chartType = $("#chartType") ? $("#chartType").value : "bar";
  const m = DASH_METRICS[metric] || DASH_METRICS.revenue;
  const rows = bucketDaily(d.daily, gran);
  const data = rows.map((x) => x[metric] || 0);
  const dataset = chartType === "line"
    ? { label: m.label, data, borderColor: m.color, backgroundColor: m.color, fill: false, tension: 0.25, pointRadius: 2 }
    : { label: m.label, data, backgroundColor: m.color, maxBarThickness: 32 };
  setChart("chartDaily", {
    type: chartType,
    data: { labels: rows.map((r) => dashBucketLabel(r.date, gran)), datasets: [dataset] },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { ticks: { maxTicksLimit: 14 } },
        y: { ticks: { callback: (v) => fmtMoney(v) } },
      },
    },
  });
}

function dashWeekKey(dateStr) {
  const d = new Date(dateStr + "T00:00:00");
  const day = (d.getDay() + 6) % 7;
  const m = new Date(d);
  m.setDate(d.getDate() - day);
  return m.getFullYear() + "-" + String(m.getMonth() + 1).padStart(2, "0")
    + "-" + String(m.getDate()).padStart(2, "0");
}

function dashBucketLabel(key, gran) {
  const p = key.split("-");
  return gran === "week" ? Number(p[2]) + "." + p[1] : p[1] + "." + p[0];
}

function bucketDaily(rows, gran) {
  if (gran === "day") return rows;
  const map = new Map();
  for (const r of rows) {
    const key = gran === "week" ? dashWeekKey(r.date) : r.date.slice(0, 7);
    const b = map.get(key) || { date: key, revenue: 0, income: 0, sells: 0, profit: 0 };
    b.revenue += r.revenue;
    b.income += r.income;
    b.sells += r.sells;
    b.profit += r.profit;
    map.set(key, b);
  }
  return Array.from(map.values())
    .sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
}

function paintDashCharts(d) {
  paintDashChartA();
  paintDashDailyChart(d);
}

function repaintDashCharts() {
  if (dashData) paintDashCharts(dashData);
}

function paintDashStocks(s) {
  const codes = Object.keys(s);
  $("#dashStocks").innerHTML = codes.map((code) => {
    const st = s[code];
    return `
      <div class="kpi ${code}" style="border-left-color:${MP_COLORS[code]}">
        <div class="title">${MP_LABELS[code] || code}</div>
        <div class="lines">
          <div class="line"><span>Остаток, шт</span><span class="kvalue">${fmt(st.quantity_full)}</span></div>
          <div class="line"><span>В пути, шт</span><span class="kvalue">${fmt(st.in_way)}</span></div>
          <div class="line"><span>Оценка, ₽</span><span class="kvalue">${fmtMoney(st.value)}</span></div>
          <div class="line"><span>Срез</span><span class="kvalue">${st.date || "—"}</span></div>
        </div>
      </div>`;
  }).join("") || '<div class="empty">Остатков нет за период</div>';
}

function paintDashFreshness(rows) {
  $("#dashFresh").innerHTML = rows.length ? rows.map((r) => `
    <div class="fresh-row">
      <span class="tag ${r.api}">${MP_LABELS[r.api] || r.api}</span>
      <span class="fresh-kind">${r.kind}</span>
      <span class="fresh-time">${r.last_success_at ? r.last_success_at.slice(0, 16) : "—"}</span>
    </div>`).join("") : '<div class="empty">Загрузок не было</div>';
}

async function renderDashboard() {
  dashData = await api("/dashboard" + qs(dashQuery()));
  paintDashKpis(dashData.kpis);
  paintDashCharts(dashData);
  paintDashColviewTable("dashProfit", dashHeaders.tops, dashData.tops.profit.rows, dashData.tops.profit.count, "dash-profit");
  paintDashColviewTable("dashLoss", dashHeaders.tops, dashData.tops.loss.rows, dashData.tops.loss.count, "dash-loss");
  paintDashColviewTable("dashPriceUp", dashHeaders.price, dashData.price.up.rows, dashData.price.up.count, "dash-price", false);
  paintDashColviewTable("dashPriceDown", dashHeaders.price, dashData.price.down.rows, dashData.price.down.count, "dash-price", false);
  paintDashColviewTable("dashPrefix", dashHeaders.prefix, dashData.prefixes.rows, dashData.prefixes.count, "dash-prefix");
  paintDashStocks(dashData.stocks);
  paintDashFreshness(dashData.freshness);
  const exp = $("#exportDashboard");
  if (exp) exp.href = "/api/export/dashboard" + qs(dashQuery());
}

const marginHeaders = [
{ k: "article", label: "Артикул", render: cellFmts.text , tip: "Артикул поставщика (vendorCode) из строк детализации продаж WB. Артикулы разных размеров агрегируются в одну строку."},
  { k: "nm_id", label: "Артикул WB", render: cellFmts.text , tip: "Код номенклатуры WB (nmId) из строки детализации — по нему товар ищется в каталогах WB."},
  { k: "name", label: "Наименование", render: cellFmts.text , tip: "Наименование из каталога товаров; при пустом каталоге — из строки детализации."},
  { k: "sells",   label: "Продано, шт", num: true, render: cellFmts.int , tip: "Количество проданных за период. Возвраты вычитаются со знаком минус, поэтому значение может быть нулём или отрицательным."},
  { k: "returns_qty", label: "Возвращено, шт", num: true, render: cellFmts.int , tip: "Единиц, возвращённых покупателями: строки детализации с типом документа «Возврат». Уже учтено в «Продано» со знаком минус."},
  { k: "stock_qty", label: "Остаток, шт", num: true, render: cellFmts.int, tip: "По последнему срезу стоков на конец периода (≤ date_to)" },
  { k: "stock_total", label: "Остаток всего, шт", num: true, render: cellFmts.int, tip: "Всего на складах WB по последнему срезу стоков" },
  { k: "stock_in_way", label: "В пути, шт", num: true, render: cellFmts.int, tip: "Ожидается поставкой по последнему срезу стоков" },
  { k: "revenue", label: "Выручка", num: true, render: cellFmts.money , tip: "Реализация (retailAmount) за период. Возвраты вычитаются со знаком минус, поэтому возвращённый товар уменьшает выручку."},
  { k: "commission", label: "Комиссия", num: true, render: cellFmts.moneyCls , tip: "Комиссия КВВ за период с учётом знака возврата. Справочно: из прибыли не вычитается — уже сидит в «к перечислению»."},
  { k: "logistics", label: "Логистика", num: true, render: cellFmts.moneyCls , tip: "Логистика целиком (туда + обратно). Расход остаётся расходом даже по возвращённому товару."},
  { k: "logistics_out", label: "Логистика туда", num: true, render: cellFmts.moneyCls, tip: "Доставка покупателю (строки Продажа)" },
  { k: "logistics_in", label: "Логистика обратно", num: true, render: cellFmts.moneyCls, tip: "Обратная доставка (строки Возврат)" },
  { k: "storage", label: "Хранение (оц)", num: true, render: cellFmts.moneyCls, tip: "Безартикульные платы WB разнесены по «объём × тариф × остаток»" },
  { k: "services", label: "Услуги", num: true, render: cellFmts.moneyCls , tip: "Прочие удержания WB: штрафы, удержания, доплаты и перерасчёт логистики. В безартикульных строках разносятся по весу статьи."},
  { k: "income",  label: "К перечислению", num: true, render: cellFmts.money , tip: "forPay за период с учётом знака возвратов: сколько WB перечислит за товар после всех удержаний."},
  { k: "net_cost", label: "Себестоимость", num: true, render: cellFmts.money , tip: "Себестоимость единицы из каталога: сначала по артикулу, иначе по баркоду строки детализации. Нет в каталоге — значение по умолчанию, строка помечается как оценка."},
  { k: "margin_gross", label: "Маржа, до себестоимости", num: true, render: cellFmts.moneyCls , tip: "К перечислению − логистика − хранение − услуги. Показывает прибыль до списания себестоимости проданного."},
  { k: "margin",  label: "Прибыль", num: true, render: cellFmts.moneyCls , tip: "Маржа до себестоимости − себестоимость × проданное. Себестоимость списывается только за проданное, за возвращённое — нет."},
  { k: "margin_per_one", label: "Прибыль на ед.", num: true, render: cellFmts.moneyCls , tip: "Прибыль ÷ проданное количество, ₽/шт."},
  { k: "margin_pct", label: "Прибыль, %", num: true, render: cellFmts.pct , tip: "Рентабельность: прибыль ÷ выручка (реализация) × 100. База — выручка, а в дашборде — к перечислению."},
  { k: "margin_pct_income", label: "Прибыль % (к перечисл.)", num: true, render: cellFmts.pct , tip: "Рентабельность: прибыль ÷ к перечислению × 100. База — доход после всех удержаний (как в прежнем разделе «Маржинальность»)."},
  { k: "commission_per_one", label: "Комиссия/ед.", num: true, render: cellFmts.moneyCls , tip: "Комиссия КВВ, делённая на количество проданных, ₽/шт."},
  { k: "logistics_per_one", label: "Логистика/ед.", num: true, render: cellFmts.moneyCls , tip: "Вся логистика (туда + обратно) ÷ проданное количество, ₽/шт."},
  { k: "logistics_out_per_one", label: "Логистика туда/ед.", num: true, render: cellFmts.moneyCls , tip: "Логистика туда ÷ проданное количество, ₽/шт."},
  { k: "logistics_in_per_one", label: "Логистика обратно/ед.", num: true, render: cellFmts.moneyCls , tip: "Логистика обратно ÷ проданное количество, ₽/шт. Высокая доля — сигнал о проблемах с качеством или размерной сеткой."},
  { k: "storage_per_one", label: "Хранение/ед.", num: true, render: cellFmts.moneyCls , tip: "Хранение (включая оценку безартикульных плат) ÷ проданное количество, ₽/шт."},
  { k: "income_per_one", label: "К перечисл./ед.", num: true, render: cellFmts.moneyCls , tip: "К перечислению ÷ проданное количество, ₽/шт — фактическая цена продажи, которая достанется продавцу."},
  { k: "revenue_per_one", label: "Средняя цена", num: true, render: cellFmts.moneyCls , tip: "Выручка ÷ проданное количество — средняя цена продажи, ₽/шт."},
  { k: "margin_gross_per_one", label: "Маржа до себест./ед.", num: true, render: cellFmts.moneyCls , tip: "Маржа до себестоимости (перечисление − расходы) ÷ проданное количество, ₽/шт."},
  { k: "return_rate", label: "Доля возвратов, %", num: true, render: cellFmts.pct , tip: "Возвраты ÷ (продажи + возвраты) × 100. Показывает качество товара и размерную сетку."},
  { k: "net_cost_est", label: "Себестоимость оценка", num: true, render: cellFmts.moneyEst , tip: "Себестоимость проданного, оценённая по воронке (поле net_cost_est). ~ перед суммой = каталожной себестоимости нет."},
  { k: "sells_pp", label: "Пред. период: Продано, шт", num: true, render: cellFmts.int , tip: "Продано за предыдущий период — видно при включённом сравнении периодов."},
  { k: "margin_pp", label: "Пред. период: Прибыль, руб", num: true, render: cellFmts.moneyCls , tip: "Прибыль за предыдущий период — видно при включённом сравнении периодов."},
  { k: "delta_ru", label: "Δ прибыли, руб", num: true, render: cellFmts.moneyCls , tip: "Текущая прибыль минус прибыль предыдущего периода, ₽."},
  { k: "delta_pct", label: "Δ прибыли, %", num: true, render: cellFmts.signedPct , tip: "Изменение прибыли к предыдущему периоду, %. Плюс = рост."},
];
// ----------------------------------------------------- «Анализ Продаж OZON» — при
// ----------------------------------------------------- маржинальности от детализации Ozon
const ozonMarginHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text , tip: "Базовый артикул товара Ozon: артикулы отдельных размеров свёрнуты в одну строку по умолчанию."},
  { k: "size", label: "Размер", render: cellFmts.text , tip: "Размер, выделенный из артикула Ozon. Заполняется только в режиме «в разрезе размеров»."},
  { k: "sizes_count", label: "Размеров", num: true, render: cellFmts.int , tip: "Сколько разных размеров свёрнуто в строку товара. 1 — артикул без размера (безразмерный товар)."},
  { k: "offers_count", label: "Артикулов", num: true, render: cellFmts.int , tip: "Сколько карточек Ozon (offer_id) попало в строку товара."},
  { k: "nm_id", label: "Артикул WB", render: cellFmts.text , tip: "Код номенклатуры WB, подтянутый из карточек Ozon по совпадению vendor_code с offer_id. Пусто, если карточка не найдена."},
  { k: "name", label: "Наименование", render: cellFmts.text , tip: "Наименование из каталога товаров по базовому артикулу; при пустом — из строки детализации Ozon."},
  { k: "sells", label: "Продано, шт", num: true, render: cellFmts.int , tip: "Гросс-продажи (quantity из детализации). Возвраты здесь НЕ вычитаются — смотрите отдельную колонку."},
  { k: "returns_qty", label: "Возвращено, шт", num: true, render: cellFmts.int , tip: "Возвраты (returnQty) за период. Их деньги уже учтены в «К перечислению» со знаком минус."},
  { k: "postings", label: "Постинги", num: true, render: cellFmts.int , tip: "Сколько уникальных постингов (отгрузок) было у товара за период — косвенный признак частоты поставок."},
  { k: "revenue", label: "Выручка", num: true, render: cellFmts.money , tip: "Цена продавца × количество (sellerPrice × quantity), суммой по товару за период."},
  { k: "amount", label: "Сумма продажи", num: true, render: cellFmts.money , tip: "Поле amount отчёта Ozon: своя база начислений, может отличаться от выручки. Расхождение — признак разной учётной базы Ozon."},
  { k: "commission", label: "Комиссия", num: true, render: cellFmts.moneyCls , tip: "Комиссия Ozon, в отчёте идёт со знаком минус. Справочно: уже вычтена из «к перечислению», в прибыли не вычитается повторно."},
  { k: "services", label: "Услуги", num: true, render: cellFmts.moneyCls , tip: "Прочие услуги Ozon (standardFee), знак минус. Также справочно: уже учтены в «к перечислению»."},
  { k: "income", label: "К перечислению", num: true, render: cellFmts.money , tip: "Чистая сумма к перечислению Ozon с учётом возвратов. В расчёте прибыли берётся как есть — это база Ozon."},
  { k: "cashflow_est", label: "На р/с (оценка)", num: true, render: cellFmts.money , tip: "Доля фактически поступивших средств за окно, распределённая пропорционально «к перечислению». Показывает задержки выплат Ozon."},
  { k: "accrued_sale", label: "Начисл.: продажа", num: true, render: cellFmts.money , tip: "Начисления Ozon по корзине «sale» из отчёта начислений за окно: сколько начислено именно за продажу."},
  { k: "accrued_commission", label: "Начисл.: комиссия", num: true, render: cellFmts.moneyCls , tip: "Начисления «commission» — комиссия Ozon (отрицательное значение = удержание)."},
  { k: "accrued_logistics", label: "Начисл.: логистика", num: true, render: cellFmts.moneyCls , tip: "Начисления «logistics» — логистика Ozon за окно, знак минус."},
  { k: "accrued_services", label: "Начисл.: услуги", num: true, render: cellFmts.moneyCls , tip: "Начисления «services» — услуги Ozon (в том числе хранение/размещение), знак минус."},
  { k: "accrued_other", label: "Начисл.: прочее", num: true, render: cellFmts.moneyCls , tip: "Начисления «other» (NON_ITEM) — расходы без привязки к карточке товара, целиком на строку."},
  { k: "accrued_net", label: "На р/с (нач.)", num: true, render: cellFmts.money , tip: "Сумма всех начислений по артикулу: столько Ozon реально перечислит. Считается точнее, чем детализация продаж."},
  { k: "accrued_diff", label: "Δ нач. vs дет.", num: true, render: cellFmts.moneyCls , tip: "Начисления минус (к перечислению + услуги). Ненулевое расхождение = детализация за период неполная."},
  { k: "margin_accrued", label: "Прибыль (нач.)", num: true, render: cellFmts.moneyCls , tip: "Прибыль по начислениям = начисления на р/с − себестоимость × продано. Пусто, если по артикулу начислений не было."},
  { k: "has_detail", label: "Детализация", render: (v) => v ? "да" : "нет" , tip: "Есть ли строки детализации продаж по товару за окно. «нет» — доверять можно только начислениям."},
  { k: "storage", label: "Хранение", num: true, render: cellFmts.moneyCls , tip: "Стоимость размещения за период по отчёту Ozon, хранится со знаком минус. В прибыли прибавляется, то есть уменьшает её как расход."},
  { k: "net_cost", label: "Себестоимость", num: true, render: cellFmts.money , tip: "Себестоимость единицы: ищем по базовому артикулу, затем по артикулу размера, затем по баркоду строки. Нет в каталоге — значение по умолчанию, строка помечена как оценка."},
  { k: "margin", label: "Прибыль", num: true, render: cellFmts.moneyCls , tip: "К перечислению + хранение (минус) − себестоимость × продано. Себестоимость списывается за гросс-продажи, возвраты уже в перечислении."},
  { k: "margin_per_one", label: "Прибыль на ед.", num: true, render: cellFmts.moneyCls , tip: "Прибыль ÷ гросс-продажи, ₽/шт. При возвратах завышает реальную прибыль с выкупленного товара."},
  { k: "margin_pct", label: "Прибыль, %", num: true, render: cellFmts.pct , tip: "Рентабельность: прибыль ÷ выручка (цена продавца × количество) × 100."},
  { k: "commission_per_one", label: "Комиссия/ед.", num: true, render: cellFmts.moneyCls , tip: "Комиссия Ozon ÷ гросс-продажи, ₽/шт."},
  { k: "services_per_one", label: "Услуги/ед.", num: true, render: cellFmts.moneyCls , tip: "Услуги Ozon ÷ гросс-продажи, ₽/шт."},
  { k: "income_per_one", label: "К перечисл./ед.", num: true, render: cellFmts.moneyCls , tip: "К перечислению ÷ гросс-продажи, ₽/шт."},
  { k: "revenue_per_one", label: "Средняя цена", num: true, render: cellFmts.moneyCls , tip: "Выручка ÷ гросс-продажи, ₽/шт."},
  { k: "return_rate", label: "Доля возвратов, %", num: true, render: cellFmts.pct , tip: "Возвраты ÷ (продажи + возвраты) × 100 за период."},
  { k: "accrued_coverage", label: "Есть начисления", render: (v) => v ? "да" : "нет" , tip: "1/0: есть ли по артикулу строки отчёта начислений Ozon за окно. Без них «Прибыль (нач.)» пустая."},
  { k: "margin_gross", label: "Маржа, до себестоимости, руб", num: true, render: cellFmts.moneyCls , tip: "К перечислению + хранение (минус) — прибыль до списания себестоимости."},
  { k: "net_cost_est", label: "Себестоимость оценка", num: true, render: cellFmts.moneyEst , tip: "Себестоимость проданного, оценённая по воронке (net_cost_est). ~ = каталожной себестоимости нет."},
  { k: "storage_per_one", label: "Хранение на ед., руб", num: true, render: cellFmts.moneyCls , tip: "Стоимость размещения ÷ гросс-продажи, ₽/шт."},
  { k: "sells_pp", label: "Пред. период: Продано, шт", num: true, render: cellFmts.int , tip: "Гросс-продажи за предыдущий период — видно при включённом сравнении периодов."},
  { k: "margin_pp", label: "Пред. период: Прибыль, руб", num: true, render: cellFmts.moneyCls , tip: "Прибыль за предыдущий период — видно при включённом сравнении периодов."},
  { k: "delta_ru", label: "Δ прибыли, руб", num: true, render: cellFmts.moneyCls , tip: "Текущая прибыль минус прибыль предыдущего периода, ₽."},
  { k: "delta_pct", label: "Δ прибыли, %", num: true, render: cellFmts.signedPct , tip: "Изменение прибыли к предыдущему периоду, %. Плюс = рост."},
];
// ----------------------------------------------------- «Вид таблицы» — единый механизм
// Для каждого таба регистрируется набор настраиваемых колонок (headers и режимы).
// Наборы колонок, подписи и флаги по умолчанию (def) приходят из единого источника
// app/static/columns.json (см. loadColViews): localStorage хранит только
// пользовательские отклонения — { колонка: true/false, order, pin }.
// Состояние живёт в localStorage под ключом `<storageKey>[_<mode>]`.
const _COLVIEWS = {};
// columns.json — мастер колонок (наборы/def/группы/Excel-словари). null до fetch.
let COLS = null;
async function loadColViews() {
  try {
    const r = await fetch("/static/columns.json", { cache: "no-store" });
    if (r.ok) COLS = await r.json();
  } catch (e) { COLS = null; }
}
function registerColView(tab, cfg) { _COLVIEWS[tab] = cfg; }
function ccTab(tab) {
  const [head, ...rest] = tab.split("-");
  return head + rest.map((s) => s[0].toUpperCase() + s.slice(1)).join("");
}
function viewBtnId(tab) { return "btn" + ccTab(tab) + "View"; }
function viewMenuId(tab) { return ccTab(tab) + "ViewMenu"; }
function viewPanelId(tab) { return ccTab(tab) + "ViewPanel"; }
// Текст фильтра в панели «Вид таблицы» — по вкладке, переживает перерисовку панели.
const _COLVIEW_FILTER = {};
function setColViewFilter(tab, q) {
  _COLVIEW_FILTER[tab] = q || "";
  applyColViewFilter(tab);
}
// Скрывает строки панели, не подходящие под фильтр (перерисовку не делаем,
// иначе при наборе теряется фокус в поле). Семантика «*» — как в общем поиске.
function applyColViewFilter(tab) {
  const panel = $("#" + viewPanelId(tab));
  if (!panel) return;
  const q = _COLVIEW_FILTER[tab] || "";
  const hit = (s) => !q || likeMatch(s || "", q);
  for (const row of panel.querySelectorAll(":scope > .colview-row")) {
    row.style.display = hit(row.dataset.lbl) ? "" : "none";
  }
  let any = false;
  for (const grp of panel.querySelectorAll(".colview-grp")) {
    const titleHit = hit(grp.dataset.lbl);
    let gAny = false;
    for (const row of grp.querySelectorAll(".colview-row")) {
      const vis = titleHit || hit(row.dataset.lbl);
      row.style.display = vis ? "" : "none";
      if (vis) gAny = true;
    }
    grp.style.display = gAny ? "" : "none";
    if (gAny) any = true;
  }
  // разделители секций лишние, пока фильтр активен
  for (const hr of panel.querySelectorAll("hr")) hr.style.display = q ? "none" : "";
  const empty = panel.querySelector(".colview-filter-empty");
  if (empty) {
    const visRows = panel.querySelectorAll(":scope > .colview-row");
    let shown = any;
    for (const r of visRows) if (r.style.display !== "none") { shown = true; break; }
    empty.hidden = !q || shown;
  }
}
function colViewSet(tab, modeHint) {
  const c = _COLVIEWS[tab];
  if (!c) return null;
  const mode = modeHint || (c.mode ? c.mode() : "default");
  const set = c.sets ? c.sets[mode] : { headers: c.headers };
  if (!set) return null;
  // optional/группы — из columns.json; до fetch (или без него) все колонки видимы.
  const tm = (COLS && COLS.tabs[tab] && COLS.tabs[tab].modes[mode]) || null;
  const optional = tm
    ? tm.columns.map((x) => ({ k: x.k, label: x.label, def: x.def }))
    : set.headers.map((h) => ({ k: h.k, label: h.label, def: true }));
  return {
    key: c.storageKey + (c.mode ? "_" + mode : ""),
    headers: set.headers,
    optional,
    groups: (COLS && COLS.tabs[tab] && COLS.tabs[tab].groups) || [],
  };
}
function colViewState(tab, modeHint) {
  const set = colViewSet(tab, modeHint);
  if (!set) return null;
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(set.key)); } catch (e) { saved = null; }
  if (!saved || typeof saved !== "object") saved = {};
  // Миграция: колонка статуса Потребности в панели переименована status → status_label
  // (предыдущие сохранения с «status» не должны теряться).
  if (tab === "replenish" && "status" in saved && !("status_label" in saved)) {
    saved.status_label = saved.status;
    delete saved.status;
  }
  const st = {};
  for (const o of set.optional) st[o.k] = (o.k in saved) ? !!saved[o.k] : !!o.def;
  st.pin = Array.isArray(saved.pin)
    ? saved.pin.filter((k) => set.headers.some((h) => h.k === k))
    : set.headers.filter((h) => h.pinned).map((h) => h.k);
  st.order = Array.isArray(saved.order)
    ? saved.order.filter((k) => set.headers.some((h) => h.k === k))
    : [];
  for (const h of set.headers) if (!st.order.includes(h.k)) st.order.push(h.k);
  return st;
}
function colViewPinKeys(tab) {
  const set = colViewSet(tab);
  if (!set) return [];
  return (colViewState(tab).pin) || [];
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
  const map = new Map(headersList.map((h) => [h.k, h]));
  const out = [];
  for (const k of st.order || []) {
    const h = map.get(k);
    if (h && st[h.k] !== false) out.push(h);
  }
  for (const h of headersList) {
    if (st[h.k] !== false && !st.order.includes(h.k)) out.push(h);
  }
  return out;
}
// Параметр экспорта «cols» — только видимые колонки. Если видны все — не отправляем
// ничего (бэкенд отдаёт полный набор). extraKeys — всегда добавляемые ключи (напр.
// колонки сравнения у margin-detail). modeHint — принудительный режим (напр. wb-prices
// в свёрнутом режиме экспортируется по базовым колонкам, т.к. агрегат на сервере не считается).
function colViewParam(tab, extraKeys, modeHint) {
  const set = colViewSet(tab, modeHint);
  if (!set) return "";
  const st = colViewState(tab, modeHint);
  const optKeys = new Set(set.optional.map((o) => o.k));
  const keys = [];
  const push = (k) => {
    if (!optKeys.has(k)) { keys.push(k); return; }
    if (st[k] !== false) keys.push(k);
  };
  const done = new Set();
  for (const k of st.order || []) { done.add(k); push(k); }
  for (const h of set.headers) if (!done.has(h.k)) { done.add(h.k); push(h.k); }
  if (extraKeys) keys.push(...extraKeys);
  return keys.length ? "cols=" + keys.join(",") : "";
}
// Способ перестановки колонок в «Вид таблицы»: "drag" — перетаскивание,
// "arrows" — кнопки ◀ ▶ (прежний способ, доступен для возврата).
const COLVIEW_REORDER = "drag";

function buildColViewMenu(tab) {
  const c = _COLVIEWS[tab];
  const set = colViewSet(tab);
  const panel = $("#" + viewPanelId(tab));
  if (!set || !panel) return;
  panel.innerHTML = "";
  // Поиск по колонкам — чтобы не глазать десятки чекбоксов вручную.
  // Значение храним по вкладке: панель перерисовывается при каждом переключении
  // чекбокса, текст фильтра должен переживать это.
  const fw = document.createElement("div");
  fw.className = "colview-filter";
  const fi = document.createElement("input");
  fi.type = "search";
  fi.placeholder = "Фильтр колонок… * = любая часть";
  fi.value = _COLVIEW_FILTER[tab] || "";
  fi.setAttribute("aria-label", "Фильтр колонок в меню «Вид таблицы»");
  fi.addEventListener("click", (e) => e.stopPropagation());
  fi.addEventListener("input", (e) => setColViewFilter(tab, e.target.value));
  // Escape сбрасывает фильтр; клики/набор не должны «прокидываться» наружу —
  // панель закрывается по клику в документ.
  fi.addEventListener("keydown", (e) => { if (e.key === "Escape") { fi.value = ""; setColViewFilter(tab, ""); } });
  fw.appendChild(fi);
  panel.appendChild(fw);
  const fe = document.createElement("div");
  fe.className = "colview-filter-empty";
  fe.textContent = "Ничего не найдено";
  panel.appendChild(fe);
  const st = colViewState(tab);
  const pinSet = new Set(st.pin || []);
  const base = set.headers.filter((h) => !set.optional.some((o) => o.k === h.k));
  const onToggle = () => {
    const reload = c.reloadTab || tab;
    if (currentTab === reload) loadTab(reload);
    buildColViewMenu(tab);
  };
  const dragMode = COLVIEW_REORDER === "drag";
  const orderFor = (keys) => {
    const ord = (st.order || []).filter((k) => keys.includes(k));
    for (const k of keys) if (!ord.includes(k)) ord.push(k);
    return ord;
  };
  // Можно ли колонки a и b менять местами: только в рамках одной секции меню
  // (базовые/необязательные у плоских видов; в групповых — в пределах своей группы).
  const sameGrp = (a, b) => {
    const ta = set.optional.some((o) => o.k === a);
    const tb = set.optional.some((o) => o.k === b);
    if (ta !== tb) return false;
    if (!set.groups.length) return true;
    const gi = (kk) => { for (let i = 0; i < set.groups.length; i++) if (set.groups[i].keys.includes(kk)) return i; return -1; };
    const ia = gi(a), ib = gi(b);
    return ia !== -1 && ia === ib;
  };
  const addMoves = (row, k) => {
    const mw = document.createElement("span");
    mw.className = "colview-mv";
    const mk = (txt, dir) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn small";
      b.textContent = txt;
      b.title = dir < 0 ? "Сдвинуть влево" : "Сдвинуть вправо";
      b.addEventListener("click", (ev) => {
        ev.stopPropagation();
        const s2 = colViewState(tab);
        const o = s2.order || [];
        const i = o.indexOf(k);
        const n = i + dir;
        if (i < 0 || n < 0 || n >= o.length || !sameGrp(k, o[n])) return;
        o[i] = o[n]; o[n] = k;
        s2.order = o;
        colViewSave(tab, s2);
        buildColViewMenu(tab);
        if (currentTab === (c.reloadTab || tab)) loadTab(c.reloadTab || tab);
      });
      return b;
    };
    mw.appendChild(mk("\u25C0", -1));
    mw.appendChild(mk("\u25B6", 1));
    row.appendChild(mw);
  };
  const addDrag = (row, k) => {
    const h = document.createElement("span");
    h.className = "colview-drag";
    h.draggable = true;
    h.title = "Перетащить, чтобы менять порядок колонок";
    h.textContent = "\u2630";
    h.addEventListener("dragstart", (ev) => {
      ev.dataTransfer.effectAllowed = "move";
      ev.dataTransfer.setData("text/plain", k);
      h.classList.add("dragging");
    });
    h.addEventListener("dragend", () => {
      h.classList.remove("dragging");
      row.classList.remove("drag-over");
    });
    h.addEventListener("dragover", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      row.classList.add("drag-over");
    });
    h.addEventListener("dragleave", () => row.classList.remove("drag-over"));
    h.addEventListener("drop", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      row.classList.remove("drag-over");
      const src = ev.dataTransfer.getData("text/plain");
      if (!src || src === k) return;
      const s2 = colViewState(tab);
      if (!sameGrp(src, k)) {
        row.classList.add("drag-no");
        setTimeout(() => row.classList.remove("drag-no"), 400);
        return;
      }
      const o = s2.order || [];
      const si = o.indexOf(src);
      const ti = o.indexOf(k);
      if (si < 0 || ti < 0 || si === ti) return;
      o.splice(si, 1);
      o.splice(Math.min(o.indexOf(k), o.length), 0, src);
      s2.order = o;
      colViewSave(tab, s2);
      buildColViewMenu(tab);
      if (currentTab === (c.reloadTab || tab)) loadTab(c.reloadTab || tab);
    });
    row.appendChild(h);
  };
  const item = (k, label, visOn, pinOn) => {
    const row = document.createElement("div");
    row.className = "colview-row";
    row.dataset.lbl = label;
    const lbl = document.createElement("label");
    lbl.className = "chk";
    if (visOn !== null) {
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = visOn;
      cb.addEventListener("change", (e) => {
        st[k] = e.target.checked;
        colViewSave(tab, st);
        onToggle();
      });
      lbl.appendChild(cb);
    }
    lbl.appendChild(document.createTextNode(visOn === null ? label : " " + label));
    row.appendChild(lbl);
    if (dragMode) addDrag(row, k); else addMoves(row, k);
    if (c && c.pinnable) {
      const pinLbl = document.createElement("label");
      pinLbl.className = "chk pin";
      pinLbl.title = "Закрепить колонку — остаётся на месте при прокрутке таблицы вправо";
      const pb = document.createElement("input");
      pb.type = "checkbox";
      pb.checked = pinOn;
      pb.addEventListener("change", (e) => {
        if (e.target.checked) pinSet.add(k); else pinSet.delete(k);
        st.pin = Array.from(pinSet);
        colViewSave(tab, st);
        onToggle();
      });
      pinLbl.appendChild(pb);
      const pico = document.createElement("span");
      pico.className = "pin-ico";
      pico.textContent = "\uD83D\uDCCC";
      pinLbl.appendChild(pico);
      row.appendChild(pinLbl);
    }
    panel.appendChild(row);
  };
  const lblAll = document.createElement("label");
  lblAll.className = "chk";
  const cbAll = document.createElement("input");
  cbAll.type = "checkbox";
  cbAll.checked = set.optional.every((o) => !!st[o.k]);
  cbAll.addEventListener("change", (e) => {
    for (const o of set.optional) st[o.k] = e.target.checked;
    colViewSave(tab, st);
    onToggle();
  });
  lblAll.appendChild(cbAll);
  lblAll.appendChild(document.createTextNode(" Показать все"));
  panel.appendChild(lblAll);
  const lblReset = document.createElement("label");
  lblReset.className = "chk";
  lblReset.title = "Сбросить колонки к виду по умолчанию";
  const cbReset = document.createElement("input");
  cbReset.type = "checkbox";
  cbReset.addEventListener("change", (e) => {
    if (!e.target.checked) return;
    try {
      const base = _COLVIEWS[tab] && _COLVIEWS[tab].storageKey;
      if (base) {
        for (const k of Object.keys(localStorage)) {
          if (k === base || k.startsWith(base + "_")) localStorage.removeItem(k);
        }
      }
    } catch (err) { /* ignore */ }
    cbReset.checked = false;
    buildColViewMenu(tab);
    if (currentTab === (c.reloadTab || tab)) loadTab(c.reloadTab || tab);
  });
  lblReset.appendChild(cbReset);
  lblReset.appendChild(document.createTextNode(" По умолчанию"));
  panel.appendChild(lblReset);
  const sep = document.createElement("hr");
  sep.style.margin = "4px 0";
  panel.appendChild(sep);
  if (base.length) {
    for (const b of base) item(b.k, b.label, null, pinSet.has(b.k));
    const sep2 = document.createElement("hr");
    sep2.style.margin = "4px 0";
    panel.appendChild(sep2);
  }
  const subPin = (k) => {
    const pinLbl = document.createElement("label");
    pinLbl.className = "chk pin";
    pinLbl.title = "Закрепить колонку — остаётся на месте при прокрутке таблицы вправо";
    const pb = document.createElement("input");
    pb.type = "checkbox";
    pb.checked = pinSet.has(k);
    pb.addEventListener("change", (e) => {
      if (e.target.checked) pinSet.add(k); else pinSet.delete(k);
      st.pin = Array.from(pinSet);
      colViewSave(tab, st);
      onToggle();
    });
    pinLbl.appendChild(pb);
    const pico = document.createElement("span");
    pico.className = "pin-ico";
    pico.textContent = "\uD83D\uDCCC";
    pinLbl.appendChild(pico);
    return pinLbl;
  };
  const optByKey = new Map(set.optional.map((o) => [o.k, o]));
  if (set.groups.length) {
    for (const g of set.groups) {
      const entries = orderFor(g.keys.filter((k) => optByKey.has(k))).map((k) => optByKey.get(k));
      if (!entries.length) continue;
      const allOn = entries.every((o) => !!st[o.k]);
      const anyOn = entries.some((o) => !!st[o.k]);
      const grp = document.createElement("div");
      grp.className = "colview-grp";
      grp.dataset.lbl = g.title;
      const lbl = document.createElement("label");
      lbl.className = "chk colview-grp-lbl";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = allOn;
      cb.indeterminate = !allOn && anyOn;
      cb.addEventListener("change", (e) => {
        for (const o of entries) st[o.k] = e.target.checked;
        colViewSave(tab, st);
        onToggle();
      });
      lbl.appendChild(cb);
      lbl.appendChild(document.createTextNode(" " + g.title));
      grp.appendChild(lbl);
      const wrap = document.createElement("div");
      wrap.className = "colview-grp-items";
      for (const o of entries) {
        const row = document.createElement("div");
        row.className = "colview-row colview-sub";
        row.dataset.lbl = o.label;
        const il = document.createElement("label");
        il.className = "chk";
        const icb = document.createElement("input");
        icb.type = "checkbox";
        icb.checked = !!st[o.k];
        icb.addEventListener("change", (e) => {
          st[o.k] = e.target.checked;
          colViewSave(tab, st);
          onToggle();
        });
        il.appendChild(icb);
        il.appendChild(document.createTextNode(" " + o.label));
        row.appendChild(il);
        if (dragMode) addDrag(row, o.k); else addMoves(row, o.k);
        if (c.pinnable) row.appendChild(subPin(o.k));
        wrap.appendChild(row);
      }
      grp.appendChild(wrap);
      panel.appendChild(grp);
    }
  } else {
    for (const k of orderFor(set.optional.map((o) => o.k))) {
      const o = optByKey.get(k);
      if (o) item(o.k, o.label, !!st[o.k], pinSet.has(o.k));
    }
  }
  // панель могли перестроить при включённом фильтре — применяем его заново
  applyColViewFilter(tab);
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
  { k: "date", label: "Дата", num: true, render: cellFmts.text , tip: "Дата продажи (saleDt) строки операции WB. Список отсортирован от новых к старым."},
  { k: "article", label: "Артикул", render: cellFmts.text , tip: "Артикул продавца (vendorCode) операции. У служебных строк (логистика, хранение, возмещение) артикул пустой."},
  { k: "title", label: "Наименование", render: cellFmts.text , tip: "Название товара из этой строки отчёта WB (поле title)."},
  { k: "doc_type", label: "Тип документа", render: cellFmts.text , tip: "Тип операции. Берётся «Обоснование для оплаты», если оно заполнено, иначе «Тип документа» отчёта."},
  { k: "quantity", label: "Кол-во", num: true, render: cellFmts.int , tip: "Количество единиц в строке. У свёрнутых служебных строк (логистика/хранение) всегда 0."},
  { k: "retail_amount", label: "Реализовано", num: true, render: cellFmts.money , tip: "«Вайлдберриз реализовал Товар (Пр)» — сумма реализации строки; у строк-возвратов отрицательная."},
  { k: "commission", label: "КВВ", num: true, render: cellFmts.money , tip: "Комиссия WB (КВВ): «Вознаграждение с продаж» до вычета услуг ПВЗ, без НДС. Хранится со знаком минус."},
  { k: "for_pay", label: "К перечислению", num: true, render: cellFmts.money , tip: "«К перечислению Продавцу за реализованный Товар» — сумма к выплате WB по этой строке."},
  { k: "logistics", label: "Доставка", num: true, render: cellFmts.money , tip: "Плата за доставку покупателю (КВВ) по строке, в минус; часто приходит отдельной служебной строкой."},
  { k: "storage", label: "Хранение", num: true, render: cellFmts.money , tip: "Платёж за хранение по строке, в минус. В детализации WB такие платы идут без артикула, у товарных строк обычно 0."},
  { k: "office", label: "Склад", render: cellFmts.text , tip: "Склад WB (officeName) из строки отчёта; у служебных строк часто пусто."},
  { k: "source", label: "Источник", render: cellFmts.tag , tip: "Источник строки: excel — файл ЛК, api — finance-API WB."},
  { k: "retail_price", label: "Цена розничная", num: true, render: cellFmts.money , tip: "Розничная цена единицы в строке отчёта (retailPrice), ₽."},
  { k: "srid", label: "SRID", render: cellFmts.text , tip: "Идентификатор строки реализации (srid) из отчёта WB — по нему строка сверяется с API."},
];

const wbDetailSummaryHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text , tip: "Артикул продавца из детализации. Безартикульные строки (платы без товара) в свод не попадают."},
  { k: "title", label: "Наименование", render: cellFmts.text , tip: "Название товара из строк детализации — первое непустое по артикулу за окно."},
  { k: "sells", label: "Продано, шт", num: true, render: cellFmts.int , tip: "Нетто в штуках: товарные строки «Продажа» минус строки «Возврат». Служебные строки (логистика, хранение) не считаются."},
  { k: "returns_qty", label: "Возвращено, шт", num: true, render: cellFmts.int , tip: "Сумма количества по строкам, у которых тип операции — возврат (поиск подстроки «возврат»/«return»)."},
  { k: "revenue", label: "Реализовано", num: true, render: cellFmts.money , tip: "Σ «Вайлдберриз реализовал Товар (Пр)» по строкам артикула; строки-возвраты вычитаются обратным знаком."},
  { k: "commission", label: "Комиссия", num: true, render: cellFmts.money , tip: "Σ комиссии WB (КВВ, ppvzSalesCommission) по артикулу; возвраты вычитаются. В базе значения отрицательные."},
  { k: "for_pay", label: "К перечислению", num: true, render: cellFmts.money , tip: "Σ «К перечислению» по строкам артикула, возвраты вычитаются. Свод отсортирован по этой сумме (по убыванию)."},
  { k: "logistics", label: "Доставка", num: true, render: cellFmts.money , tip: "Платы за доставку. Безартикульные строки разнесены по весу «доставки + возврат доставки», иначе по модулю продаж."},
  { k: "delivery_count", label: "Доставок", num: true, render: cellFmts.int , tip: "Счётчик «Количество доставок» из отчёта; безартикульные строки разносятся целыми числами по тому же весу."},
  { k: "return_delivery_count", label: "Возврат доставок", num: true, render: cellFmts.int , tip: "Счётчик «Количество возврата» (обратных доставок) из отчёта, с тем же распределением по артикулам."},
  { k: "storage", label: "Хранение (оц)", num: true, render: cellFmts.money, tip: "Оценка: безартикульные платы WB разнесены по «объём × тариф × остаток»" },
  { k: "pvz_compensation", label: "ПВЗ-компенсации", num: true, render: cellFmts.money , tip: "Σ «Возмещение за выдачу и возврат товаров на ПВЗ»; безартикульные строки разнесены по весу доставок."},
  { k: "payment_services", label: "Платёжные услуги", num: true, render: cellFmts.money , tip: "Σ комиссии за интеграцию платёжных сервисов (paymentServices); безартикульные строки разнесены по весу доставок."},
  { k: "services", label: "Услуги/штрафы", num: true, render: cellFmts.money , tip: "Σ штрафов, удержаний, корректировок ВВ и возмещения логистики; безартикульные строки разнесены по весу доставок."},
  { k: "ops_count", label: "Операций", num: true, render: cellFmts.int , tip: "Число строк детализации (операций), попавших на артикул за окно."},
  { k: "sources", label: "Источник", render: cellFmts.tag , tip: "Источники строк свода: excel — файлы ЛК, api — finance-API WB. Перечисляются через запятую."},
];

const ozDetailRowHeaders = [
  { k: "date", label: "Дата", num: true, render: cellFmts.text , tip: "Дата операции — дата создания постинга (order.created_date) из отчёта Ozon."},
  { k: "posting_number", label: "Постинг", render: cellFmts.text , tip: "Номер постинга (отправления) Ozon. В своде по нему считаются уникальные «Постинги»."},
  { k: "offer_id", label: "Артикул", render: cellFmts.text , tip: "Ваш артикул Ozon (Offer ID), размер зашит в конец артикула. В своде артикул размера сворачивается в базовый."},
  { k: "name", label: "Наименование", render: cellFmts.text , tip: "Название товара из строки отчёта Ozon (item.name)."},
  { k: "sku", label: "SKU", render: cellFmts.text , tip: "SKU Ozon — идентификатор товара в системе Ozon; вместе с датой и постингом — ключ строки детализации."},
  { k: "quantity", label: "Кол-во", num: true, render: cellFmts.int , tip: "Количество единиц в строке. У строки-возврата без продажи берётся количество возврата."},
  { k: "seller_price", label: "Цена", num: true, render: cellFmts.money , tip: "Цена продавца за единицу × количество строки (seller_price_per_instance × кол-во), без скидок Ozon."},
  { k: "amount", label: "Сумма", num: true, render: cellFmts.money , tip: "Сумма продажи строки (delivery_commission.amount) — база, от которой Ozon считает комиссию."},
  { k: "commission", label: "Комиссия", num: true, render: cellFmts.moneyCls , tip: "Комиссия Ozon, всегда в минус: берётся standard_fee, а если он пуст — «к перечислению × commission_ratio»."},
  { k: "standard_fee", label: "Услуги", num: true, render: cellFmts.moneyCls , tip: "Стандартные услуги Ozon по строке (standard_fee) — расход, хранится в минус."},
  { k: "income", label: "К перечислению", num: true, render: cellFmts.money , tip: "Итог постинга по продаже (delivery_commission.total) минус сумма возврата этого же постинга."},
  { k: "return_qty", label: "Возврат, шт", num: true, render: cellFmts.int , tip: "Количество возвратов в строке (return_commission.quantity). В своде считается отдельно от «Продано»."},
  { k: "return_total", label: "Возврат, руб", num: true, render: cellFmts.money , tip: "Сумма возврата по строке (return_commission.total) — именно она вычитается из «К перечислению»."},
  { k: "source", label: "Источник", render: cellFmts.tag , tip: "Источник строки: api — прямой метод /v1/finance/realization/posting, report — фолбэк-отчёт /v1/report/realization/posting."},
  { k: "barcode", label: "Штрихкод", render: cellFmts.text , tip: "Штрихкод из строки детализации Ozon — ключ сопоставления с каталогом."},
  { k: "commission_ratio", label: "Доля комиссии", num: true, render: (v) => v == null ? "—" : fmtPct(Number(v) * 100) , tip: "Доля комиссии Ozon (доля от суммы строки), %. Используется, когда standard_fee пуст."},
];

const ozDetailSummaryHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text , tip: "Базовый артикул товара: артикулы размеров свёрнуты в один товар. В режиме «по размерам» — артикул размера."},
  { k: "size", label: "Размер", render: cellFmts.text , tip: "Размер, выделенный из артикула Ozon. Заполняется только в режиме «в разрезе размеров»."},
  { k: "sizes_count", label: "Размеров", num: true, render: cellFmts.int , tip: "Сколько разных размеров свёрнуто в строку товара. 1 — артикул без размера (безразмерный товар)."},
  { k: "offers_count", label: "Артикулов", num: true, render: cellFmts.int , tip: "Сколько карточек Ozon (offer_id) попало в строку товара."},
  { k: "name", label: "Наименование", render: cellFmts.text , tip: "Название товара из строк детализации Ozon — первое непустое по группе за окно."},
  { k: "sells", label: "Продано, шт", num: true, render: cellFmts.int , tip: "Σ количества по строкам детализации, шт. Продажи гроссом, возвраты вынесены в отдельную колонку."},
  { k: "returns_qty", label: "Возвращено, шт", num: true, render: cellFmts.int , tip: "Σ return_qty по строкам детализации, шт — возвраты не уменьшают «Продано»."},
  { k: "postings", label: "Постингов", num: true, render: cellFmts.int , tip: "Число уникальных номеров постингов среди строк артикула за окно."},
  { k: "seller_total", label: "Продажи (цена×кол-во)", num: true, render: cellFmts.money , tip: "Σ «цена продавца × количество» по строкам детализации, руб — без скидок Ozon."},
  { k: "amount", label: "Реализовано", num: true, render: cellFmts.money , tip: "Σ суммы продажи (delivery_commission.amount) — база, из которой Ozon считает комиссию."},
  { k: "commission", label: "Комиссия", num: true, render: cellFmts.moneyCls , tip: "Σ комиссии Ozon по строкам детализации. Хранится в минус, поэтому в таблице красная."},
  { k: "services", label: "Услуги", num: true, render: cellFmts.moneyCls , tip: "Σ standard_fee (стандартных услуг) по строкам детализации, в минус."},
  { k: "storage", label: "Хранение", num: true, render: cellFmts.moneyCls , tip: "Стоимость размещения на складах Ozon за окно из отчёта placement/by-products. Начисления хранятся в минус."},
  { k: "income", label: "К перечислению", num: true, render: cellFmts.money , tip: "Σ «к перечислению» по строкам детализации; возвраты по постингам уже вычтены. Свод отсортирован по этой сумме."},
  { k: "ops_count", label: "Операций", num: true, render: cellFmts.int , tip: "Число строк детализации (операций), попавших на артикул за окно."},
  { k: "buyout_sum", label: "Сумма выкупов", num: true, render: cellFmts.money , tip: "Σ сумм выкупов из отчёта /v1/finance/products/buyout. Даты у выкупов нет — суммируется вся таблица."},
  { k: "buyout_percent", label: "Выкуп, %", num: true, render: cellFmts.pct , tip: "Выкуп, %: сумма выкупов ÷ «цена × количество» (колонка «Продажи (цена×кол-во)») × 100."},
];

const ozPlacementRowHeaders = [
  { k: "date", label: "Дата", num: true, render: cellFmts.text , tip: "Дата начисления из отчёта «Стоимость размещения» (/v1/report/placement/by-products). Одна строка = один SKU на одном складе в этот день."},
  { k: "offer_id", label: "Артикул", render: cellFmts.text , tip: "Ваш артикул (offer_id) из отчёта Ozon, обычно с размером. В сводной таблице такие артикулы сворачиваются в базовый."},
  { k: "sku", label: "SKU", render: cellFmts.text , tip: "SKU Ozon — идентификатор товара в базе Ozon, не штрихкод. Связан с артикулом через детализацию продаж."},
  { k: "warehouse", label: "Склад", render: cellFmts.text , tip: "Склад из отчёта (например, «Павловская Слобода (доставка)»). Входит в ключ строки: тот же SKU на другом складе — другая строка."},
  { k: "paid_quantity", label: "Платных экз.", num: true, render: cellFmts.int , tip: "«Кол-во платных экземпляров»: объём сверх бесплатных лимитов Ozon, за который платит склад. Не всё количество товара на складе."},
  { k: "paid_volume", label: "Платный объём, мл", num: true, render: cellFmts.num , tip: "«Платный объём в миллилитрах» из отчёта. В сводной таблице по артикулу складывается по всем SKU, складам и дням."},
  { k: "storage", label: "Начислено", num: true, render: cellFmts.moneyCls , tip: "«Начисленная стоимость размещения» за день. Хранится со знаком минус (расход), поэтому суммы отрицательные и красные."},
];

const ozPlacementSummaryHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text , tip: "Базовый артикул: артикулы разных размеров свёрнуты в одну строку. В режиме «по размерам» — полный артикул."},
  { k: "size", label: "Размер", render: cellFmts.text , tip: "Размер, выделенный из артикула Ozon. Заполняется только в режиме «в разрезе размеров»."},
  { k: "sizes_count", label: "Размеров", num: true, render: cellFmts.int , tip: "Сколько разных размеров свёрнуто в строку товара. 1 — артикул без размера (безразмерный товар)."},
  { k: "offers_count", label: "Артикулов", num: true, render: cellFmts.int , tip: "Сколько карточек Ozon (offer_id) попало в строку товара."},
  { k: "name", label: "Наименование", render: cellFmts.text , tip: "Первый попавшийся полный артикул группы (обычно с размером) — служебное поле группировки, не название товара из ЛК."},
  { k: "days", label: "Дней хранения", num: true, render: cellFmts.int , tip: "Сколько уникальных дней были начисления по артикулу за окно. Не сумма дней по складам и не длина окна."},
  { k: "paid_quantity", label: "Платных экз.", num: true, render: cellFmts.int , tip: "Сумма платных экземпляров по всем SKU, складам и дням окна. В строке итогов суммируется."},
  { k: "paid_volume", label: "Платный объём, мл", num: true, render: cellFmts.num , tip: "Сумма платного объёма (мл) по всем SKU, складам и дням окна."},
  { k: "storage", label: "Начислено", num: true, render: cellFmts.moneyCls , tip: "Сумма начислений за размещение по группе, руб. Знак минус (расход). Строки отсортированы по возрастанию: больше расход — выше."},
  { k: "ops_count", label: "Операций", num: true, render: cellFmts.int , tip: "Число строк отчёта в группе = дата × SKU × склад. В строке итогов не суммируется."},
];

const ozCashflowHeaders = [
  { k: "period_begin", label: "Период с", render: cellFmts.text , tip: "Начало расчётного периода из отчёта «Движение средств» (/v1/finance/cash-flow-statement/list). Это ключ строки в базе: повторная загрузка перезаписывает период."},
  { k: "period_end", label: "Период по", render: cellFmts.text , tip: "Конец того же периода. В выборку попадают и периоды, пересекающиеся с окном: period_end ≥ дата с, period_begin ≤ дата по."},
  { k: "begin_balance", label: "Баланс на начало", num: true, render: cellFmts.money , tip: "Остаток на начало периода (begin_balance_amount) по данным Ozon. В строке итогов не суммируется — сумма остатков бессмысленна."},
  { k: "payments_amount", label: "Выплаты на р/с", num: true, render: cellFmts.moneyCls , tip: "Сумма первой выплаты из массива payments (payments[0].payment). Хранится отрицательной: «фактически получено» в шапке = −сумма колонки."},
  { k: "delivery_total", label: "Логистика", num: true, render: cellFmts.money , tip: "Итог блока delivery отчёта за период (delivery.total), без разбивки по услугам. Знак берётся из отчёта Ozon без пересчёта."},
  { k: "return_total", label: "Возвраты", num: true, render: cellFmts.moneyCls , tip: "Итог блока return (return.total) — возвраты покупателей за период, сумма возвратов покупателю."},
  { k: "services_total", label: "Услуги", num: true, render: cellFmts.moneyCls , tip: "Итог блока services (services.total): агрегированная сумма платных услуг Ozon за период, детализация по видам услуг здесь отсутствует."},
  { k: "others_total", label: "Прочее", num: true, render: cellFmts.moneyCls , tip: "Итог блока others (others.total) — прочие операции и удержания, не попавшие в доставку, возвраты и услуги."},
  { k: "end_balance", label: "Баланс на конец", num: true, render: cellFmts.money , tip: "Остаток на конец периода (end_balance_amount). Не суммируется в итогах и не участвует в расчёте «фактически получено»."},
];

// Колонки маржинальной воронки. Порядок = порядок по умолчанию; видимость по
// умолчанию (def) — из columns.json (dicts["_FUNNEL_COL_DEFAULTS"] → margin-funnel).
const funnelHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text , tip: "Артикул поставщика в срезе воронки продаж WB. Один товар — одна строка, независимо от числа размеров."},
  { k: "name", label: "Наименование", render: cellFmts.text , tip: "Берётся из воронки (поле title), при отсутствии — из каталога товаров. Смотрите также колонку «Название из воронки»."},
  { k: "views", label: "Просмотры", num: true, render: cellFmts.int , tip: "Просмотры карточки за срез воронки (openCount). Знаменатель всех наших конверсий в этой таблице."},
  { k: "opens", label: "Открытия", num: true, render: cellFmts.int , tip: "Открытия карточки (openCardCount) — метрика WB, может отличаться от просмотров."},
  { k: "adds", label: "В корзину", num: true, render: cellFmts.int , tip: "Добавления в корзину (cartCount) за срез. Товар в корзине, но не заказанный — потенциальный спрос."},
  { k: "orders", label: "Заказы", num: true, render: cellFmts.int , tip: "Заказы покупателей (orderCount), включая те, что позже отменили. Факт выкупа — отдельная колонка."},
  { k: "cancelled", label: "Отмены", num: true, render: cellFmts.int , tip: "Отменённые заказы (cancelCount). Высокая доля к заказам — признак мыльного спроса или проблем с описанием."},
  { k: "buyouts", label: "Выкупы", num: true, render: cellFmts.int , tip: "Выкупы (buyoutCount): заказы, которые покупатель забрал. Это фактические продажи по воронке."},
  { k: "cart_pct", label: "В корзину, %", num: true, render: cellFmts.pct , tip: "Наш расчёт: добавления в корзину ÷ просмотры × 100 за срез."},
  { k: "order_pct", label: "Заказы, %", num: true, render: cellFmts.pct , tip: "Наш расчёт: заказы ÷ просмотры × 100. Не путать с конверсией, посчитанной WB в колонках conv_*."},
  { k: "avg_price", label: "Ср. цена", num: true, render: cellFmts.money , tip: "Средняя цена заказа из воронки (avgPrice). Если выручка пустая, она же используется для оценки выручки."},
  { k: "revenue", label: "Выручка (оценка)", num: true, render: cellFmts.money , tip: "Сумма заказов из воронки (orderSum). Если она нулевая, берётся ср. цена × заказы. Расходов WB в воронке нет."},
  { k: "net_cost", label: "Себестоимость", num: true, render: cellFmts.money , tip: "Себестоимость единицы из каталога товаров по этому артикулу."},
  { k: "margin", label: "Маржа (оц.)", num: true, render: cellFmts.moneyCls , tip: "Оценка ДО расходов WB: выручка (или ср. цена × заказы) − себестоимость × заказы. Комиссия, логистика и хранение здесь не учтены."},
  { k: "margin_pct", label: "Маржа, %", num: true, render: cellFmts.pct , tip: "Оценочная маржа ÷ выручка × 100. Из-за отсутствия расходов WB завышает реальную рентабельность."},
  { k: "storage_est", label: "Хранение (оц.)", num: true, render: cellFmts.money , tip: "Оценка платы за хранение по этому артикулу: безартикульные платы WB разносятся по «объём × тариф × остаток». В маржу не входит."},
  { k: "nm_id", label: "Артикул WB", render: cellFmts.text , tip: "Код номенклатуры WB (nmId) из среза воронки — по нему WB отдаёт метрики."},
  { k: "title", label: "Название из воронки", render: cellFmts.text , tip: "Название карточки так, как оно загружено на WB. Отличие от «Наименования» = карточка переименована или каталог устарел."},
  { k: "subject_id", label: "ID предмета", render: cellFmts.text , tip: "Идентификатор предмета WB. Одинаковый subject_id у товаров одной категории — полезно для группировки."},
  { k: "subject_name", label: "Предмет", render: cellFmts.text , tip: "Предмет WB (например «Носки») — категория карточки."},
  { k: "brand_name", label: "Бренд", render: cellFmts.text , tip: "Бренд по данным карточки WB в срезе воронки."},
  { k: "tags", label: "Теги", render: cellFmts.text , tip: "Теги карточки, которые WB отдаёт в срезе воронки. Пусто, если продавец теги не проставил."},
  { k: "add_to_wishlist", label: "В избранное", num: true, render: cellFmts.int , tip: "Добавления в избранное/закладки за срез (addToWishlist). Метрика интереса без заказа."},
  { k: "avg_orders_per_day", label: "Заказов в день", num: true, render: numDec(2) , tip: "Среднее число заказов в день по карточке за срез, метрика WB."},
  { k: "cancel_sum", label: "Отмены, руб", num: true, render: cellFmts.money , tip: "Сумма отменённых заказов в рублях (cancelSum) за срез."},
  { k: "buyout_sum", label: "Сумма выкупов, руб", num: true, render: cellFmts.money , tip: "Сумма выкупленных заказов в рублях (buyoutSum) — фактические деньги воронки."},
  { k: "share_order_percent", label: "Доля заказов, %", num: true, render: cellFmts.pct , tip: "Доля заказов карточки в заказах её категории, метрика WB. Показывает позицию среди конкурентов."},
  { k: "conv_to_cart_percent", label: "В корзину (воронка WB), %", num: true, render: cellFmts.pct , tip: "Конверсия в корзину, посчитанная WB (addToCartPercent). Отличается от нашей «В корзину, %» из-за другой базы знаменателя."},
  { k: "conv_cart_to_order_percent", label: "Корзина→Заказ, %", num: true, render: cellFmts.pct , tip: "Конверсия корзина→заказ по расчёту WB (cartToOrderPercent). Низкая — обычно вопрос цены или описания."},
  { k: "conv_buyout_percent", label: "Выкуп, %", num: true, render: cellFmts.pct , tip: "Доля выкупов среди заказов по расчёту WB (buyoutPercent). Главный сигнал качества спроса."},
  { k: "localization_percent", label: "Локализация, %", num: true, render: cellFmts.pct , tip: "Доля локализованного товара, метрика WB. Выше — лучше позиции в выдаче."},
  { k: "stock_wb", label: "Остаток WB, шт", num: true, render: cellFmts.int , tip: "Остаток на складах WB на момент среза воронки (product.stocks.wb). Может отличаться от данных «ВБ Остатки»."},
  { k: "stock_mp", label: "Остаток МП, шт", num: true, render: cellFmts.int , tip: "Остаток, который WB отдаёт по карточке (product.stocks.mp) — снимок на дату среза."},
  { k: "stock_balance_sum", label: "Остаток (баланс), руб", num: true, render: cellFmts.money , tip: "Оценка стоимости остатка в рублях, которую WB рассчитывает по своим тарифам (stocks.balanceSum). Не равна нашей себестоимости."},
  { k: "product_rating", label: "Рейтинг товара", num: true, render: numDec(1) , tip: "Рейтинг карточки WB по шкале 1..5 — в основном по качеству и поставкам. 0 = данных нет."},
  { k: "feedback_rating", label: "Рейтинг отзывов", num: true, render: numDec(2) , tip: "Средняя оценка по отзывам покупателей 1..5 из воронки. Используется автопилотом: ценный товар не скидываем."},
  { k: "time_to_ready_min", label: "До готовности, мин", num: true, render: cellFmts.int , tip: "Срок сборки заказа на складе WB, переведённый в минуты (дни×1440 + часы×60 + минуты). Показывает скорость отгрузок."},
  { k: "wb_club_order_count", label: "WB Клуб: заказы", num: true, render: cellFmts.int , tip: "Заказы, оформленные с подпиской WB Клуб (блок wbClub воронки). Обычно дороже и стабильнее."},
  { k: "wb_club_order_sum", label: "WB Клуб: заказы, руб", num: true, render: cellFmts.money , tip: "Сумма заказов WB Клуб в рублях за срез."},
  { k: "wb_club_buyout_count", label: "WB Клуб: выкупы", num: true, render: cellFmts.int , tip: "Выкупы среди заказов WB Клуб. Разница с заказами — отмены и невыкупы."},
  { k: "wb_club_buyout_sum", label: "WB Клуб: выкупы, руб", num: true, render: cellFmts.money , tip: "Сумма выкупов WB Клуб в рублях за срез."},
  { k: "wb_club_cancel_count", label: "WB Клуб: отмены", num: true, render: cellFmts.int , tip: "Отменённые заказы WB Клуб за срез."},
  { k: "wb_club_cancel_sum", label: "WB Клуб: отмены, руб", num: true, render: cellFmts.money , tip: "Сумма отменённых заказов WB Клуб в рублях."},
  { k: "wb_club_avg_price", label: "WB Клуб: ср. цена", num: true, render: cellFmts.money , tip: "Средняя цена заказа у подписчиков WB Клуб — обычно выше, чем у остальных."},
  { k: "wb_club_buyout_percent", label: "WB Клуб: выкуп, %", num: true, render: cellFmts.pct , tip: "Доля выкупов среди заказов WB Клуб. Обычно выше общей конверсии — качественная аудитория."},
  { k: "wb_club_avg_orders_per_day", label: "WB Клуб: заказов в день", num: true, render: numDec(2) , tip: "Среднее число заказов в день у подписчиков WB Клуб."},
  { k: "past_views", label: "Пред. период: просмотры", num: true, render: cellFmts.int , tip: "Просмотры за предыдущий период по данным WB (statistic.past), а не пересчёт по нашей базе."},
  { k: "past_adds", label: "Пред. период: в корзину", num: true, render: cellFmts.int , tip: "Добавления в корзину за предыдущий период по данным WB."},
  { k: "past_orders", label: "Пред. период: заказы", num: true, render: cellFmts.int , tip: "Заказы за предыдущий период по данным WB."},
  { k: "past_cancelled", label: "Пред. период: отмены", num: true, render: cellFmts.int , tip: "Отмены за предыдущий период по данным WB."},
  { k: "past_buyouts", label: "Пред. период: выкупы", num: true, render: cellFmts.int , tip: "Выкупы за предыдущий период по данным WB."},
  { k: "past_revenue", label: "Пред. период: выручка", num: true, render: cellFmts.money , tip: "Сумма заказов за предыдущий период по данным WB."},
  { k: "past_buyout_sum", label: "Пред. период: выкуп, руб", num: true, render: cellFmts.money , tip: "Сумма выкупов за предыдущий период по данным WB."},
  { k: "past_cancel_sum", label: "Пред. период: отмены, руб", num: true, render: cellFmts.money , tip: "Сумма отмен за предыдущий период по данным WB."},
  { k: "past_avg_price", label: "Пред. период: ср. цена", num: true, render: cellFmts.money , tip: "Средняя цена заказа за предыдущий период по данным WB."},
  { k: "dy_views", label: "Динамика просмотров, %", num: true, render: cellFmts.signedPct , tip: "Изменение просмотров к предыдущему периоду в процентах — метрика WB, плюс = рост."},
  { k: "dy_adds", label: "Динамика корзины, %", num: true, render: cellFmts.signedPct , tip: "Изменение добавлений в корзину к предыдущему периоду, % — метрика WB."},
  { k: "dy_orders", label: "Динамика заказов, %", num: true, render: cellFmts.signedPct , tip: "Изменение числа заказов к предыдущему периоду, % — метрика WB."},
  { k: "dy_cancelled", label: "Динамика отмен, %", num: true, render: cellFmts.signedPct , tip: "Изменение числа отмен к предыдущему периоду, % — метрика WB."},
  { k: "dy_buyouts", label: "Динамика выкупов, %", num: true, render: cellFmts.signedPct , tip: "Изменение числа выкупов к предыдущему периоду, % — метрика WB."},
  { k: "dy_revenue", label: "Динамика выручки, %", num: true, render: cellFmts.signedPct , tip: "Изменение суммы заказов к предыдущему периоду, % — метрика WB."},
  { k: "dy_avg_price", label: "Динамика ср. цены, %", num: true, render: cellFmts.signedPct , tip: "Изменение средней цены заказа к предыдущему периоду, % — метрика WB."},
];

async function renderMarginFunnel(p) {
  const data = await api("/margin/funnel" + p);
  pagedTable($("#marginFunnelTable"), colViewHeaders("margin-funnel", funnelHeaders), data.rows || [], null, null, colViewPinKeys("margin-funnel"));
  const cp = colViewParam("margin-funnel");
  $("#exportMarginFunnel").href = "/api/export/margin/funnel" + p + (cp ? (p ? "&" : "?") + cp : "");
  const msg = statusEl();
  const warn = document.getElementById("marginFunnelPeriodWarn");
  if (data.snapshot_from && data.snapshot_to) {
    msg.textContent = "Срез воронки за " + data.snapshot_from + " … " + data.snapshot_to +
      (data.matched ? "" : " (запрошено " + data.date_from + " — " + data.date_to + ")");
    if (warn) {
      if (!data.matched && data.rows && data.rows.length) {
        warn.style.display = "block";
        warn.textContent = "В базе нет среза точно за " + data.date_from + " — " + data.date_to
          + ". Показан срез " + data.snapshot_from + " — " + data.snapshot_to
          + " (итоги — по нему). Чтобы увидеть свой период, нажмите «Обновить базу» в шапке.";
      } else {
        warn.style.display = "none";
        warn.textContent = "";
      }
    }
  } else {
    msg.textContent = "Нет данных. Сначала скачайте WB API ▸ Воронка продаж.";
    if (warn) { warn.style.display = "none"; warn.textContent = ""; }
  }
}

async function renderMarginDetail(p) {
  const compare = /compare=1/.test(p || "");
  const data = await api("/margin/detail" + p);
  let headers = colViewHeaders("margin-detail", marginHeaders);
  if (compare) {
    headers = headers.concat([
      { k: "sells_pp", label: "Пред. период: Продано, шт", num: true, render: cellFmts.int },
      { k: "margin_pp", label: "Пред. период: Прибыль, руб", num: true, render: cellFmts.moneyCls },
      { k: "delta_ru", label: "Δ прибыли, руб", num: true, render: cellFmts.moneyCls },
      { k: "delta_pct", label: "Δ прибыли, %", num: true, render: cellFmts.signedPct },
    ].filter((h) => !headers.some((x) => x.k === h.k)));
  }
  pagedTable($("#marginDetailTable"), headers, data.rows || [], data.totals, null, colViewPinKeys("margin-detail"));
  const exportBtn = $("#exportMarginDetail");
  if (exportBtn) {
    const cp = colViewParam("margin-detail", compare ? ["sells_pp", "margin_pp", "delta_ru", "delta_pct"] : null);
    exportBtn.dataset.url = "/api/export/margin/detail" + p + (cp ? (p ? "&" : "?") + cp : "");
  }
  // Статус один на весь раздел: сначала писалось «сравнение: …», следом
  // «Строк: N» — второе затирало первое. Теперь собираем одну строку.
  const cmpMsg = (compare && data.prev_window) ?
    "сравнение: " + data.prev_window.date_from + " … " + data.prev_window.date_to :
    (compare ? "для сравнения нужны даты «С» и «По»" : "");
  const tail = cmpMsg ? " · " + cmpMsg : "";
  if ((data.rows || []).length === 0) {
    setStatus("Нет данных. Финансовый отчёт WB скачивается отдельным ключом (finance): WB API ▸ Детализация продаж. " +
      "Запрос редкий (1 в ~12 ч), отчёт формируется на вчерашний день." + tail);
    return;
  }
  setStatus("Строк: " + fmt((data.rows || []).length) + tail);
}

async function renderMarginOzonDetail(p) {
  const compare = /compare=1/.test(p || "");
  const bySize = ozBySize("margin-ozon-detail");
  const data = await api("/margin/ozon-detail" + ozBySizeParam("margin-ozon-detail", p));
  let headers = colViewHeaders("margin-ozon-detail", ozonMarginHeaders);
  if (compare) {
    headers = headers.concat([
      { k: "sells_pp", label: "Пред. период: Продано, шт", num: true, render: cellFmts.int },
      { k: "margin_pp", label: "Пред. период: Прибыль, руб", num: true, render: cellFmts.moneyCls },
      { k: "delta_ru", label: "Δ прибыли, руб", num: true, render: cellFmts.moneyCls },
      { k: "delta_pct", label: "Δ прибыли, %", num: true, render: cellFmts.signedPct },
    ].filter((h) => !headers.some((x) => x.k === h.k)));
  }
  pagedTable($("#marginOzonDetailTable"), headers, data.rows || [], data.totals, null, colViewPinKeys("margin-ozon-detail"));
  const exportBtn = $("#exportMarginOzonDetail");
  if (exportBtn) {
    const cp = colViewParam("margin-ozon-detail", compare ? ["sells_pp", "margin_pp", "delta_ru", "delta_pct"] : null);
    const ep = ozBySizeParam("margin-ozon-detail", p);
    exportBtn.dataset.url = "/api/export/margin/ozon-detail" + ep + (cp ? (ep ? "&" : "?") + cp : "");
  }
  const cmpMsg = (compare && data.prev_window) ?
    "сравнение: " + data.prev_window.date_from + " … " + data.prev_window.date_to :
    (compare ? "для сравнения нужны даты «С» и «По»" : "");
  const tail = cmpMsg ? " · " + cmpMsg : "";
  if ((data.rows || []).length === 0) {
    // Раньше здесь был зашитый текст с периодом 2026-02-21 … 2026-08-30:
    // он не зависел ни от выбранного окна, ни от базы и сбивал с толку.
    setStatus(emptyPeriodReason(data, "детализации продаж Ozon",
      "Обновите детализацию: «Обновить базу» → OZON API ▸ Детализация продаж.") + tail);
    return;
  }
  let m = "Строк: " + fmt((data.rows || []).length) +
    (bySize ? " (в разрезе размеров)" : " (по товарам)") +
    (data.estimated ? " (оценка себестоимости: " + data.estimated + ")" : "");
  if (data.cashflow_received != null) {
    m += " · на р/с фактически получено: " + fmtMoney(data.cashflow_received) +
      " (движение средств, " + fmt(data.cashflow_periods || 0) + " пер.)";
    if (data.cashflow_ratio != null) {
      m += " · от начислений: " + fmt(data.cashflow_ratio) + "%";
    }
  }
  if (data.accrued_total != null) {
    m += " · по начислениям: " + fmtMoney(data.accrued_total) +
      " (артикулов с данными: " + fmt(data.accrued_rows || 0) + ")";
    const un = (data.accrued_other || 0) + (data.accrued_unmapped || 0);
    if (un) {
      m += " · нераспределено: " + fmtMoney(un) +
        " (прочее: " + fmtMoney(data.accrued_other || 0) +
        ", без артикула: " + fmtMoney(data.accrued_unmapped || 0) + ")";
    }
  }
  setStatus(m + tail);
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
    const q = e.target.value;
    draw(data.rows.filter((r) => likeMatch(r.article + " " + (r.name || ""), q)));
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
  { k: "name", label: "Наименование", render: cellFmts.text , tip: "Контрагент из справочника «Наш склад». Служит ключом при импорте: при совпадении названия существующий контрагент обновляется, а не создаётся заново."},
  { k: "ctype", label: "Тип", render: (v) => v == null ? "—" : (whCpLabels[v] || v) , tip: "Тип контрагента: поставщик, покупатель, маркетплейс, перевозчик, другое. WB и Ozon заведены как «маркетплейс» — отгрузка в их сторону это передача товара на склад."},
  { k: "inn", label: "ИНН", render: cellFmts.text , tip: "ИНН контрагента, подтягивается из Excel при импорте справочника. Поле необязательное, служит для сверки."},
  { k: "phone", label: "Телефон", render: cellFmts.text , tip: "Контактный телефон контрагента из справочника."},
  { k: "note", label: "Примечание", render: cellFmts.text , tip: "Произвольный комментарий из справочника (например, условия поставки). В расчётах не участвует."},
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
  { k: "date", label: "Дата", render: cellFmts.text , tip: "Дата документа из шапки Excel-файла (при отсутствии — дата импорта). Документы сортируются по дате по убыванию."},
  { k: "doc_num", label: "№ документа", render: cellFmts.text , tip: "Номер документа. Строки Excel группируются в один документ по связке «дата + № + контрагент»."},
  { k: "counterparty", label: "Контрагент", render: cellFmts.text , tip: "Название контрагента из справочника. Пусто, если в файле колонка контрагента не заполнена (для отгрузок это допустимо)."},
  { k: "total", label: "Сумма", num: true, render: cellFmts.money , tip: "Сумма документа = Σ (количество × цена) по его строкам. Пересчитывается при каждом импорте и редактировании."},
  { k: "items_count", label: "Строк", num: true, render: cellFmts.int , tip: "Сколько товарных позиций в документе. При повторном импорте строки заменяются целиком."},
  { k: "source", label: "Источник", render: cellFmts.text , tip: "Откуда создан документ: excel (импорт файла), ui (создан в интерфейсе), disk (с Яндекс.Диска)."},
  { k: "_d", label: "", render: (v, r) => r.id ? '<button class="btn small" data-doc-id="' + r.id + '">Строки</button>' : "" , tip: "Кнопка открывает товарные позиции документа. Изменение строк здесь меняет остатки и пересчитывает себестоимость."},
];

const whDocItemHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text , tip: "Артикул товара. Именно по нему накапливаются приходы и отгрузки, от него считается себестоимость."},
  { k: "name", label: "Наименование", render: cellFmts.text , tip: "Наименование подставляется из каталога товаров на момент импорта. Если товара нет в каталоге — остаётся пустым."},
  { k: "quantity", label: "Кол-во", num: true, render: (v) => v == null ? "—" : fmtFloat(v, 0) , tip: "Количество единиц в строке. Может быть дробным (например, для сборных комплектов); строки с нулевым количеством при импорте отбрасываются."},
  { k: "price", label: "Цена", num: true, render: cellFmts.money2 , tip: "Цена за единицу из документа. Приход — закупочная, отгрузка — заложенная в расчёт себестоимости."},
  { k: "amount", label: "Сумма", num: true, render: cellFmts.money2 , tip: "Количество × цена, ₽. Суммы строк дают итог документа в колонке «Сумма»."},
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
  { k: "article", label: "Артикул", render: cellFmts.text , tip: "Артикул из документов «Нашего склада». Строка появляется, если по артикулу был хоть один приход или отгрузка."},
  { k: "name", label: "Наименование", render: cellFmts.text , tip: "Наименование из каталога товаров на момент формирования выборки."},
  { k: "start_qty", label: "Начальный", num: true, render: (v) => v == null ? "—" : fmtFloat(v, 0) , tip: "Входящий остаток из справочника «Наш склад → Остатки» (custom_stock) — то, что было до первого прихода."},
  { k: "received", label: "Приход", num: true, render: (v) => v == null ? "—" : fmtFloat(v, 0) , tip: "Сумма количеств по всем документам типа «Приход» за всю историю, а не за период."},
  { k: "shipped", label: "Отгрузка", num: true, render: (v) => v == null ? "—" : fmtFloat(v, 0) , tip: "Сумма количеств по всем документам типа «Отгрузка» за всю историю, не за период."},
  { k: "balance", label: "Остаток", num: true, render: (v) => v == null ? "—" : fmtFloat(v, 0) , tip: "Начальный + приход − отгрузка, шт. Строки отсортированы по убыванию остатка."},
  { k: "avg_cost", label: "Себестоимость ед.", num: true, render: cellFmts.money2 , tip: "Средневзвешенная по приходам: (начальный остаток × его цена + Σ приход × цена) ÷ (начальный + приход). Эта же цифра пишется в каталог товаров."},
  { k: "stock_value", label: "Стоимость остатков", num: true, render: cellFmts.money , tip: "Остаток × средневзвешенная себестоимость, ₽ — сколько денег «заморожено» в этом товаре."},
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
  { k: "counterparty", label: "Контрагент", render: cellFmts.text , tip: "Контрагент из справочника. Прочерк означает документ без привязанного контрагента (обычно отгрузка без указания получателя)."},
  { k: "in_n", label: "Приход, док.", num: true, render: cellFmts.int , tip: "Сколько документов типа «Приход» заведено на этого контрагента за всю историю."},
  { k: "in_sum", label: "Приход, сумма", num: true, render: cellFmts.money , tip: "Сумма всех приходных документов контрагента, ₽ (Σ итогов документов). Это закупки, а не выручка."},
  { k: "out_n", label: "Отгрузка, док.", num: true, render: cellFmts.int , tip: "Сколько документов типа «Отгрузка» заведено на этого контрагента за всю историю."},
  { k: "out_sum", label: "Отгрузка, сумма", num: true, render: cellFmts.money , tip: "Сумма всех отгрузочных документов контрагента, ₽. Строки отсортированы по сумме прихода и отгрузки вместе."},
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

const replenishHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text, pinned: true, tip: "Артикул товара из каталога «Наш склад → Товары»." },
  { k: "name", label: "Наименование", render: (v) => (v == null || v === "") ? "—" : v, tip: "Название товара из каталога (или из детализации)." },
  { k: "actual_mp", label: "Карточка", render: (v) => (v == null || v === "") ? "—"
    : v.split(",").map((x) => MP_LABELS[x] || x).join(" / "), tip: "На каком маркетплейсе есть карточка товара. Сверка по артикулу и штрихкоду; без карточки товар неактуален." },
  { k: "status_label", label: "Статус", render: (v, r) => `<span class="tag st-${r.status}">${v}</span>`, tip: "Срочно — есть дефицит или нужен докуп; Нет нигде — продажи есть, а остатков нет; Норма — запаса хватает; Неактуальный — нет карточки на WB/Ozon." },
  { k: "demand", label: "Спрос, шт/д", num: true, render: (v) => v == null ? "—" : fmtFloat(v, 2), tip: "Скорость продаж: (продажи − возвраты) за выбранное окно, штук в день." },
  { k: "demand_wb", label: "WB, шт/д", num: true, render: (v) => v == null ? "—" : fmtFloat(v, 2), tip: "Скорость продаж только по WB за окно, штук в день (продажи − возвраты)." },
  { k: "demand_oz", label: "Ozon, шт/д", num: true, render: (v) => v == null ? "—" : fmtFloat(v, 2), tip: "Скорость продаж только по Ozon за окно, штук в день (продажи − возвраты)." },
  { k: "wb_sells", label: "Продано WB, шт", num: true, render: (v) => v == null ? "—" : fmt(v), tip: "Сколько штук продано на WB за выбранное окно, шт, нетто (продажи − возвраты). Продано WB ÷ дни окна = «WB, шт/д»." },
  { k: "return_rate", label: "Возвраты, %", num: true, render: (v) => v == null ? "—" : fmtPct(v), tip: "Доля возвратов: возвраты ÷ (продажи + возвраты) за окно, %." },
  { k: "our_stock", label: "У нас, шт", num: true, render: (v) => v == null ? "—" : fmt(v), tip: "Остаток на нашем складе: начальный + приход − отгрузка по документам." },
  { k: "our_cost", label: "Себест-ть", num: true, render: cellFmts.money, tip: "Себестоимость из каталога товаров; если не задана — оценка по умолчанию." },
  { k: "wb_avail", label: "WB доступно", num: true, render: cellFmts.intZero, tip: "Доступно к продаже на WB: остатки на складах (quantity_full) + в пути." },
  { k: "wb_in_way", label: "WB в пути", num: true, render: cellFmts.intZero, tip: "Товар в пути на склады WB (уже отгружен со склада WB-поставщика или в поставке)." },
  { k: "wb_doc", label: "WB, дн", num: true, render: (v) => v == null ? "—" : fmtFloat(v, 1), tip: "Запас на WB в днях: доступно ÷ спрос в день. Идеал — целевой запас (по умолчанию 30 дн)." },
  { k: "wb_def", label: "WB дефицит", num: true, render: (v) => (v || 0) > 0 ? `<span class="pos">+${fmt(v)}</span>` : "—", tip: "Дефицит WB до целевого запаса. Считается как «Дослать» из плана подсортировки (как PDF: скорость 180 дн, +1 шт в пустой размер карточки, учёт прибыльности) — чтобы в окне без WB-продаж не было 0 при пустой карточке. Что нужно довезти на WB." },
  { k: "to_sort", label: "Дослать", num: true, render: (v) => (v || 0) > 0 ? `<span class="pos">+${fmt(v)}</span>` : "—", tip: "План подсортировки на WB, как в PDF: итог по карточке = скорость продаж 180 дн за вычетом доступного, +1 шт в пустой размер, с учётом прибыльности. То же значение, что «WB дефицит» (для артикулов с WB)." },
  { k: "oz_avail", label: "Ozon доступно", num: true, render: cellFmts.intZero, tip: "Доступно к продаже на Ozon: остатки на складах + в пути." },
  { k: "oz_in_way", label: "Ozon в пути", num: true, render: cellFmts.intZero, tip: "Товар в пути на склады Ozon." },
  { k: "oz_doc", label: "Ozon, дн", num: true, render: (v) => v == null ? "—" : fmtFloat(v, 1), tip: "Запас на Ozon в днях: доступно ÷ спрос в день. Идеал — целевой запас." },
  { k: "oz_def", label: "Ozon дефицит", num: true, render: (v) => (v || 0) > 0 ? `<span class="pos">+${fmt(v)}</span>` : "—", tip: "Дефицит Ozon до целевого запаса: целевые дни × спрос в день − доступно." },
  { k: "ship_wb", label: "Отгрузить WB", num: true, render: (v) => (v || 0) > 0 ? `<b>${fmt(v)}</b>` : "—", tip: "Сколько отгрузить с нашего склада на WB: покрытие дефицита WB. Приоритет — товару с самым низким запасом в днях, при равенстве — более маржинальному." },
  { k: "ship_oz", label: "Отгрузить Ozon", num: true, render: (v) => (v || 0) > 0 ? `<b>${fmt(v)}</b>` : "—", tip: "Сколько отгрузить с нашего склада на Ozon: покрытие дефицита Ozon (по той же логике приоритета, что и WB)." },
  { k: "need_buy", label: "Купить у поставщика", num: true, render: (v) => (v || 0) > 0 ? `<b>${fmt(v)}</b>` : "—", tip: "Сколько докупить у поставщика, чтобы общий запас (наш склад + WB + Ozon) покрывал целевой запас в днях продаж." },
  { k: "margin_per_one", label: "Маржа/шт", num: true, render: cellFmts.money, tip: "Маржа (после себестоимости и расходов маркетплейса) в расчёте на одну проданную штуку." },
  { k: "margin_pct", label: "Рент-сть, %", num: true, render: cellFmts.pct, tip: "Рентабельность: маржа ÷ сумма к перечислению, %." },
  { k: "margin", label: "Маржа, руб", num: true, render: cellFmts.money, tip: "Маржа за окно по артикулу: к перечислению − расходы маркетплейса − себестоимость проданного." },
  { k: "sells", label: "Продано, шт", num: true, render: (v) => v == null ? "—" : fmt(v), tip: "Продано за окно суммарно по WB и Ozon, шт (с учётом возвратов)." },
  { k: "returns_qty", label: "Возвраты, шт", num: true, render: (v) => v == null ? "—" : fmt(v), tip: "Возвраты покупателей за окно по WB и Ozon, шт." },
  { k: "income", label: "К перечислению, руб", num: true, render: cellFmts.money, tip: "Сумма к перечислению маркетплейсами за окно: WB + Ozon после всех удержаний." },
  { k: "oz_qty", label: "Ozon склад, шт", num: true, render: cellFmts.int, tip: "Остаток на складах Ozon (свободный + резерв) по последнему срезу." },
  { k: "wb_qty", label: "WB склад, шт", num: true, render: cellFmts.int, tip: "Остаток на складах WB (quantity) по последнему срезу, все склады суммой." },
];

const replenishSizeHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text, pinned: true, tip: "Артикул товара из каталога." },
  { k: "size", label: "Размер", render: (v) => (v == null || v === "") ? "—" : v, tip: "Технологический размер. Разрез по размерам доступен для продаж WB (tech_size) и остатков складов; у Ozon размера в продажах нет — Ozon-спрос только по артикулу." },
  { k: "barcode", label: "Штрихкод", render: cellFmts.text, tip: "Штрихкод размера: из каталога размеров («Наш склад → Товары»), отчёта WB или остатков складов маркетплейсов." },
  { k: "name", label: "Наименование", render: (v) => (v == null || v === "") ? "—" : v, tip: "Название товара из каталога (или из детализации)." },
  { k: "actual_mp", label: "Карточка", render: (v) => (v == null || v === "") ? "—"
    : v.split(",").map((x) => MP_LABELS[x] || x).join(" / "), tip: "Где есть карточка (сверка по артикулу/штрихкоду); без карточки товар неактуален." },
  { k: "status_label", label: "Статус", render: (v, r) => `<span class="tag st-${r.status}">${v}</span>`, tip: "Статус по размеру (по WB-спросу и остаткам размера; «Нет нигде» — продажи есть, остатков по размеру и на нашем складе нет)." },
  { k: "wb_sells", label: "Продано WB", num: true, render: (v) => v == null ? "—" : fmt(v), tip: "Продано на WB по этому размеру за окно, шт (нетто: продажи − возвраты)." },
  { k: "wb_vel", label: "Спрос WB, шт/д", num: true, render: (v) => v == null ? "—" : fmtFloat(v, 2), tip: "Спрос по размеру на WB: продажи (без возвратов) за окно ÷ дни, шт/день." },
  { k: "wb_avail", label: "WB доступно", num: true, render: cellFmts.intZero, tip: "Доступно к продаже на WB по этому размеру: остаток (quantity_full) + в пути." },
  { k: "wb_in_way", label: "WB в пути", num: true, render: cellFmts.intZero, tip: "В пути на склады WB по этому размеру." },
  { k: "wb_doc", label: "WB, дн", num: true, render: (v) => v == null ? "—" : fmtFloat(v, 1), tip: "Запас этого размера на WB в днях: доступно ÷ спрос в день." },
  { k: "wb_def", label: "WB дефицит", num: true, render: (v) => (v || 0) > 0 ? `<span class="pos">+${fmt(v)}</span>` : "—", tip: "Дефицит размера на WB до целевого запаса. Считается как «Дослать» из плана подсортировки (как PDF: скорость 180 дн, +1 шт в пустой размер карточки, учёт прибыльности) — чтобы пустой живой размер не показывал 0." },
  { k: "to_sort", label: "Дослать", num: true, render: (v) => (v || 0) > 0 ? `<span class="pos">+${fmt(v)}</span>` : "—", tip: "План подсортировки на WB по этому размеру, как в PDF: скорость 180 дн за вычетом доступного, +1 шт в пустой размер карточки, с учётом прибыльности. То же значение, что «WB дефицит»." },
  { k: "ship_wb", label: "Отгрузить WB", num: true, render: (v) => (v || 0) > 0 ? `<b>${fmt(v)}</b>` : "—", tip: "Сколько отгрузить этого размера с нашего склада на WB: покрытие дефицита размера; приоритет — самому низкому запасу в днях." },
  { k: "our_stock", label: "У нас, шт", num: true, render: (v) => v == null ? "—" : fmt(v), tip: "Остаток на нашем складе — по всему артикулу (по размерам склад не ведётся)." },
  { k: "margin_per_one", label: "Маржа/шт", num: true, render: cellFmts.money, tip: "Маржа/шт по артикулу целиком (по размерам не раскладывается)." },
  { k: "margin_pct", label: "Рент-сть, %", num: true, render: cellFmts.pct, tip: "Рентабельность по артикулу целиком, %." },
  { k: "wb_net", label: "Продажи WB нетто, шт", num: true, render: (v) => v == null ? "—" : fmt(v), tip: "Продажи этого размера на WB за окно нетто (продажи − возвраты, без минусовых возвратов)." },
  { k: "wb_ret", label: "Возвраты WB, шт", num: true, render: (v) => v == null ? "—" : fmt(v), tip: "Возвраты этого размера на WB за окно, шт." },
  { k: "wb_qty", label: "WB склад, шт", num: true, render: cellFmts.int, tip: "Остаток этого размера на складах WB (quantity) по последнему срезу." },
];

function replenishView() {
  const el = $("#replenishView");
  return (el && el.value) || "article";
}

function _footSum(rows, k) { return rows.reduce((a, r) => a + (Number(r[k]) || 0), 0); }

function _footAvg(rows, k, dp) {
  let s = 0, c = 0;
  for (const r of rows) {
    const n = Number(r[k]);
    if (r[k] != null && r[k] !== "" && isFinite(n)) { s += n; c++; }
  }
  if (!c) return null;
  const o = { avg: s / c };
  if (dp === "pct") o.pct = true; else if (dp != null) o.dp = dp;
  return o;
}

// Итоги вида «По артикулам»: сумма — для остатков/дефицитов/отгрузок/докупки,
// среднее (≈) — для скоростей, дней запаса, долей возвратов и маржи/шт.
function replenishFooters(rows) {
  if (!rows || !rows.length) return null;
  const t = {
    wb_sells: _footSum(rows, "wb_sells"),
    our_stock: _footSum(rows, "our_stock"),
    wb_avail: _footSum(rows, "wb_avail"), wb_in_way: _footSum(rows, "wb_in_way"),
    wb_def: _footSum(rows, "wb_def"), to_sort: _footSum(rows, "to_sort"),
    oz_avail: _footSum(rows, "oz_avail"), oz_in_way: _footSum(rows, "oz_in_way"),
    oz_def: _footSum(rows, "oz_def"),
    ship_wb: _footSum(rows, "ship_wb"), ship_oz: _footSum(rows, "ship_oz"),
    need_buy: _footSum(rows, "need_buy"), margin: _footSum(rows, "margin"),
    demand: _footAvg(rows, "demand", 2),
    demand_wb: _footAvg(rows, "demand_wb", 2), demand_oz: _footAvg(rows, "demand_oz", 2),
    return_rate: _footAvg(rows, "return_rate", "pct"),
    wb_doc: _footAvg(rows, "wb_doc", 1), oz_doc: _footAvg(rows, "oz_doc", 1),
    margin_per_one: _footAvg(rows, "margin_per_one", 2),
    margin_pct: _footAvg(rows, "margin_pct", "pct"),
  };
  Object.keys(t).forEach((k) => { if (t[k] == null) delete t[k]; });
  return t;
}

// Итоги вида «По размерам»: те же принципы — сумма/среднее.
function replenishSizeFooters(rows) {
  if (!rows || !rows.length) return null;
  const t = {
    wb_sells: _footSum(rows, "wb_sells"),
    wb_avail: _footSum(rows, "wb_avail"), wb_in_way: _footSum(rows, "wb_in_way"),
    wb_def: _footSum(rows, "wb_def"), to_sort: _footSum(rows, "to_sort"),
    ship_wb: _footSum(rows, "ship_wb"), our_stock: _footSum(rows, "our_stock"),
    margin: _footSum(rows, "margin"),
    wb_vel: _footAvg(rows, "wb_vel", 2), wb_doc: _footAvg(rows, "wb_doc", 1),
    margin_per_one: _footAvg(rows, "margin_per_one", 2),
    margin_pct: _footAvg(rows, "margin_pct", "pct"),
  };
  Object.keys(t).forEach((k) => { if (t[k] == null) delete t[k]; });
  return t;
}

function replenishQs(f) {
  const win = Math.max(1, parseInt(($("#replenishWindow") || {}).value, 10) || 30);
  const tgt = Math.max(1, parseInt(($("#replenishTarget") || {}).value, 10) || 30);
  const sortEl = $("#replenishSort");
  const fromEl = $("#fFrom");
  const toEl = $("#fTo");
  const now = new Date();
  let to = toEl && toEl.value ? toEl.value : now.toISOString().slice(0, 10);
  let from = fromEl && fromEl.value ? fromEl.value : "";
  if (!from) {
    const d = new Date(now);
    d.setDate(d.getDate() - (win - 1));
    from = d.toISOString().slice(0, 10);
  }
  const q = {
    date_from: from, date_to: to, window_days: win, target_days: tgt,
    sort: (sortEl && sortEl.value) || "urgency",
    view: replenishView(),
    show_inactive: $("#replenishShowInactive") && $("#replenishShowInactive").checked ? 1 : undefined,
    hide_zero_sizes: $("#replenishHideZeroSizes") && $("#replenishHideZeroSizes").checked ? 1 : undefined,
  };
  if (f.marketplace) q.marketplace = f.marketplace;
  const like = tabLike("replenishLike");
  if (like) q.article_like = like;
  return qs(q);
}

async function renderReplenish() {
  const box = $("#replenishTable");
  let f = {};
  try { f = filters(); } catch (e) { /* пустой объект */ }
  const p = replenishQs(f);
  const exp = $("#exportReplenish");
  if (exp) {
    const cp = colViewParam("replenish");
    exp.href = "/api/export/replenish" + p + (cp ? "&" + cp : "");
  }
  let data;
  try {
    data = await api("/replenish" + p);
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить потребность: ' + escapeHtml(err.message) + "</div>";
    return;
  }
  const isSize = replenishView() === "sizes";
  const msg = statusEl();
  if (msg) {
    const m = data.meta || {};
    const parts = [
      "Окно: " + (data.date_from || "—") + " … " + (data.date_to || "—"),
      "Запас: " + m.target_days + " дн",
      (isSize ? "Размеров: " : "Строк: ") + fmt(m.count),
      "срочно: " + (m.urgent || 0),
      "нет нигде: " + (m.nostock || 0),
      "норма: " + (m.normal || 0),
      "неактуальные (скрыты): " + (m.inactive_total || 0),
    ];
    if (!isSize) {
      parts.push("Докупить: " + fmt(m.need_total) + " шт");
    }
    parts.push(isSize ? "Отгрузить на WB: " + fmt(m.ship_total) + " шт" : "Отгрузить: " + fmt(m.ship_total) + " шт");
    const full = parts.join(" · ");
    if (full.length > 150) {
      msg.textContent = full.slice(0, 150) + "…";
      msg.dataset.tip = full;
    } else {
      msg.textContent = full;
      delete msg.dataset.tip;
    }
  }
  const headers = colViewHeaders("replenish", isSize ? replenishSizeHeaders : replenishHeaders);
  const footers = isSize ? replenishSizeFooters(data.rows || []) : replenishFooters(data.rows || []);
  pagedTable(box, headers, data.rows || [], footers, null, colViewPinKeys("replenish"));
}

// ---- Выгрузка потребности в PDF с фотографиями --------------------------
// У PDF своя раскладка карточки (фото → артикул → размеры → итог), но набор
// галочек по умолчанию берём из видимых колонок «Вида таблицы», чтобы список
// полей не расходился с экраном. Выбранное хранится отдельно от colView.
const REPLENISH_PDF_KEY = "replenishPdf";
const PDF_PHOTO_MAX = 9;

// Окна скорости продаж размера для плана подсортировки. 0 — вся история.
const PDF_VEL_WINDOWS = [
  { v: 180, label: "180 дней" },
  { v: 365, label: "365 дней" },
  { v: 0, label: "всё время" },
];
const PDF_VEL_DEFAULT = 180;

function pdfParams() {
  let s = {};
  try { s = JSON.parse(localStorage.getItem(REPLENISH_PDF_KEY) || "{}") || {}; } catch (e) { s = {}; }
  const n = parseInt(s.photoCount, 10);
  const vd = parseInt(s.velDays, 10);
  return {
    withPhotos: s.withPhotos !== false,
    photoCount: n >= 1 && n <= PDF_PHOTO_MAX ? n : 6,
    velDays: PDF_VEL_WINDOWS.some((w) => w.v === vd) ? vd : PDF_VEL_DEFAULT,
    // галка включена по умолчанию: подсорт по прибыльности полезнее плоских 1 шт
    profit: s.profit !== false,
  };
}

function savePdfParams(p) {
  try { localStorage.setItem(REPLENISH_PDF_KEY, JSON.stringify(p)); } catch (e) { /* ignore */ }
}

// Колонки для галочек: всё, кроме артикула и размера — они в карточке отдельными строками.
function replenishPdfColumns() {
  return replenishHeaders.filter((h) => h.k !== "article" && h.k !== "size");
}

function replenishPdfCols() {
  // Карточка печатает ровно те колонки, что включены в «Вид таблицы» вида
  // «По артикулам» (режим берём явно — PDF всегда по артикулам, даже когда
  // таблица сейчас открыта в разрезе «По размерам»).
  const st = colViewState("replenish", "article");
  if (!st) return replenishPdfColumns().map((h) => h.k);
  const keys = [];
  for (const h of replenishPdfColumns()) {
    if (st[h.k] !== false) keys.push(h.k);
  }
  return keys;
}

// — PDF из Excel-файла (дропзона в меню PDF) ——————————————————————————
// Файл с правками: состав карточек и атрибуты берутся из него, бюджет
// «Итого дослать» — из колонки «WB дефицит» (пустая ячейка = расчёт по
// складу), размеры/фото/коэффициенты — как в базовом режиме.
let replenishExcel = null; // {name, rows, meta} | null

function isExcelFileName(name) {
  return /\.xlsx$/i.test(String(name || "").trim());
}

function replenishExcelSummary(x) {
  const m = (x && x.meta) || {};
  const parts = [((x && x.name) || "Файл") + " — карточек: " + fmt(m.count || 0)];
  if (m.unknown && m.unknown.length) parts.push("нераспознанные колонки: " + m.unknown.join(", "));
  if (m.dropped) parts.push("без артикула: " + fmt(m.dropped));
  if (m.duplicates) parts.push("дубли колонок: " + fmt(m.duplicates));
  if (m.truncated) parts.push("обрезано строк: " + fmt(m.truncated));
  return parts.join(" · ");
}

async function replenishHttpError(resp) {
  const t = await resp.text();
  try {
    const j = JSON.parse(t);
    if (j && j.detail) return new Error(resp.status + " " + j.detail);
  } catch (e) { /* не JSON — отдаём как есть */ }
  return new Error(resp.status + " " + t);
}

async function loadReplenishExcel(file) {
  const msg = statusEl();
  const say = (t) => {
    if (!msg) return;
    msg.textContent = t;
    delete msg.dataset.tip;
  };
  if (!isExcelFileName(file.name)) {
    say("Нужен файл .xlsx — выгрузка «Потребность» (кнопка «Excel»)");
    return;
  }
  say("Читаю Excel: " + file.name + "…");
  try {
    const fd = new FormData();
    fd.append("file", file, file.name);
    const resp = await fetch("/api/replenish/import-excel", { method: "POST", body: fd });
    if (!resp.ok) throw await replenishHttpError(resp);
    const data = await resp.json();
    if (!data || !data.rows || !data.rows.length) {
      throw new Error("в файле нет строк с артикулом");
    }
    replenishExcel = { name: file.name, rows: data.rows, meta: data.meta || {} };
    buildReplenishPdfMenu();
    say(replenishExcelSummary(replenishExcel));
  } catch (err) {
    replenishExcel = null;
    buildReplenishPdfMenu();
    say("Excel не загрузился: " + err.message);
  }
}

function buildReplenishPdfMenu() {
  const panel = $("#replenishPdfPanel");
  if (!panel) return;
  panel.innerHTML = "";
  const p = pdfParams();

  const head = document.createElement("label");
  head.className = "chk";
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = p.withPhotos;
  head.appendChild(cb);
  head.appendChild(document.createTextNode(" С фото"));
  panel.appendChild(head);

  const cntRow = document.createElement("label");
  cntRow.className = "chk";
  const sel = document.createElement("select");
  sel.title = "Сколько фотографий выводить на карточку товара — первые по номеру в имени файла";
  for (let i = 1; i <= PDF_PHOTO_MAX; i++) {
    const o = document.createElement("option");
    o.value = String(i);
    o.textContent = i + " фото";
    if (i === p.photoCount) o.selected = true;
    sel.appendChild(o);
  }
  sel.addEventListener("change", (e) => {
    p.photoCount = parseInt(e.target.value, 10) || 6;
    savePdfParams(p);
  });
  cntRow.appendChild(document.createTextNode(" Фото на товар: "));
  cntRow.appendChild(sel);
  panel.appendChild(cntRow);

  const velRow = document.createElement("label");
  velRow.className = "chk";
  const velSel = document.createElement("select");
  velSel.title = "За какое окно считать скорость продаж размера. Короткое окно даёт "
    + "нулевую скорость у размеров, которые давно не продавались; «всё время» "
    + "полезно для сезонных товаров";
  for (const w of PDF_VEL_WINDOWS) {
    const o = document.createElement("option");
    o.value = String(w.v);
    o.textContent = w.label;
    if (w.v === p.velDays) o.selected = true;
    velSel.appendChild(o);
  }
  velSel.addEventListener("change", (e) => {
    const v = parseInt(e.target.value, 10);
    p.velDays = PDF_VEL_WINDOWS.some((w) => w.v === v) ? v : PDF_VEL_DEFAULT;
    savePdfParams(p);
  });
  velRow.appendChild(document.createTextNode(" Скорость по: "));
  velRow.appendChild(velSel);
  panel.appendChild(velRow);

  // Галка прибыльности меняет смысл покрытия, поэтому гасим «Запас»:
  // он при включённой галке не используется, и оставлять его активным
  // с этим значением вводит в заблуждение.
  const profitRow = document.createElement("label");
  profitRow.className = "chk";
  const profitCb = document.createElement("input");
  profitCb.type = "checkbox";
  profitCb.checked = p.profit;
  profitCb.title = "Держать на WB четверть выбранного периода продаж и домножить "
    + "цель на коэффициент по рентабельности: убыточный товар (рентабельность ≤ 0) "
    + "не подсортировывается совсем, 15% → ×1.0, 30% и выше → ×2.0. Коэффициент "
    + "считается по товару целиком: у размеров своей себестоимости нет. Выключенная "
    + "галка возвращает покрытие из поля «Запас» и подсорт 1 шт в пустые размеры";
  const syncTargetField = () => {
    const t = $("#replenishTarget");
    if (!t) return;
    t.disabled = profitCb.checked;
    t.title = profitCb.checked
      ? "Пока включена галка «Учитывать прибыльность», покрытие = период / 4"
      : "Сколько дней продаж держать на WB";
    t.parentElement && t.parentElement.classList.toggle("off", profitCb.checked);
  };
  profitCb.addEventListener("change", (e) => {
    p.profit = e.target.checked;
    savePdfParams(p);
    syncTargetField();
  });
  profitRow.appendChild(profitCb);
  profitRow.appendChild(document.createTextNode(" Учитывать прибыльность"));
  panel.appendChild(profitRow);
  syncTargetField();

  const syncCountSel = () => {
    sel.disabled = !cb.checked;
    cntRow.classList.toggle("off", !cb.checked);
  };
  cb.addEventListener("change", (e) => {
    p.withPhotos = e.target.checked;
    savePdfParams(p);
    syncCountSel();
  });
  syncCountSel();

  const sep = document.createElement("hr");
  sep.style.margin = "4px 0";
  panel.appendChild(sep);

  const hint = document.createElement("div");
  hint.textContent = "Поля карточки — колонки во «Вид таблицы».";
  hint.style.fontSize = "12px";
  hint.style.color = "#666";
  hint.title = "Карточка печатает колонки, включённые во «Вид таблицы» для вида "
    + "«По артикулам»: включили колонку — она есть и в PDF. Артикул и размер "
    + "печатаются всегда.";
  panel.appendChild(hint);

  // Дропзона Excel / карточка уже загруженного файла.
  const xrow = document.createElement("div");
  if (replenishExcel) {
    xrow.className = "pdf-x";
    const info = document.createElement("div");
    info.className = "pdf-x-info";
    info.textContent = replenishExcelSummary(replenishExcel);
    info.title = "PDF соберётся из файла: состав карточек и атрибуты — из него, "
      + "бюджет «Итого дослать» — из колонки «WB дефицит» (пустая ячейка = расчёт "
      + "по складу), размеры, фото и коэффициенты — как обычно";
    const clr = document.createElement("button");
    clr.type = "button";
    clr.className = "btn pdf-x-clear";
    clr.textContent = "×";
    clr.title = "Убрать файл и снова формировать PDF из таблицы";
    clr.addEventListener("click", () => {
      replenishExcel = null;
      buildReplenishPdfMenu();
      const m = statusEl();
      if (m) {
        m.textContent = "PDF снова из таблицы";
        delete m.dataset.tip;
      }
    });
    xrow.appendChild(info);
    xrow.appendChild(clr);
  } else {
    xrow.className = "pdf-drop-wrap";
    const dz = document.createElement("div");
    dz.className = "pdf-drop";
    dz.textContent = "Excel с правками: перетащите файл сюда или нажмите";
    dz.title = "Файл .xlsx — выгрузка «Потребность», отредактированная в Excel: "
      + "удалили карточки, поправили количества — PDF соберётся из файла. "
      + "Фото и размеры считаются, как обычно";
    const inp = document.createElement("input");
    inp.type = "file";
    inp.accept = ".xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
    inp.className = "hidden";
    dz.addEventListener("click", () => inp.click());
    dz.addEventListener("dragover", (e) => {
      e.preventDefault();
      dz.classList.add("over");
    });
    dz.addEventListener("dragleave", () => dz.classList.remove("over"));
    dz.addEventListener("drop", (e) => {
      e.preventDefault();
      dz.classList.remove("over");
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) loadReplenishExcel(f);
    });
    inp.addEventListener("change", (e) => {
      const f = e.target.files && e.target.files[0];
      if (f) loadReplenishExcel(f);
      e.target.value = ""; // чтобы тот же файл можно было выбрать повторно
    });
    xrow.appendChild(dz);
    xrow.appendChild(inp);
  }
  panel.appendChild(xrow);

  const go = document.createElement("button");
  go.type = "button";
  go.className = "btn";
  go.textContent = replenishExcel ? "Скачать PDF из Excel" : "Скачать PDF";
  go.addEventListener("click", () => {
    panel.classList.add("hidden");
    downloadReplenishPdf();
  });
  panel.appendChild(go);
}

function initReplenishPdfMenu() {
  const btn = $("#btnReplenishPdf");
  const menu = $("#replenishPdfMenu");
  const panel = $("#replenishPdfPanel");
  if (!btn || !menu || !panel) return;
  menu.addEventListener("click", (e) => e.stopPropagation());
  // Падение файла мимо дропзоны не должно открывать его новой вкладкой.
  const isFileDrag = (e) => e.dataTransfer
    && Array.prototype.indexOf.call(e.dataTransfer.types || [], "Files") >= 0;
  document.addEventListener("dragover", (e) => { if (isFileDrag(e)) e.preventDefault(); });
  document.addEventListener("drop", (e) => { if (isFileDrag(e)) e.preventDefault(); });
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    const wasHidden = panel.classList.contains("hidden");
    const vpanel = $("#replenishViewPanel");
    if (vpanel) vpanel.classList.add("hidden");
    if (wasHidden) {
      buildReplenishPdfMenu();
      panel.classList.remove("hidden");
    } else {
      panel.classList.add("hidden");
    }
  });
  document.addEventListener("click", () => panel.classList.add("hidden"));
}

function replenishPdfUrl() {
  let f = {};
  try { f = filters(); } catch (e) { /* пустой объект */ }
  const q = replenishQs(f);
  const params = new URLSearchParams(q.charAt(0) === "?" ? q.slice(1) : q);
  params.delete("view"); // карточка всегда по артикулам, размеры подтягиваются отдельно
  const p = pdfParams();
  params.set("with_photos", p.withPhotos ? 1 : 0);
  params.set("photo_count", p.photoCount);
  params.set("vel_days", p.velDays);
  params.set("profit", p.profit ? 1 : 0);
  const cols = replenishPdfCols().join(",");
  if (cols) params.set("cols", cols);
  return "/api/export/replenish/pdf?" + params.toString();
}

async function downloadReplenishPdf() {
  const msg = statusEl();
  const say = (t) => {
    if (!msg) return;
    msg.textContent = t;
    delete msg.dataset.tip;
  };
  const p = pdfParams();
  const x = replenishExcel;
  say("Формирую PDF" + (x ? " из Excel" : "") + (p.withPhotos ? " с фото" : "") + "…");
  try {
    const url = replenishPdfUrl();
    const resp = x
      ? await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rows: x.rows, source: x.name }),
      })
      : await fetch(url);
    if (!resp.ok) throw await replenishHttpError(resp);
    const blob = await resp.blob();
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = filenameFromDisposition(resp.headers.get("Content-Disposition")) || "potrebnost.pdf";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(a.href);
    const n = resp.headers.get("X-Count");
    const hits = resp.headers.get("X-Photo-Hits");
    const cut = resp.headers.get("X-Truncated");
    const parts = ["PDF готов"];
    if (x) parts.push("из файла «" + x.name + "»");
    if (n != null) parts.push("товаров: " + fmt(n));
    if (p.withPhotos && hits != null) parts.push("с фото: " + fmt(hits));
    if (cut != null && +cut > 0) parts.push("сверху отброшено: " + fmt(cut));
    say(parts.join(" · "));
  } catch (err) {
    say("Ошибка PDF: " + err.message);
  }
}

const productsBaseHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text , tip: "Артикул единого каталога. Он же служит ключом строк размеров, документов склада и продаж."},
  { k: "name", label: "Наименование", render: cellFmts.text , tip: "Наименование из карточек WB и Ozon (каталог обновляется кнопкой «Обновить каталог»)."},
  { k: "brand", label: "Бренд", render: cellFmts.text , tip: "Бренд из карточки маркетплейса. Пусто, если бренд в карточке не заполнен."},
  { k: "barcode", label: "Баркод", render: cellFmts.text , tip: "В агрегированном режиме — штрихкод первого размера по алфавиту. Включите «По размерам», чтобы увидеть штрихкод каждого размера."},
  { k: "sizes_count", label: "Размеров", num: true, render: cellFmts.int , tip: "Сколько размеров заведено в каталоге для артикула. 0 = размеры не выгружались, остатки по размерам не покажутся."},
  { k: "net_cost", label: "Себестоимость", num: true, render: cellFmts.money , tip: "Себестоимость единицы из каталога. Пересчитывается как средневзвешенная по приходам «Нашего склада» и используется в расчёте прибыли."},
  { k: "recommended_price", label: "Рекомендуемая цена", num: true, render: cellFmts.money , tip: "Базовая цена WB = себестоимость × f(себестоимость) × f(объём), округление вверх до «…9». Якоря настраиваются в «Установить цены»."},
  { k: "min_price", label: "Мин. цена", num: true, render: cellFmts.money , tip: "Цена безубыточности: (себестоимость + хранение + логистика + услуги) ÷ (1 − комиссия% − мин. прибыль%). Ниже не опускаемся, не ниже себестоимости."},
  { k: "markup", label: "Наценка, %", num: true, render: (v) => v == null ? "—" : fmtPct(Number(v) * 100) , tip: "Итоговый множитель наценки f(себестоимость) × f(объём), показанный в процентах (значение ×100). Рекомендуемая цена = себестоимость × этот множитель до округления."},
  { k: "replenishable", label: "Докупаемый", render: replenishableCell , tip: "Чекбокс: докупаем ли товар. Флаг влияет на решения автопилота — дефицитный докупаемый товар он не спешит удорожать. Сохраняется в каталог."},
  { k: "subject", label: "Предмет", render: (v) => (v == null || v === "") ? "—" : v , tip: "Предмет WB (категория карточки) из каталога."},
  { k: "composition", label: "Состав", render: (v) => (v == null || v === "") ? "—" : v , tip: "Состав товара из карточки маркетплейса."},
  { k: "tags", label: "Маркетплейсы", render: (v) => Array.isArray(v) ? v.join(", ") : ((v == null || v === "") ? "—" : v) , tip: "Маркетплейсы, где заведена карточка: wb, ozon."},
  { k: "volume_l", label: "Объём, л", num: true, render: (v) => v == null || v === 0 ? "—" : fmt(v) , tip: "Объём товара в литрах из ЛК WB — по нему считается стоимость хранения."},
];
const productsSizeHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text , tip: "Артикул единого каталога, к которому относится размер."},
  { k: "size", label: "Размер", render: cellFmts.text , tip: "Размер из карточки (product_sizes). Выгрузка размеров нужна, чтобы вести остатки по размеру."},
  { k: "name", label: "Наименование", render: cellFmts.text , tip: "Наименование товара из карточки маркетплейса — одинаковое для всех его размеров."},
  { k: "brand", label: "Бренд", render: cellFmts.text , tip: "Бренд из карточки маркетплейса."},
  { k: "barcode", label: "Баркод", render: cellFmts.text , tip: "Штрихкод именно этого размера. По нему WB различает карточки, а сопоставление с детализацией продаж идёт по SKU."},
  { k: "net_cost", label: "Себестоимость", num: true, render: cellFmts.money , tip: "Себестоимость единицы из каталога — общая для всех размеров товара. Делить на размер не нужно: расчёт на единицу."},
  { k: "recommended_price", label: "Рекомендуемая цена", num: true, render: cellFmts.money , tip: "Базовая цена = себестоимость × f(себестоимость) × f(объём) с округлением вверх до «…9». Одинакова для всех размеров товара."},
  { k: "min_price", label: "Мин. цена", num: true, render: cellFmts.money , tip: "Цена безубыточности единицы по unit-экономике товара (комиссия, логистика, хранение, услуги за 30 дней). Не ниже себестоимости."},
  { k: "markup", label: "Наценка, %", num: true, render: (v) => v == null ? "—" : fmtPct(Number(v) * 100) , tip: "Множитель наценки f(себестоимость) × f(объём) в процентах (значение ×100). Работает от себестоимости и объёма, размер на него не влияет."},
  { k: "replenishable", label: "Докупаемый", render: replenishableCell , tip: "Чекбокс «докупаемый» — общий флаг на товар, одинаков для всех размеров. Используется автопилотом скидок и в отчёте о пополнении."},
  { k: "subject", label: "Предмет", render: (v) => (v == null || v === "") ? "—" : v , tip: "Предмет WB (категория карточки) из каталога."},
  { k: "composition", label: "Состав", render: (v) => (v == null || v === "") ? "—" : v , tip: "Состав товара из карточки маркетплейса."},
  { k: "tags", label: "Маркетплейсы", render: (v) => Array.isArray(v) ? v.join(", ") : ((v == null || v === "") ? "—" : v) , tip: "Маркетплейсы, где заведена карточка: wb, ozon."},
  { k: "volume_l", label: "Объём, л", num: true, render: (v) => v == null || v === 0 ? "—" : fmt(v) , tip: "Объём товара в литрах из ЛК WB — по нему считается стоимость хранения."},
];
const productsStockHeaders = [
  { k: "own_stock", label: "Остаток свой", num: true, render: (v) => v == null ? "—" : (Number.isInteger(Number(v)) ? fmt(v) : fmtFloat(Number(v), 1)) , tip: "Наш склад: баланс по документам (начальный + приход − отгрузка) без разбивки по размерам. Тот же источник, что и вкладка «Остатки»."},
  { k: "mp_stock", label: "Остаток МП", num: true, render: cellFmts.int , tip: "Остаток на маркетплейсах по последнему срезу стоков: WB + Ozon, суммой по всем складам. В режиме «По размерам» — только WB по этому размеру."},
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

function productsPriceMode() {
  const el = document.querySelector('input[name="productsPriceMode"]:checked');
  return el ? el.value : "recommended";
}

function productsPreviewHeaders() {
  const sizesEl = document.getElementById("productsSizes");
  const stocksEl = document.getElementById("productsStocks");
  const sizes = sizesEl ? sizesEl.checked : false;
  const stocks = stocksEl ? stocksEl.checked : false;
  let headers = (sizes ? productsSizeHeaders : productsBaseHeaders).map((h) => ({ ...h }));
  const recIdx = headers.findIndex((h) => h.k === "recommended_price");
  if (productsPriceMode() === "min" && recIdx >= 0) {
    headers[recIdx] = { k: "price", label: "Цена (мин.)", num: true, render: cellFmts.money };
  }
  if (stocks) headers = headers.concat(productsStockHeaders);
  return headers;
}

async function renderProducts() {
  const box = $("#productsTable");
  const msg = statusEl();
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
  pagedTable(box, colViewHeaders("products", productsVisibleHeaders()), data.rows || [], data.totals || null, "#productsTablePager", colViewPinKeys("products"));
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
      $("#headerMsg").textContent =
        "Докупаемый: " + article + " → " + (t.checked ? "да" : "нет");
    } catch (err) {
      t.checked = !t.checked;
      $("#headerMsg").textContent = "Ошибка: " + err.message;
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
  const msg = statusEl();
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
  const msg = statusEl();
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
  const num = box.querySelector('input[data-key="min_margin_pct"]');
  if (num) {
    const v = parseFloat(String(num.value).replace(",", "."));
    if (!isNaN(v) && v >= 0) s.min_margin_pct = v;
  }
  saveProductsPriceSettings(s);
  return s;
}

async function buildProductsPricePanel() {
  if (!productsPriceMeta) {
    try {
      productsPriceMeta = await api("/products/price-settings");
    } catch (err) {
      const msgEl = statusEl();
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
  for (const key of ["cost_anchors", "vol_anchors", "round_nice", "min_margin_pct"]) {
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
    } else if (key === "min_margin_pct") {
      const inp = document.createElement("input");
      inp.type = "number";
      inp.dataset.key = key;
      inp.min = "0";
      inp.step = "0.1";
      inp.value = Number(cur);
      field.appendChild(inp);
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
  const msgEl = statusEl();
  if (!msgEl) return;
  const likeEl = document.getElementById("productsLike");
  const sizesEl = document.getElementById("productsSizes");
  const stocksEl = document.getElementById("productsStocks");
  const q = likeEl ? likeEl.value.trim() : "";
  msgEl.textContent = "Пересчитываю цены…";
  try {
    const settings = collectProductsPriceSettings();
    const data = await apiPost("/products/preview", {
      like: q || undefined,
      sizes: sizesEl && sizesEl.checked ? 1 : 0,
      stocks: stocksEl && stocksEl.checked ? 1 : 0,
      mode: productsPriceMode(),
      price_settings: settings,
    });
    const rows = data.rows || [];
    const msg = statusEl();
    if (msg) {
      msg.textContent = "Предпросмотр цены (без записи): " + fmt(rows.length) +
        " — нажмите «Обновить базу» в шапке, чтобы применить на карточках";
    }
    pagedTable(
      $("#productsTable"),
      colViewHeaders("products", productsPreviewHeaders()),
      rows,
      null,
      "#productsTablePager",
      colViewPinKeys("products")
    );
    msgEl.textContent = productsPriceMode() === "min"
      ? "Готово — минимальные цены (break-even) по вашим коэффициентам"
      : "Готово — пересчитано по вашим коэффициентам";
  } catch (err) {
    msgEl.textContent = "Ошибка: " + err.message;
  }
}

async function sendProductsPrices() {
  const msgEl = statusEl();
  if (!msgEl) return;
  const likeEl = document.getElementById("productsLike");
  const sizesEl = document.getElementById("productsSizes");
  const stocksEl = document.getElementById("productsStocks");
  const q = likeEl ? likeEl.value.trim() : "";
  msgEl.textContent = "Отправляю цены в WB API…";
  try {
    const settings = collectProductsPriceSettings();
    const data = await apiPost("/products/prices/apply", {
      like: q || undefined,
      sizes: sizesEl && sizesEl.checked ? 1 : 0,
      stocks: stocksEl && stocksEl.checked ? 1 : 0,
      mode: productsPriceMode(),
      price_settings: settings,
    });
    if (data && data.ok === false) {
      msgEl.textContent = "Ошибка: " + (data.error || "неизвестная");
      return;
    }
    let msg = "Отправлено на WB: " + (data.pushed ?? 0);
    if (data.skipped) msg += "; пропущено без nm_id: " + data.skipped;
    if (data.note) msg += ": " + data.note;
    msgEl.textContent = msg;
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
  const days = pane.querySelector("input[data-days]");
  const month = pane.querySelector("input[data-month]");
  const year = pane.querySelector("input[data-year]");
  const writeDb = pane.querySelector(".write-db");
  const bySize = pane.querySelector(".by-size");
  const params = {};
  const pd = paneDates();
  if (pd.date_from) params.date_from = pd.date_from;
  if (pd.date_to) params.date_to = pd.date_to;
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
  const days = pane.querySelector("input[data-days]");
  const month = pane.querySelector("input[data-month]");
  const year = pane.querySelector("input[data-year]");
  const writeDb = pane.querySelector(".write-db");
  const bySize = pane.querySelector(".by-size");
  const params = {};
  const pd = paneDates();
  if (pd.date_from) params.date_from = pd.date_from;
  if (pd.date_to) params.date_to = pd.date_to;
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
  const msg = statusEl();
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
      const msg = statusEl();
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
  const msg = statusEl();
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
  const msg = statusEl();
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
    const msg = statusEl();
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
  { k: "chrt_id", label: "Код размера", render: cellFmts.text , tip: "Идентификатор размера: у WB — chrtId из выгрузки ЛК, у Ozon — Ozon Product ID. Строка = один размер/SKU."},
  { k: "vendor_code", label: "Артикул продавца", render: cellFmts.text , tip: "Ваш артикул: у WB — vendorCode из ЛК, у Ozon — Offer ID из отчёта о товарах."},
  { k: "nm_id", label: "Артикул WB", render: cellFmts.text , tip: "Артикул WB (nmID). Для карточек Ozon подставляется при загрузке из API: сначала по штрихкоду, иначе по артикулу без хвостовых сегментов (размера)."},
  { k: "brand", label: "Бренд", render: cellFmts.text , tip: "Бренд из карточки товара; у Ozon берётся «Бренд», а если его нет — «Категория»."},
  { k: "subject", label: "Предмет", render: cellFmts.text , tip: "Предмет WB из выгрузки ЛК. У карточек Ozon поле всегда пустое — Ozon предмета не отдаёт."},
  { k: "size", label: "Размер", render: cellFmts.text , tip: "Размер (techSize) из ЛК WB. У Ozon пусто: размер зашит в конец артикула Offer ID."},
  { k: "barcode", label: "Баркод", render: cellFmts.text , tip: "Штрихкод размера: ШК WB или «Штрихкод (Серийный номер / EAN)» у Ozon. По нему карточки WB и Ozon сопоставляются."},
  { k: "volume_l", label: "Объём, л.", num: true, render: (v) => v == null || v === 0 ? "—" : fmt(v) , tip: "Объём товара в литрах из ЛК WB — по нему считается стоимость хранения. У карточек Ozon всегда 0."},
  { k: "composition", label: "Состав", render: (v) => cellFmts.text(String(v || "").slice(0, 60)) , tip: "Состав из выгрузки ЛК WB; в таблице обрезается до 60 символов. У карточек Ozon пусто."},
  { k: "name", label: "Название", render: cellFmts.text , tip: "Название товара: у WB — «Название товара» из ЛК, у Ozon — «Name» из отчёта о товарах."},
];

function cardIds(name) {
  const apiName = name.split("-")[0];
  const tag = apiName === "oz" ? "oz" : "wb";
  const mp = apiName === "oz" ? "ozon" : "wb";
  return {
    apiName, mp, tag,
    prefix: apiName + "Cards",
    msgExcel: "#headerMsg",
    msgTable: "#headerMsg",
  };
}

function cardsTotals(headers, rows) {
  const t = {};
  for (const h of headers) {
    if (!h.num) continue;
    t[h.k] = (rows || []).reduce((s, r) => s + (Number(r[h.k]) || 0), 0);
  }
  return t;
}

async function renderCards(name) {
  const ids = cardIds(name);
  const box = document.getElementById(ids.prefix + "Table");
  const likeEl = document.getElementById(ids.prefix + "Like");
  const msg = document.querySelector(ids.msgTable);
  const st = { rows: [], total: 0, limit: 500, loading: false, fetching: null };
  box._st = st;

  const paint = () => {
    const prevCol = box._sc ? box._sc.col : 0;
    const prevTop = box._sc ? box._sc.top : 0;
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
    box.classList.add("paged");
    const headers = colViewHeaders(name, cardsHeaders);
    const tscroll = document.createElement("div");
    tscroll.className = "tscroll";
    const wrap = document.createElement("div");
    wrap.innerHTML = table(headers, st.rows, null, cardsTotals(headers, st.rows));
    tscroll.appendChild(wrap);
    box.appendChild(tscroll);
    const tr = wrap.querySelector("tr.totals-row");
    if (tr) {
      const hdr = wrap.querySelector("thead tr:not(.totals-row)");
      const h = hdr ? hdr.offsetHeight : 0;
      tr.querySelectorAll("th").forEach((th) => { th.style.top = h + "px"; });
    }
    applyStickyCols(wrap, headers, colViewPinKeys(name));
    const cs = box.querySelector(".tscroll");
    if (cs && prevCol) cs.scrollLeft = prevCol;
    if (prevTop) box.scrollTop = prevTop;
    box._sc = { col: cs ? cs.scrollLeft : 0, top: box.scrollTop };
    mountXBar(box, tscroll);
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
  { k: "article", label: "Артикул", render: cellFmts.text , tip: "Артикул продавца (vendorCode) из отчёта WB; если поле не пришло — из таблицы соответствия nmId → артикул."},
  { k: "name", label: "Название", render: cellFmts.text , tip: "Название из локального каталога товаров по этому артикулу, а не из отчёта WB. Пусто, если товара нет в каталоге."},
  { k: "orders", label: "Заказы", num: true, render: cellFmts.int , tip: "Заказы за срез (WB orderCount) — оформленные заказы, включая отменённые и возвращённые."},
  { k: "revenue", label: "Выручка", num: true, render: cellFmts.money , tip: "Сумма заказов за срез (orderSum), ₽. Включает отменённые; фактические деньги — в колонке «Сумма выкупа»."},
  { k: "avg_price", label: "Ср. цена", num: true, render: cellFmts.money , tip: "Средняя цена заказа в отчёте WB (avgPrice), ₽: сумма заказов ÷ число заказов за срез."},
];

const wbFunnelHeaders = [
  { k: "date_from", label: "С", render: cellFmts.text , tip: "Начало окна среза, который реально лежит в базе. Может отличаться от запрошенного периода — тогда итоги считаются за этот срез."},
  { k: "date_to", label: "По", render: cellFmts.text , tip: "Конец окна среза из базы. Если точного среза за выбранный период нет, здесь показан другой доступный диапазон."},
  { k: "article", label: "Артикул", render: cellFmts.text , tip: "Артикул продавца (vendorCode) из отчёта WB; если поле не пришло — из таблицы соответствия nmId → артикул."},
  { k: "nm_id", label: "Артикул WB", render: cellFmts.text , tip: "Артикул WB (product.nmID) — внутренний идентификатор карточки на маркетплейсе, не путать с артикулом продавца."},
  { k: "name", label: "Название", render: cellFmts.text , tip: "Название из локального каталога товаров по этому артикулу, а не из отчёта WB. Пусто, если товара нет в каталоге."},
  { k: "title", label: "Название (API)", render: cellFmts.text , tip: "Название карточки из самого отчёта WB (product.title). Колонка «Название» берётся из локального каталога, значения могут расходиться."},
  { k: "subject_name", label: "Предмет", render: cellFmts.text , tip: "Предмет товара по классификатору WB (product.subjectName) — категория карточки из отчёта."},
  { k: "subject_id", label: "ID предмета", render: cellFmts.text , tip: "Числовой ID предмета в классификаторе WB (product.subjectId); используется в фильтрах отчёта воронки."},
  { k: "brand_name", label: "Бренд", render: cellFmts.text , tip: "Бренд из карточки отчёта воронки (product.brandName)."},
  { k: "tags", label: "Теги", render: cellFmts.text , tip: "Теги карточки из product.tags: список склеивается через запятую, иначе выводится строкой JSON."},
  { k: "product_rating", label: "Рейтинг карточки", num: true, render: numDec(1) , tip: "Рейтинг товара по оценкам покупателей (product.productRating), шкала 0–10. Приходит вместе со срезом воронки."},
  { k: "feedback_rating", label: "Рейтинг по отзывам", num: true, render: numDec(2) , tip: "Средняя оценка по отзывам (product.feedbackRating), шкала 0–5. Это не то же самое, что рейтинг товара."},
  { k: "stock_wb", label: "Остатки WB", num: true, render: cellFmts.intZero , tip: "Остаток на складах WB из карточки отчёта (product.stocks.wb), шт — на момент среза воронки, а не на сегодня."},
  { k: "stock_mp", label: "Остатки свой склад", num: true, render: cellFmts.intZero , tip: "Второе поле блока остатков карточки в отчёте (product.stocks.mp), шт. Часто приходит 0 — тогда ориентируйтесь на остаток WB."},
  { k: "stock_balance_sum", label: "Сумма остатков", num: true, render: cellFmts.moneyZero , tip: "Сумма баланса остатков карточки (product.stocks.balanceSum), ₽ — приходит из отчёта воронки, у нас не пересчитывается."},
  { k: "views", label: "Просмотры", num: true, render: cellFmts.int , tip: "Просмотры карточки за срез (openCount) — сколько раз карточку открывали в каталоге и поиске WB."},
  { k: "opens", label: "Открытия", num: true, render: cellFmts.intZero , tip: "Открытия карточки (openCardCount). Поле есть не во всех ответах WB, при его отсутствии сохраняется 0."},
  { k: "adds", label: "В корзину", num: true, render: cellFmts.int , tip: "Добавления в корзину за срез (cartCount), шт."},
  { k: "orders", label: "Заказы", num: true, render: cellFmts.int , tip: "Заказы за срез (WB orderCount) — оформленные заказы, включая отменённые и возвращённые."},
  { k: "buyouts", label: "Выкупы", num: true, render: cellFmts.intZero , tip: "Выкупы — заказы, полученные покупателем (buyoutCount). С текущим ключом WB их не отдаёт, поэтому обычно 0."},
  { k: "cancelled", label: "Отмены", num: true, render: cellFmts.int , tip: "Отменённые заказы за срез (cancelCount), шт — включая возвраты покупателем."},
  { k: "cancel_sum", label: "Сумма отмен", num: true, render: cellFmts.moneyZero , tip: "Сумма отменённых заказов (cancelSum), ₽ за срез."},
  { k: "avg_price", label: "Ср. цена", num: true, render: cellFmts.moneyZero , tip: "Средняя цена заказа из отчёта WB (avgPrice), ₽: сумма заказов ÷ число заказов за срез."},
  { k: "revenue", label: "Выручка", num: true, render: cellFmts.money , tip: "Сумма заказов за срез (orderSum), ₽. Включает отменённые; фактические поступления — в колонке «Сумма выкупа»."},
  { k: "buyout_sum", label: "Сумма выкупа", num: true, render: cellFmts.moneyZero , tip: "Сумма выкупленных заказов (buyoutSum), ₽ — реальные деньги по товару. Обычно 0: у ключа нет доступа к выкупам."},
  { k: "avg_orders_per_day", label: "Заказов в день", num: true, render: numDec(2) , tip: "Среднее число заказов в день (avgOrdersCountPerDay) — метрика WB, посчитанная ими по выбранному периоду."},
  { k: "share_order_percent", label: "Доля в выручке, %", num: true, render: cellFmts.pct , tip: "Доля заказов этой карточки в заказах магазина за период (shareOrderPercent), % — метрика WB."},
  { k: "add_to_wishlist", label: "В отложенные", num: true, render: cellFmts.intZero , tip: "Добавления в избранное и ожидание (addToWishlist) за срез, шт — отложенные покупки покупателя."},
  { k: "time_to_ready_min", label: "Доставка, средн.", num: true, render: fmtMinutes , tip: "Срок, отведённый WB на поставку товара на склад (timeToReady): дни × 1440 + часы × 60 + минуты."},
  { k: "localization_percent", label: "Локальные, %", num: true, render: cellFmts.pct , tip: "Доля локальных заказов (localizationPercent), % — метрика WB, а не наш расчёт по складам."},
  { k: "conv_to_cart_percent", label: "Просмотр→Корзина, %", num: true, render: cellFmts.pct , tip: "Конверсия «просмотр → корзина» (conversions.addToCartPercent), % — метрика WB: корзина ÷ просмотры."},
  { k: "conv_cart_to_order_percent", label: "Корзина→Заказ, %", num: true, render: cellFmts.pct , tip: "Конверсия «корзина → заказ» (conversions.cartToOrderPercent), %: заказы ÷ добавления в корзину."},
  { k: "conv_buyout_percent", label: "Заказ→Выкуп, %", num: true, render: cellFmts.pct , tip: "Конверсия «заказ → выкуп» (conversions.buyoutPercent), %: выкупы ÷ заказы. Обычно 0 — у ключа нет выкупов."},
  { k: "past_views", label: "Просмотры (пред. период)", num: true, render: cellFmts.int , tip: "Просмотры за предыдущий период сравнения (statistic.past.openCount), шт."},
  { k: "past_adds", label: "В корзину (пред. период)", num: true, render: cellFmts.int , tip: "Добавления в корзину за предыдущий период (statistic.past.cartCount), шт."},
  { k: "past_orders", label: "Заказы (пред. период)", num: true, render: cellFmts.int , tip: "Заказы за предыдущий период (statistic.past.orderCount), шт."},
  { k: "past_cancelled", label: "Отмены (пред. период)", num: true, render: cellFmts.int , tip: "Отмены за предыдущий период (statistic.past.cancelCount), шт."},
  { k: "past_buyouts", label: "Выкупы (пред. период)", num: true, render: cellFmts.int , tip: "Выкупы за предыдущий период (statistic.past.buyoutCount), шт. Обычно 0 — у ключа нет выкупов."},
  { k: "past_revenue", label: "Выручка (пред. период)", num: true, render: cellFmts.money , tip: "Сумма заказов за предыдущий период (statistic.past.orderSum), ₽."},
  { k: "past_buyout_sum", label: "Сумма выкупа (пред. период)", num: true, render: cellFmts.money , tip: "Сумма выкупа за предыдущий период (statistic.past.buyoutSum), ₽. Обычно 0 — нет доступа к выкупам."},
  { k: "past_cancel_sum", label: "Сумма отмен (пред. период)", num: true, render: cellFmts.money , tip: "Сумма отмен за предыдущий период (statistic.past.cancelSum), ₽."},
  { k: "past_avg_price", label: "Ср. цена (пред. период)", num: true, render: cellFmts.money , tip: "Средняя цена заказа за предыдущий период (statistic.past.avgPrice), ₽."},
  { k: "dy_views", label: "Динамика просмотров, %", num: true, render: cellFmts.signedPct , tip: "Динамика просмотров к предыдущему периоду, % (comparison.openCountDynamic)."},
  { k: "dy_adds", label: "Динамика «в корзину», %", num: true, render: cellFmts.signedPct , tip: "Динамика добавлений в корзину, % (comparison.cartCountDynamic)."},
  { k: "dy_orders", label: "Динамика заказов, %", num: true, render: cellFmts.signedPct , tip: "Динамика заказов, % (comparison.orderCountDynamic)."},
  { k: "dy_cancelled", label: "Динамика отмен, %", num: true, render: cellFmts.signedPct , tip: "Динамика отмен, % (comparison.cancelCountDynamic)."},
  { k: "dy_buyouts", label: "Динамика выкупов, %", num: true, render: cellFmts.signedPct , tip: "Динамика выкупов, % (comparison.buyoutCountDynamic). Обычно 0 — выкупов в отчёте нет."},
  { k: "dy_revenue", label: "Динамика выручки, %", num: true, render: cellFmts.signedPct , tip: "Динамика суммы заказов, % (comparison.orderSumDynamic)."},
  { k: "dy_avg_price", label: "Динамика ср. цены, %", num: true, render: cellFmts.signedPct , tip: "Динамика средней цены заказа, % (comparison.avgPriceDynamic)."},
  { k: "wb_club_order_count", label: "WB Клуб: заказы", num: true, render: cellFmts.intZero , tip: "Заказы участников WB Клуба (wbClub.orderCount), шт — подмножество общих заказов."},
  { k: "wb_club_order_sum", label: "WB Клуб: заказы, ₽", num: true, render: cellFmts.moneyZero , tip: "Сумма заказов участников WB Клуба (wbClub.orderSum), ₽."},
  { k: "wb_club_buyout_count", label: "WB Клуб: выкупы", num: true, render: cellFmts.intZero , tip: "Выкупы участников WB Клуба (wbClub.buyoutCount), шт. Обычно 0 — у ключа нет выкупов."},
  { k: "wb_club_buyout_sum", label: "WB Клуб: выкупы, ₽", num: true, render: cellFmts.moneyZero , tip: "Сумма выкупленных заказов участников WB Клуба (wbClub.buyoutSum), ₽."},
  { k: "wb_club_cancel_count", label: "WB Клуб: отмены", num: true, render: cellFmts.intZero , tip: "Отмены среди заказов участников WB Клуба (wbClub.cancelCount), шт."},
  { k: "wb_club_cancel_sum", label: "WB Клуб: отмены, ₽", num: true, render: cellFmts.moneyZero , tip: "Сумма отмен среди заказов участников WB Клуба (wbClub.cancelSum), ₽."},
  { k: "wb_club_avg_price", label: "WB Клуб: ср. цена", num: true, render: cellFmts.moneyZero , tip: "Средняя цена заказа участника WB Клуба (wbClub.avgPrice), ₽."},
  { k: "wb_club_buyout_percent", label: "WB Клуб: % выкупа", num: true, render: cellFmts.pct , tip: "Доля выкупов среди заказов WB Клуба (wbClub.buyoutPercent), %. Обычно 0."},
  { k: "wb_club_avg_orders_per_day", label: "WB Клуб: заказов/день", num: true, render: numDec(2) , tip: "Среднее число заказов в день от участников WB Клуба (wbClub.avgOrderCountPerDay)."},
];

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
  const msg = statusEl();
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
    const warn = document.getElementById("wbFunnelPeriodWarn");
    if (warn) {
      if (partial && data.rows && data.rows.length) {
        warn.style.display = "block";
        warn.textContent = "В базе нет среза точно за " + p.date_from + " — " + p.date_to
          + ". Показан срез " + data.snapshot_from + " — " + data.snapshot_to
          + " (итоги — по нему). Чтобы увидеть свой период, нажмите «Обновить базу» в шапке.";
      } else {
        warn.style.display = "none";
        warn.textContent = "";
      }
    }
  }
  const headers = colViewHeaders("wb-funnel", expandedEl && expandedEl.checked ? wbFunnelHeaders : wbFunnelCompact);
  pagedTable(box, headers, data.rows || [], data.totals || null, "#wbFunnelTablePager", colViewPinKeys("wb-funnel"));
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
  { k: "article", label: "Артикул", render: cellFmts.text , tip: "Артикул продавца: vendorCode на WB, offer_id на Ozon. На Ozon он может включать размер."},
  { k: "name", label: "Наименование", render: cellFmts.text , tip: "Название из локального каталога товаров по артикулу (таблица products), не из отчёта маркетплейса."},
  { k: "chrt_id", label: "Код размера", render: cellFmts.text , tip: "Код размера WB (chrtId) из отчёта об остатках. На Ozon всегда пусто — там размер определяется по offer_id."},
  { k: "size", label: "Размер", render: cellFmts.text , tip: "Размер: на WB подставляется из карточек по chrtId, на Ozon — из offer_id. Может остаться пустым."},
  { k: "barcode", label: "Баркод", render: cellFmts.text , tip: "Баркод размера: на WB берётся из карточек WB по chrtId, на Ozon — из данных Ozon. Пусто, если размер не распознан."},
  { k: "warehouse", label: "Склад", render: cellFmts.text , tip: "Склад из отчёта об остатках. Строка — одна на пару «артикул + размер + склад», они же ключ записи."},
  { k: "quantity", label: "Доступно", num: true, render: cellFmts.int , tip: "Доступно к продаже: на WB — quantity из отчёта об остатках, на Ozon — свободный остаток free_to_sell_amount."},
  { k: "quantity_full", label: "Всего на складах", num: true, render: cellFmts.int , tip: "Всего на складах: на WB — quantityFull, на Ozon — свободный + зарезервированный + обещанный остаток."},
  { k: "in_way", label: "В пути", num: true, render: cellFmts.int , tip: "В пути: на WB — inWayToClient + inWayFromClient, на Ozon — обещанное количество promised_amount."},
  { k: "date", label: "Дата", render: cellFmts.text , tip: "Дата среза остатков из отчёта маркетплейса."},
  { k: "marketplace", label: "Маркетплейс", render: (v) => (v == null || v === "") ? "—" : (MP_LABELS[v] || v) , tip: "Маркетплейс среза: WB или Ozon."},
];

const wbStockAggHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text , tip: "Артикул, по которому свёрнуты остатки: WB — vendorCode, Ozon — offer_id."},
  { k: "name", label: "Наименование", render: cellFmts.text , tip: "Название из локального каталога товаров по артикулу."},
  { k: "warehouse", label: "Склад", render: cellFmts.text , tip: "Склад, по которому сгруппированы остатки. В свёрнутом виде суммируются все размеры артикула на складе."},
  { k: "quantity", label: "Доступно", num: true, render: cellFmts.int , tip: "Сумма доступного к продаже по всем размерам артикула на складе; свёртка считается в браузере."},
  { k: "quantity_full", label: "Всего на складах", num: true, render: cellFmts.int , tip: "Сумма «всего на складах» по всем размерам артикула на складе; свёртка считается в браузере."},
  { k: "in_way", label: "В пути", num: true, render: cellFmts.int , tip: "Сумма «в пути» по всем размерам артикула на складе; свёртка считается в браузере."},
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
    rows = rows.filter((r) => likeMatch(r.article + " " + (r.name || "") + " " + (r.size || ""), q));
  }
  const head = colViewHeaders("wb-stock", agg && agg.checked ? wbStockAggHeaders : wbStockHeaders);
  const msg = statusEl();
  if (msg) msg.textContent = stockSummary(rows, data.date, "Остатки");
  pagedTable(box, head, rows, null, null, colViewPinKeys("wb-stock"));
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
  { k: "article", label: "Артикул", render: cellFmts.text , tip: "Артикул продавца: vendorCode на WB, offer_id на Ozon."},
  { k: "name", label: "Наименование", render: cellFmts.text , tip: "Название из локального каталога товаров по артикулу (products), не из прайса маркетплейса."},
  { k: "size", label: "Размер", render: cellFmts.text , tip: "Размер из прайса: на WB — techSize, на Ozon — восстанавливается из offer_id. Снимок хранит пару «артикул + размер»."},
  { k: "discounted_price", label: "Цена со скид.", num: true, render: cellFmts.money , tip: "Цена со скидкой: на WB — discountedPrice из discounts-prices-api. На Ozon поле равно текущей цене."},
  { k: "price", label: "Цена без скид.", num: true, render: cellFmts.money , tip: "Цена без скидки: на WB — price из discounts-prices-api, на Ozon — текущая цена price_price."},
  { k: "discount", label: "Скидка, %", num: true, render: (v) => v == null ? "—" : fmt(v, 1) + "%" , tip: "Скидка в %: WB — (price − discounted_price) ÷ price × 100; Ozon — (1 − цена ÷ зачёркнутая цена) × 100."},
  { k: "nm_id", label: "Артикул WB", render: cellFmts.text , tip: "Код номенклатуры WB (nmId) из прайса — по нему товар ищется на маркетплейсе."},
];

const wbPricesAggHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text , tip: "Артикул, по которому свёрнуты цены по всем размерам."},
  { k: "name", label: "Наименование", render: cellFmts.text , tip: "Название из локального каталога товаров по артикулу."},
  { k: "sizes", label: "Размеров", num: true, render: cellFmts.int , tip: "Сколько строк (размеров) свёрнуто в артикул; считается в браузере по позициям снимка цен."},
  { k: "disc_min", label: "Цена со скид.", num: true, render: (v, r) => rangeLabel(r.disc_min, r.disc_max, fmtMoney) , tip: "Минимальная цена со скидкой по размерам артикула. Если значения разные, ячейка показывает диапазон min–max."},
  { k: "price_min", label: "Цена без скид.", num: true, render: (v, r) => rangeLabel(r.price_min, r.price_max, fmtMoney) , tip: "Минимальная цена без скидки по размерам артикула. Если значения разные, ячейка показывает диапазон min–max."},
  { k: "disc_min_p", label: "Скидка, %", num: true, render: (v, r) => rangeLabel(r.disc_min_p, r.disc_max_p, (x) => fmt(x, 1) + "%") , tip: "Минимальная скидка в % по размерам артикула. Если значения разные, ячейка показывает диапазон min–max."},
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
  const msg = statusEl();
  if (msg) {
    const when = data.updated_at ? " · срез: " + data.updated_at : "";
    msg.textContent = data.count ? "Позиций: " + fmt(data.count) + when : "Нет данных в базе";
  }
  pagedTable(box, colViewHeaders("wb-prices", agg ? wbPricesAggHeaders : wbPricesHeaders), rows, null, null, colViewPinKeys("wb-prices"));
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
    rows = rows.filter((r) => likeMatch(r.article + " " + (r.name || "") + " " + (r.size || ""), q));
  }
const head = colViewHeaders("oz-stock", agg && agg.checked ? wbStockAggHeaders : wbStockHeaders);
  const msg = statusEl();
  if (msg) msg.textContent = stockSummary(rows, data.date, "Снимок") + " · «С»/«По» не применяются: раздел снимка на дату";
  pagedTable(box, head, rows, null, null, colViewPinKeys("oz-stock"));
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
const msg = statusEl();
  if (msg) {
    const when = data.updated_at ? " · на дату: " + data.updated_at : "";
    const note = " · «С»/«По» не применяются: раздел текущих цен";
    msg.textContent = data.count ? "Карточек: " + fmt(data.count) + when + note : "Нет данных" + note;
  }
  pagedTable(box, colViewHeaders("oz-prices", agg ? wbPricesAggHeaders : wbPricesHeaders), rows, null, null, colViewPinKeys("oz-prices"));
}

async function renderOzSales() {
  const box = document.getElementById("ozRealTable");
  const likeEl = document.getElementById("ozRealLike");
  const p = paneDates();
  const q = { date_from: p.date_from || undefined, date_to: p.date_to || undefined };
  let data;
  try {
    data = await api("/sales" + qs({ marketplace: "ozon", date_from: q.date_from, date_to: q.date_to }));
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить реализацию: ' + escapeHtml(err.message) + "</div>";
    setStatus("Ошибка: " + err.message, { error: true });
    return;
  }
  let rows = data.rows || [];
  if (likeEl) {
    const s = likeEl.value.trim().toLowerCase();
    if (s) rows = rows.filter((r) => likeMatch(r.article + " " + (r.name || ""), s));
  }
  const win = (p.date_from && p.date_to) ? p.date_from + " … " + p.date_to : "";
  if (!rows.length && !data.count) {
    setStatus(win
      ? "Нет данных: реализации Ozon за " + win + " в базе нет. Обновите продажи: «Обновить базу» → WB/OZON API ▸ Продажи."
      : "Нет данных: реализации Ozon в базе нет. Задайте период в шапке и нажмите «Применить».");
  } else {
    setStatus((win ? win + " · " : "") + "Строк: " + fmt(data.count));
  }
  pagedTable(box, colViewHeaders("oz-realization", salesHeaders), rows, null, null, colViewPinKeys("oz-realization"));
}

const wbStorageHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text , tip: "Артикул продавца (vendorCode) из отчёта по платному хранению WB."},
  { k: "name", label: "Наименование", render: cellFmts.text , tip: "Название из локального каталога товаров по артикулу (products)."},
  { k: "barcodes_count", label: "Баркодов", num: true, render: cellFmts.int , tip: "Число баркодов артикула в отчёте paid_storage, усреднённое по складам и дням отчёта."},
  { k: "volume", label: "Объём, л", num: true, render: (v) => v == null ? "—" : fmtVol(v) , tip: "Объём товара по данным WB, л. Тоже среднее по складам и дням отчёта — по умолчанию за 7 дней."},
  { k: "storage_price", label: "Хранение за баркод", num: true, render: cellFmts.money4 , tip: "Хранение за один баркод, ₽: warehousePrice ÷ barcodesCount. Метрика WB за 7 дней, за баркод, не за литр."},
  { k: "warehouse_price", label: "Сумма хранения", num: true, render: cellFmts.money4 , tip: "Сумма хранения warehousePrice, ₽ за отчётный период (7 дней), усреднённая по складам и сохранённая по nm_id."},
  { k: "nm_id", label: "Артикул WB", render: cellFmts.text , tip: "Код номенклатуры WB (nmId), по которому хранение сохраняется в базе."},
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
  const msg = statusEl();
  if (msg) {
    const when = data.updated_at ? " · срез: " + data.updated_at : "";
    msg.textContent = data.count ? "Позиций: " + fmt(data.count) + when : "Нет данных в базе";
  }
  pagedTable(box, colViewHeaders("wb-storage", wbStorageHeaders), data.rows || [], null, null, colViewPinKeys("wb-storage"));
}

const salesHeaders = [
  { k: "date", label: "Дата", render: cellFmts.text , tip: "WB — дата продажи из отчёта о реализации, Ozon — дата конца месяца отчёта. Строка = дата + маркетплейс + артикул; строки из детализации (source=detail) исключены."},
  { k: "marketplace", label: "Маркетплейс", render: cellFmts.tag , tip: "Код маркетплейса строки: wb или ozon."},
  { k: "article", label: "Артикул", render: cellFmts.text , tip: "Артикул продавца из отчёта (vendorCode / Offer ID), по нему суммируются все строки за дату."},
  { k: "name", label: "Наименование", render: cellFmts.text , tip: "Название из каталога товаров по артикулу. Строки, у которых артикула нет в каталоге, в выборку не попадают."},
  { k: "quantity", label: "Продано, шт", num: true, render: cellFmts.int , tip: "Сумма количества за дату и артикул: у WB количество нетто (отчёт уже вычитал возвраты), у Ozon — количество проданных единиц."},
  { k: "revenue", label: "Выручка", num: true, render: cellFmts.money , tip: "Сумма по дню и артикулу: WB — «Вайлдберриз реализовал (Пр)», Ozon — цена продавца × количество (без скидок Ozon)."},
  { k: "income", label: "К перечислению", num: true, render: cellFmts.money , tip: "Сумма к перечислению: WB — «К перечислению» (forPay), Ozon — «К перечислению» минус сумма возвратов по строке."},
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
    if (q) rows = rows.filter((r) => likeMatch(r.article + " " + (r.name || ""), q));
  }
  const msg = statusEl();
  if (msg) msg.textContent = data.count ? "Строк: " + fmt(data.count) : "Нет данных за период";
  pagedTable(box, colViewHeaders("wb-sales", salesHeaders), rows, null, null, colViewPinKeys("wb-sales"));
}

async function renderWbDetail() {
  const box = document.getElementById("wbDetailTable");
  const likeEl = document.getElementById("wbDetailLike");
  const rawEl = document.getElementById("wbDetailRaw");
  const f = filters();
  const p = { date_from: f.date_from, date_to: f.date_to };
  const q = likeEl ? likeEl.value.trim() : "";
  const raw = rawEl ? rawEl.checked : false;
  const msg = statusEl();
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
      pagedTable(box, colViewHeaders("wb-detail", wbDetailRowHeaders), rows, null, "#wbDetailTablePager", colViewPinKeys("wb-detail"));
    } else {
      const data = await api("/wb/detail-summary" + qs({
        date_from: p.date_from || undefined,
        date_to: p.date_to || undefined,
        article_like: q || undefined,
      }));
      const rows = data.rows || [];
      tipEl.classList.remove("hidden");
      if (msg) msg.textContent = "По артикулам: " + fmt(data.count || 0);
      pagedTable(box, colViewHeaders("wb-detail", wbDetailSummaryHeaders), rows, data.totals, "#wbDetailTablePager", colViewPinKeys("wb-detail"));
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
  const msg = statusEl();
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
  const msg = statusEl();
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
  "wb-cards": "#headerMsg",
  "wb-stock": "#headerMsg",
  "wb-funnel": "#headerMsg",
  "wb-sales": "#headerMsg",
  "wb-prices": "#headerMsg",
  "wb-storage": "#headerMsg",
  "wb-detail": "#headerMsg",
  "oz-detail": "#headerMsg",
  "oz-cards": "#headerMsg",
  "oz-stock": "#headerMsg",
  "oz-prices": "#headerMsg",
  "oz-realization": "#headerMsg",
};

async function renderOzDetail() {
  const box = document.getElementById("ozDetailTable");
  const likeEl = document.getElementById("ozDetailLike");
  const rawEl = document.getElementById("ozDetailRaw");
  const f = filters();
  const q = likeEl ? likeEl.value.trim() : "";
  const raw = rawEl ? rawEl.checked : false;
  const msg = statusEl();
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
      if (!rows.length) {
        setStatus(emptyPeriodReason(data, "детализации продаж Ozon",
          "Обновите детализацию: «Обновить базу» → OZON API ▸ Детализация продаж."));
      } else if (msg) msg.textContent = "Строк в базе: " + fmt(data.total || 0) +
        (rows.length < (data.total || 0) ? " (показаны первые " + fmt(rows.length) + " — меняйте период или поиск)" : "");
      pagedTable(box, colViewHeaders("oz-detail", ozDetailRowHeaders), rows, null, "#ozDetailTablePager", colViewPinKeys("oz-detail"));
    } else {
      const data = await api("/ozon/detail-summary" + ozBySizeParam("oz-detail", qs({
        date_from: f.date_from || undefined,
        date_to: f.date_to || undefined,
        article_like: q || undefined,
      })));
      const rows = data.rows || [];
      tipEl.classList.remove("hidden");
      if (msg) {
        if (!rows.length && !data.count) {
          setStatus(emptyPeriodReason(data, "детализации продаж Ozon",
            "Обновите детализацию: «Обновить базу» → OZON API ▸ Детализация продаж."));
        } else {
          let m = (ozBySize("oz-detail") ? "По размерам: " : "По товарам: ") + fmt(data.count || 0);
          if (data.cashflow_received != null) {
            m += " · на р/с фактически получено: " + fmtMoney(data.cashflow_received) +
              " (движение средств, " + fmt(data.cashflow_periods || 0) + " пер.)";
          }
          msg.textContent = m;
        }
      }
      pagedTable(box, colViewHeaders("oz-detail", ozDetailSummaryHeaders), rows, data.totals, "#ozDetailTablePager", colViewPinKeys("oz-detail"));
    }
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить детализацию Ozon: ' + escapeHtml(err.message) + "</div>";
    setStatus("Ошибка: " + err.message, { error: true });
  }
}

function ozDetailExportUrl() {
  const rawEl = document.getElementById("ozDetailRaw");
  const likeEl = document.getElementById("ozDetailLike");
  const f = filters();
  const raw = rawEl ? rawEl.checked : false;
  const q = likeEl ? likeEl.value.trim() : "";
  const path = raw ? "/api/export/ozon/detail-rows" : "/api/export/ozon/detail-summary";
  // режим группировки касается только свода; построчные данные всегда по карточкам
  const base = qs({
    date_from: f.date_from || undefined,
    date_to: f.date_to || undefined,
    article_like: q || undefined,
  });
  const p = raw ? base : ozBySizeParam("oz-detail", base);
  const cp = colViewParam("oz-detail");
  return path + p + (cp ? (p ? "&" : "?") + cp : "");
}

async function downloadOzDetailExcel() {
  const msg = statusEl();
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
  const msg = statusEl();
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

async function renderOzPlacement() {
  const box = document.getElementById("ozPlacementTable");
  const likeEl = document.getElementById("ozPlacementLike");
  const rawEl = document.getElementById("ozPlacementRaw");
  const p = paneDates();
  const q = likeEl ? likeEl.value.trim() : "";
  const raw = rawEl ? rawEl.checked : false;
  const msg = statusEl();
  try {
    if (raw) {
      const data = await api("/ozon/placement-rows" + qs({
        date_from: p.date_from || undefined,
        date_to: p.date_to || undefined,
        article_like: q || undefined,
        limit: 500,
      }));
      const rows = data.rows || [];
      if (!rows.length) {
        setStatus(emptyPeriodReason(data, "размещения Ozon",
          "Обновите размещение: «Обновить базу» → OZON API ▸ Размещение."));
      } else if (msg) msg.textContent = "Строк в базе: " + fmt(data.total || 0) +
        (rows.length < (data.total || 0) ? " (показаны первые " + fmt(rows.length) + " — меняйте период или поиск)" : "");
      pagedTable(box, colViewHeaders("oz-placement", ozPlacementRowHeaders), rows, data.totals, "#ozPlacementTablePager", colViewPinKeys("oz-placement"));
    } else {
      const data = await api("/ozon/placement-summary" + ozBySizeParam("oz-placement", qs({
        date_from: p.date_from || undefined,
        date_to: p.date_to || undefined,
        article_like: q || undefined,
      })));
      const rows = data.rows || [];
      if (!rows.length && !data.count) {
        setStatus(emptyPeriodReason(data, "размещения Ozon",
          "Обновите размещение: «Обновить базу» → OZON API ▸ Размещение."));
      } else if (msg) msg.textContent = (ozBySize("oz-placement") ? "По размерам: " : "По товарам: ") + fmt(data.count || 0);
      pagedTable(box, colViewHeaders("oz-placement", ozPlacementSummaryHeaders), rows, data.totals, "#ozPlacementTablePager", colViewPinKeys("oz-placement"));
    }
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить размещение Ozon: ' + escapeHtml(err.message) + "</div>";
    setStatus("Ошибка: " + err.message, { error: true });
  }
}

function ozPlacementExportUrl() {
  const rawEl = document.getElementById("ozPlacementRaw");
  const likeEl = document.getElementById("ozPlacementLike");
  const raw = rawEl ? rawEl.checked : false;
  const q = likeEl ? likeEl.value.trim() : "";
  const p = paneDates();
  const path = raw ? "/api/export/ozon/placement-rows" : "/api/export/ozon/placement-summary";
  // режим группировки касается только свода; построчные данные всегда по карточкам
  const base = qs({
    date_from: p.date_from || undefined,
    date_to: p.date_to || undefined,
    article_like: q || undefined,
  });
  const p2 = raw ? base : ozBySizeParam("oz-placement", base);
  const cp = colViewParam("oz-placement");
  return path + p2 + (cp ? (p2 ? "&" : "?") + cp : "");
}

async function downloadOzPlacementExcel() {
  const msg = statusEl();
  msg.textContent = "Формирую Excel…";
  try {
    const resp = await fetch(ozPlacementExportUrl());
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

async function uploadOzPlacementToDisk() {
  const msg = statusEl();
  msg.textContent = "Формирую файл…";
  try {
    const resp = await fetch(ozPlacementExportUrl());
    if (!resp.ok) throw new Error(resp.status + " " + (await resp.text()));
    const blob = await resp.blob();
    const now = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    const stamp = now.getFullYear() + pad(now.getMonth() + 1) + pad(now.getDate()) + "-" + pad(now.getHours()) + pad(now.getMinutes());
    const f = filters();
    const kind = (document.getElementById("ozPlacementRaw") || {}).checked ? "rows" : "summary";
    const name = "oz_placement_" + kind + "_" + (f.date_from || "na") + "_" + (f.date_to || "na") + "_" + stamp + ".xlsx";
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

async function renderOzCashflow() {
  const box = document.getElementById("ozCashflowTable");
  const p = paneDates();
  const msg = statusEl();
  try {
    const data = await api("/ozon/cashflow-rows" + qs({
      date_from: p.date_from || undefined,
      date_to: p.date_to || undefined,
    }));
    const rows = data.rows || [];
    if (!rows.length && !data.count) {
      setStatus(emptyPeriodReason(data, "движения средств Ozon",
        "Обновите: «Обновить базу» → OZON API ▸ Движение средств."));
    } else {
      let m = "Периодов: " + fmt(data.count || 0);
      if (data.received != null && data.received) {
        m += " · фактически получено: " + fmtMoney(data.received);
      }
      if (msg) msg.textContent = m;
    }
    pagedTable(box, colViewHeaders("oz-cashflow", ozCashflowHeaders), rows, data.totals, null, colViewPinKeys("oz-cashflow"));
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить движение средств: ' + escapeHtml(err.message) + "</div>";
    setStatus("Ошибка: " + err.message, { error: true });
  }
}

const ozAccrualHeaders = [
  { k: "date", label: "Дата", render: cellFmts.text , tip: "День начисления из /v1/finance/accrual/by-day. Отчёт запрашивается по одному дню; в строке хранится дата операции."},
  { k: "bucket", label: "Корзина", render: cellFmts.tag , tip: "Тип начисления, разложенный при загрузке: sale — продажа, commission — комиссия, logistics — доставка, services — услуги/эквайринг, other — без привязки к товару."},
  { k: "sku", label: "SKU", render: cellFmts.text , tip: "SKU Ozon из позиции постинга или услуги. У корзины other (страховка, размещение и пр.) SKU пустой."},
  { k: "offer_id", label: "Артикул", render: cellFmts.text , tip: "Ваш артикул: Ozon его не отдаёт, он подставляется по SKU из загруженной детализации. Нет SKU в детализации — артикул пустой."},
  { k: "unit_number", label: "Постинг", render: cellFmts.text , tip: "Номер постинга (unit_number) из отчёта. У начислений без постинга (услуги, прочее) поле пустое."},
  { k: "type_id", label: "Тип", num: true, render: cellFmts.int , tip: "Код типа начисления Ozon: 0 — продажа, 69 — комиссия с продаж, далее коды услуг доставки, платных услуг и прочих начислений."},
  { k: "quantity", label: "Кол-во", num: true, render: cellFmts.int , tip: "Количество единиц, к которым относится начисление (из позиции постинга или услуги). У корзины other всегда 0."},
  { k: "amount", label: "Сумма", num: true, render: cellFmts.moneyCls , tip: "Начисленная сумма (accrued) со знаком: продажа — плюс, комиссия, логистика, услуги, прочее — минус. В итоге строки они складываются."},
  { k: "seller_price", label: "Цена", num: true, render: cellFmts.money , tip: "Цена продавца за штуку (seller_price) из позиции постинга. У корзин services и other — 0."},
  { k: "sale_price", label: "Цена покуп.", num: true, render: cellFmts.money , tip: "Цена покупателя за штуку (sale_price). У комиссии и логистики — цена той же позиции, у услуг и прочего — 0."},
  { k: "accrual_id", label: "ID начисления", render: cellFmts.text , tip: "accrual_id из отчёта Ozon. Входит в ключ идемпотентности вместе с датой, корзиной, типом и SKU: перезагрузка дня перезаписывает строки."},
];

async function renderOzAccrual() {
  const box = document.getElementById("ozAccrualTable");
  const p = paneDates();
  const msg = statusEl();
  try {
    const data = await api("/ozon/accrual-rows" + qs({
      date_from: p.date_from || undefined,
      date_to: p.date_to || undefined,
    }));
    const rows = data.rows || [];
    if (!rows.length && !data.count) {
      setStatus(emptyPeriodReason(data, "начислений Ozon",
        "Обновите: «Обновить базу» → OZON API ▸ Начисления."));
    } else if (msg) {
      let m = "Строк: " + fmt(data.count || 0);
      const s = data.totals && data.totals.amount;
      if (s != null) m += " · итог: " + fmtMoney(s);
      msg.textContent = m;
    }
    pagedTable(box, colViewHeaders("oz-accrual", ozAccrualHeaders), rows, data.totals, "#ozAccrualTablePager", colViewPinKeys("oz-accrual"));
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить начисления: ' + escapeHtml(err.message) + "</div>";
    setStatus("Ошибка: " + err.message, { error: true });
  }
}

function ozAccrualExportUrl() {
  const p = paneDates();
  const q = qs({ date_from: p.date_from || undefined, date_to: p.date_to || undefined });
  const cp = colViewParam("oz-accrual");
  return "/api/export/ozon/accrual-rows" + q + (cp ? (q ? "&" : "?") + cp : "");
}

async function downloadOzAccrualExcel() {
  const msg = statusEl();
  msg.textContent = "Формирую Excel…";
  try {
    const resp = await fetch(ozAccrualExportUrl());
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
  const msg = statusEl();
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

async function uploadProductsFiles(files) {
  if (!files || !files.length) return;
  const fd = new FormData();
  fd.append("file", files[0]);
  const msg = statusEl();
  msg.textContent = "Загружаю " + files[0].name + "…";
  try {
    const resp = await fetch("/api/import/products", { method: "POST", body: fd });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || resp.status);
    let m = "Товаров: строк " + fmt(data.imported || data.rows || 0);
    if (data.total != null && Number(data.total) !== Number(data.imported)) m += " из " + fmt(data.total);
    if (data.errors && data.errors.length) m += "; с ошибками: " + fmt(data.errors.length);
    msg.textContent = m;
    const input = document.getElementById("productsFile");
    if (input) input.value = "";
    pullsCache = null;
    updateLastPull("products");
    if (currentTab === "products") await renderProducts();
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

async function uploadNetCostFiles(files) {
  if (!files || !files.length) return;
  const fd = new FormData();
  fd.append("file", files[0]);
  const msg = statusEl();
  msg.textContent = "Загружаю " + files[0].name + "…";
  try {
    const resp = await fetch("/api/import/net-cost", { method: "POST", body: fd });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.detail || resp.status);
    let m = "Себестоимость: строк " + fmt(data.imported || data.rows || 0);
    if (data.total != null && Number(data.total) !== Number(data.imported)) m += " из " + fmt(data.total);
    if (data.errors && data.errors.length) m += "; с ошибками: " + fmt(data.errors.length);
    msg.textContent = m;
    const input = document.getElementById("netCostFile");
    if (input) input.value = "";
    pullsCache = null;
    updateLastPull("products");
    if (currentTab === "products") await renderProducts();
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

function initProductsUpload() {
  const file = document.getElementById("productsFile");
  const drop = document.getElementById("productsDrop");
  if (file) file.addEventListener("change", () => busyRun(() => uploadProductsFiles(file.files)));
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
      if (e.dataTransfer && e.dataTransfer.files) busyRun(() => uploadProductsFiles(e.dataTransfer.files));
    });
  }
}

function initNetCostUpload() {
  const file = document.getElementById("netCostFile");
  const drop = document.getElementById("netCostDrop");
  if (file) file.addEventListener("change", () => busyRun(() => uploadNetCostFiles(file.files)));
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
      if (e.dataTransfer && e.dataTransfer.files) busyRun(() => uploadNetCostFiles(e.dataTransfer.files));
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
    if (likeEl) onTabInput(ids.prefix + "Like", name, () => renderCards(name));
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
  window_days: "Окно расчёта скорости продаж, дн",
  target_doc: "Целевой запас в днях продаж (DOC), дн",
  doc_low: "DOC дефицита ≤, дн",
  doc_high: "DOC перезапаса ≥, дн",
  floor_margin_pct: "Минимальная маржа, %",
  max_discount_pct: "Максимальная скидка от базовой цены, %",
  max_raise_pct: "Максимальный рост цены за шаг, %",
  max_drop_pct: "Максимальное снижение цены за шаг, %",
  min_delta_pp: "Мин. изменение скидки, п.п.",
  cooldown_days: "Кулдаун между изменениями цены, дн",
  season_adj: "Экстраполяция тренда продаж (сезонность)",
  season_damp: "Ослабление экстраполяции тренда, 0..1",
  min_days_with_sales: "Мин. дней с продажами для учёта тренда",
  hot_conv_pct: "Конверсия горячего спроса ≥, %",
  hot_backlog_factor: "Горячий спрос: в корзине/заказов ≥, ×",
  return_penalty: "Порог доли возвратов/отмен, 0..1",
  dead_stock_days: "Дней без продаж → мёртвый запас, ≥",
  low_conv_pct: "Низкая конверсия витрины <, %",
  min_rating_reviews: "Рейтинг по отзывам ≥, балл (ценный товар)",
  fallback_window_days: "Окно расчёта unit-экономики по факт., дн",
  raise_pct_replenishable: "Рост цены для докупаемых, %",
  min_rating_for_raise: "Мин. рейтинг для роста цены, балл",
  min_conv_buyout_for_raise: "Мин. конв. выкупа для роста цены, %",
  max_cancel_ratio_for_raise: "Макс. доля отмен для роста, 0..1",
  max_return_rate_for_raise: "Макс. возвраты для роста цены, %",
  strong_rating: "Сильные сигналы: рейтинг ≥, балл",
  strong_buyout_conv: "Сильный сигнал: конв. выкупа ≥, %",
  strong_return_rate: "Сильный сигнал: возвраты ≤, %",
  strong_margin_pct: "Сильный сигнал: факт. маржа ≥, %",
  raise_boost_pct: "Uplift роста при ≥2 сильных сигналах, %",
  dead_min_discount: "Мин. скидка мёртвого (стоп деления), %",
  prefer_raise: "Противовес: предпочитать подъём цены",
  prefer_raise_bias: "Противовес: сдвиг порогов DOC",
  use_inventory: "Остаток: дефицит или перезапас на складе",
  use_sales: "Продажи: скорость и динамика реализации",
  use_orders: "Заказы и конверсия (витринная воронка)",
  use_margin: "Прибыль/убыток: фактическая маржа",
  use_replenishable: "Докупаемость: регулярность поставок",
  use_season: "Сезонность: тренд и экстраполяция продаж",
  use_quality: "Качество: рейтинг, выкупы, отмены, возвраты",
  use_reviews: "Рейтинг по отзывам (ценность товара)",
  use_returns: "Возвраты/отмены: защита от мыльного спроса",
  promo_enabled: "Разгружать залежалое через акции WB",
  promo_push_pct: "Шаг скидки при разгрузке, п.п.",
  promo_max_beyond_floor_pp: "Скидка ниже пола при разгрузке, ≤ п.п.",
  promo_max_rows: "Товаров за проход, ≤ шт",
  promo_min_stale_days: "Порог «залежалось», дн",
  promo_protect_velocity: "Защита живых, шт/дн",
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
  season_damp: "Насколько ослабляем экстраполяцию тренда: 0 = полностью игнорировать, 1 = переносить тренд без смягчения. 0.5 — компромисс. Обычно не требует изменения.",
  min_days_with_sales: "Минимум дней с ненулевыми продажами в окне, чтобы вообще применять поправку тренда. Меньше — зашумленную динамику прогнозировать нельзя.",
  hot_conv_pct: "Конверсия (покупки/просмотры) выше этого %, % — признак «горячего» спроса: товару можно поднимать цену даже при дефиците.",
  hot_backlog_factor: "Признак горячего спроса: число покупателей «в корзине» больше числа заказов в это число раз.",
  return_penalty: "Если доля возвратов/отмен больше этой величины (0..1), продажи считаются «шумными» и товар не трогаем.",
  dead_stock_days: "Если продаж не было столько дней подряд → мёртвый запас, цену снижаем.",
  low_conv_pct: "Много «в корзине», но конверсия ниже этого %, % — интерес без покупок; товар пропускаем.",
  min_rating_reviews: "Рейтинг по отзывам из воронки продаж (1..5): чем выше — тем меньше скидка при снижении, ведь товар ценный. При рейтинге ≥ этого порога снижение ограничивается, а при 5,0 скидка не увеличивается вовсе. 0 = нет данных — ограничений нет.",
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
  use_inventory: "Остаток превращается в дни запаса (DOC): DOC выше целевого → перезапас (цена вниз), DOC ниже порога дефицита → товар уходит по «горячей» схеме (цена вверх). Выключено — остаток полностью не влияет на решение.",
  use_sales: "Скорость и динамика продаж за окно: нулевые продажи N дней → «мёртвый запас» (скидка делится пополам до минимума), ускорение/замедление корректирует глубину снижения. Выключено — продажи не влияют на решение.",
  use_orders: "Витринная воронка (заказы, просмотры, корзины): много «в корзине» при высокой конверсии → «горячий» спрос (можно поднимать цену даже в дефиците), интерес без покупок → товар пропускаем. Выключено — воронка не влияет на решение.",
  use_margin: "Фактическая выручка и маржа: из детализации считается точка безубыточности и «пол цены» (себестоимость+логистика+комиссия+хранение+запас маржи). Ниже пола цена не уходит. Выключено — нехватка финансовых данных не блокирует решение, но цена может уйти в убыток.",
  use_replenishable: "Пополняемый ли товар (Windows «докупаемость»): регулярные поставки → поднимаем умеренно и плавно, последние единицы → можно смелее реагировать на дефицит. Выключено — различие докупаемых/последних единиц игнорируется.",
  use_season: "Сезонность/тренд продаж: скорость экстраполируется по динамике (рост/падение → будущая скорость), глубина снижения корректируется. Играет роль в «мёртвый»/«перезапас» расчётах. Выключено — тренд не учитывается вовсе.",
  use_quality: "Качество спроса: рейтинг магазина, конверсия выкупа, доля отмен и возвратов из детализации. Плохие показатели блокируют повышение цены; ≥2 сильных сигналов — «качество» добавляет uplift к росту. Выключено — качество не ограничивает повышение (по умолчанию выкл.).",
  use_returns: "Защита от «мыльного» спроса: когда доля возвратов/отмен превышает порог, продажи считаются шумными и товар не трогаем (повышение/снижение замораживается). Выключено — эта защита не действует (по умолчанию выкл.).",
  use_reviews: "Рейтинг по отзывам из воронки продаж (1..5): высокий рейтинг = ценный товар, скидку при снижении не раздаём (см. «Спрос и воронка» → «Рейтинг по отзывам ≥»). Выключено — рейтинг не ограничивает скидку.",
  promo_enabled: "Разгрузка склада через акции WB. Правило выбирает товары, которые долго не продаются (нет продаж ≥ «Порог залежалось»), дешевле всего режутся по себестоимости и больше всего лежат на складе, и поднимает их скидку на «Шаг скидки». Смысл — продать залежалое и освободить капитал. ВАЖНО: это НЕ попытка набрать участие в акции. WB отбирает товары в автоакции сам (для «хитов» — с ≥1 заказом за месяц), список кандидатов нам недоступен, и «доля участия» в API — это доля наших товаров в акции, а не порог скидки. Без галки автопилот лишь показывает инфо об акциях, скидку не трогая.",
  promo_push_pct: "На сколько п.п. поднять скидку отобранному товару (по умолчанию 2). Суммарное падение скидки за проход при этом всё равно ограничено общим лимитом «Максимальное снижение, %».",
  promo_max_beyond_floor_pp: "Сколько п.п. целевой скидке разрешено опуститься ниже порога безубыточности ради разгрузки (по умолчанию 5). Потолок «Максимальная скидка» при этом сохраняется — глубже него разгрузка не идёт.",
  promo_max_rows: "Сколько товаров максимум разгружать за один проход (по умолчанию 30). Если подходящих товаров больше — берутся самые «жертвенные» по оценке.",
  promo_min_stale_days: "Товары с продажами недавле этого срока разгрузкой не трогаются (по умолчанию 30 дн.). Увеличьте, если хотите разгружать только совсем остывшие позиции.",
  promo_protect_velocity: "Защита живых товаров: если сбыт быстрее этого значения (шт/дн), разгрузка товар не трогает (по умолчанию 0.5). Поставьте 0, чтобы отключить защиту и резать всё, что прошло порог залежалось.",
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

// Подсветка целевой скидки: дельта к текущей. Скидка падает (d<0) → цена вверх (▲, зелёный),
// растёт (d>0) → скидка больше, цена вниз (▼, красный); мелкая дельта (<0.5 п.п.) — жёлтая (→).
function targetDiscCell(v, r) {
  if (v == null) return "—";
  const cur = Number(r && r.current_discount);
  const d = isNaN(cur) ? 0 : Number(v) - cur;
  const mag = Math.abs(d);
  let arrow = "", cls = "";
  if (d !== 0) {
    if (mag < 0.5) { arrow = "→ "; cls = "warn"; }
    else if (d < 0) { arrow = "▲ "; cls = "pos"; }
    else { arrow = "▼ "; cls = "neg"; }
  }
  return arrow ? `<span class="${cls}">${arrow}${fmt(v)}%</span>` : fmt(v) + "%";
}
// Подсветка целевой цены: дельта к текущей цене. Дороже (d>0) → ▲ зелёный, дешевле (d<0) → ▼ красный,
// дельта <0.5% от текущей цены — жёлтая (→).
function targetVisCell(v, r) {
  if (v == null) return "—";
  const base = Number(r && r.current_vis);
  const d = isNaN(base) || base === 0 ? 0 : Number(v) - base;
  const mag = d === 0 ? 0 : Math.abs(d) / Math.abs(base);
  let arrow = "", cls = "";
  if (d !== 0) {
    if (mag < 0.005) { arrow = "→ "; cls = "warn"; }
    else if (d > 0) { arrow = "▲ "; cls = "pos"; }
    else { arrow = "▼ "; cls = "neg"; }
  }
  return arrow ? `<span class="${cls}">${arrow}${fmtMoney(v)}</span>` : fmtMoney(v);
}

// Подсветка дельты скидки (целевая − текущая): скидка растёт (d>0) → цена вниз (▼, красный),
// падает (d<0) → цена вверх (▲, зелёный); мелкая дельта (<0.5 п.п.) — жёлтая (→).
function deltaDiscCell(v, r) {
  if (v == null) return "—";
  const d = Number(v);
  const mag = Math.abs(d);
  let arrow = "", cls = "";
  if (d !== 0) {
    if (mag < 0.5) { arrow = "→ "; cls = "warn"; }
    else if (d < 0) { arrow = "▲ "; cls = "pos"; }
    else { arrow = "▼ "; cls = "neg"; }
  }
  const s = (d > 0 ? "+" : "") + fmtFloat(d, 1) + "%";
  return arrow ? `<span class="${cls}">${arrow}${s}</span>` : s;
}

// Вклад акции WB в целевую скидку, п.п. Ноль у подавляющего большинства строк —
// показываем тире, чтобы колонка не пестрела «0.0». Знак цвета как у дельты скидки:
// скидка растёт → цена вниз (красный), падает → цена вверх (зелёный).
function promoDeltaCell(v) {
  if (v == null) return "—";
  const d = Number(v);
  if (!isFinite(d) || Math.abs(d) < 0.05) return "—";
  const s = (d > 0 ? "+" : "") + fmtFloat(d, 1) + " п.п.";
  return d > 0 ? `<span class="neg">${s}</span>` : `<span class="pos">${s}</span>`;
}

// Полнота оценки жертвенности: WB не отдаёт данные по участию конкретного
// артикула, а часть сигналов (остаток своего склада, история продаж) у нас
// есть не по всем товарам. Показываем это явно, чтобы «решение» не читалось
// как точный расчёт.
function promoConfidenceCell(v) {
  if (v == null) return "—";
  return v === "full" ? "<span class='pos'>полная</span>"
    : "<span style='color:#b8860b'>по фолбэку</span>";
}

// Прибыль по товару: маржа с единицы (руб) + рентабельность от выручки (%).
// v = margin_per_one (руб/шт), r.margin_pct — процент от выручки.
function profitCell(v, r) {
  if (v == null) return "—";
  const n = Number(v);
  if (!isFinite(n) || !n) return "—";
  const p = Number(r && r.margin_pct);
  return fmtMoney(n) + (isFinite(p) && p ? " <span style='opacity:.65'>· " + fmt(p) + "%</span>" : "");
}

const pricingHeaders = [
  { k: "article", label: "Артикул", render: cellFmts.text , tip: "Артикул каталога в верхнем регистре. В расчёт попадают только товары с карточкой WB (есть nmID) и с хоть каким-то сигналом жизни."},
  { k: "nm_id", label: "Артикул WB", render: cellFmts.text , tip: "Код номенклатуры WB. Именно по нему автопилот меняет скидку через WB API; базовая цена не трогается."},
  { k: "target_discount", label: "Целевая скидка, %", num: true, render: targetDiscCell , tip: "Рекомендуемая скидка по правилам R1-R10: зависит от покрытия остатка (DOC), спроса, конверсии, рейтинга и рентабельности. Ограничена ценой безубыточности и потолком скидки."},
  { k: "current_discount", label: "Скидка сейчас, %", num: true, render: (v) => v == null ? "—" : fmt(v) + "%" , tip: "Текущая скидка в карточке WB (поле discount из прайса). Если скидка не задана, берётся из расчёта цены со скидкой."},
  { k: "delta_discount", label: "Дельта скидки", num: true, render: deltaDiscCell , tip: "Целевая минус текущая, п.п.: плюс = автопилот хочет увеличить скидку (снизить цену), минус = поднять цену. Пусто, если решения нет."},
  { k: "target_vis", label: "Целевая цена, руб", num: true, render: targetVisCell , tip: "Цена для покупателя при целевой скидке = базовая цена × (1 − скидка/100). Ниже точки безубыточности автопилот не опускает."},
  { k: "margin_per_one", label: "Прибыль/шт, руб", num: true, render: profitCell , tip: "Фактическая прибыль с единицы по детализации продаж WB за окно: прибыль ÷ проданные штуки. Рядом в скобках — рентабельность от выручки."},
  { k: "current_vis", label: "Цена сейчас, руб", num: true, render: cellFmts.money , tip: "Действующая цена покупателя = базовая цена WB × (1 − текущая скидка/100). Сравните с целевой ценой, чтобы увидеть эффект решения."},
  { k: "net_cost", label: "Себестоимость", num: true, render: cellFmts.moneyZero , tip: "Себестоимость единицы из каталога. Ноль = не заведена: тогда пол безубыточности считается от нуля и решение принимается вслепую."},
  { k: "avg_price", label: "Ср. цена факт, руб", num: true, render: cellFmts.money , tip: "Средняя цена заказа из воронки продаж WB за срез. Если воронки нет, в расчётах она заменяется витринной ценой, и защита «цена ниже себестоимости» ослабевает."},
  { k: "stock", label: "Остаток", num: true, render: cellFmts.int , tip: "Остаток WB по последнему срезу стоков: quantity + в пути. Нет данных об остатках = автопилот пропускает товар (SKIP)."},
  { k: "buyouts", label: "Выкупы, шт", num: true, render: cellFmts.int , tip: "Выкупы за срез воронки продаж: заказы, которые покупатель забрал. Отличаются от заказов из-за отмен и невыкупов."},
  { k: "backlog", label: "В корзине", num: true, render: cellFmts.int , tip: "Добавления в корзину минус заказы, не меньше нуля: спрос есть, а сделки нет. Используется как сигнал «много в корзинах, но не покупают»."},
  { k: "conv_pct", label: "Конверсия, %", num: true, render: cellFmts.pct , tip: "Заказы ÷ просмотры × 100 по воронке. Ниже 0,7% автопилот не увеличивает скидку, при большом «в корзине» — пропускает товар."},
  { k: "margin_pct", label: "Маржа факт, % от выручки", num: true, render: cellFmts.pct , tip: "Рентабельность за окно по детализации: прибыль ÷ выручка × 100. Проверяйте согласованность с полем прибыли на единицу."},
  { k: "product_rating", label: "Рейтинг товара", num: true, render: (v) => v == null ? "—" : Number(v).toFixed(1) , tip: "Рейтинг карточки WB 1..5 из воронки. Ниже порога качества автопилот не поднимает цену, даже при дефиците."},
  { k: "action", label: "Решение", render: actionCell , tip: "Итоговое действие: RAISE — снизить скидку (поднять цену), LOWER — увеличить скидку, HALVE — пополам для мёртвых товаров, HOLD — не менять, SKIP — пропустить (нет данных, кулдаун, отказы по качеству)."},
  { k: "reason", label: "Причина", render: cellFmts.text , tip: "Текстовое объяснение решения: покрытие остатка (DOC), конверсия, корзины, рейтинг, отказы качества, кулдаун после прошлой правки."},
  { k: "name", label: "Наименование", render: cellFmts.text , tip: "Наименование товара из каталога products."},
  { k: "stock_wb", label: "Остаток WB", num: true, render: cellFmts.int , tip: "Остаток WB из воронки продаж (product.stocks.wb) — снимок на дату среза. Может отличаться от колонки «Остаток», взятой из последнего среза стоков."},
  { k: "doc", label: "DOC, дн", num: true, render: (v) => v == null ? "—" : fmt(v) , tip: "Покрытие остатка продажами: остаток ÷ скорость (шт/дн). Ниже нижней границы — дефицит, выше верхней — перезапас, вокруг целевого — шаг в любую сторону."},
  { k: "velocity", label: "v, шт/дн", num: true, render: (v) => v == null ? "—" : Number(v).toFixed(1) , tip: "Скорость продаж за окно: (продажи − возвраты) ÷ число дней окна. Ноль = продаж нет, автопилот считает товар мёртвым."},
  { k: "trend", label: "Тренд", num: true, render: (v) => v == null ? "—" : (v && v > 1 ? "<span class='pos'>▲ " : v && v < 1 ? "<span class='neg'>▼ " : "<span>") + (v || 0).toFixed(2) + "</span>" , tip: "Скорость текущего окна ÷ скорость предыдущего. Больше 1 — рост, меньше 1 — спад. Используется в сезонной поправке прогноза скорости."},
  { k: "conv_buyout_percent", label: "Конв. выкупа, %", num: true, render: cellFmts.pct , tip: "Конверсия выкупа из воронки WB: доля выкупов среди заказов. Ниже 40% автопилот не поднимает цену — спрос ненадёжный."},
  { k: "cancel_sum", label: "Отмены, руб", num: true, render: cellFmts.money , tip: "Сумма отменённых заказов из воронки, ₽. Рост вместе с заказами — сигнал мыльного спроса."},
  { k: "add_to_wishlist", label: "В избранное", num: true, render: cellFmts.int , tip: "Добавления в избранное и закладки за срез воронки. Интерес без заказа — повод не давать лишнюю скидку."},
  { k: "return_rate", label: "Возвраты, %", num: true, render: cellFmts.pct , tip: "Доля возвратов по детализации: возвраты ÷ (продажи + возвраты) × 100. Выше порога автопилот блокирует повышение цены как брак/неликвид."},
  { k: "margin_pct_at_target", label: "Маржа при цели, %", num: true, render: cellFmts.pct , tip: "Прогноз прибыли с единицы при целевой цене, %: (цена × (1 − комиссия) − логистика − хранение − услуги − себестоимость) ÷ цена. Пусто, если нет unit-экономики."},
  { k: "promo_count", label: "Акций WB", num: true, render: (v) => v == null || !v ? "—" : fmt(v) , tip: "Сколько действующих акций WB (Календарь акций) открыто на этот период. Если 0 — промо-инфо пустое, разгрузка не срабатывает."},
  { k: "promo_names", label: "Акции WB", render: cellFmts.text , tip: "Названия активных акций (до трёх, дальше — многоточие). Справочно: какие акции берутся в расчёт при разгрузке."},
  { k: "promo_part_pct", label: "Участие в акции, %", num: true, render: (v) => v == null || !v ? "—" : fmt(v) + "%" , tip: "Доля НАШИХ товаров в акции — так это считает WB (в_promo ÷ (в_promo + не_в_промо)). Это НЕ процент скидки и не показатель конкретного артикула."},
  { k: "promo_tier_pct", label: "Доля для след. буста, %", num: true, render: (v) => v == null ? "—" : fmt(v) + "%" , tip: "ParticipationRate следующей ступени акции: доля наших товаров, которую WB хочет видеть в акции, чтобы дать буст. Не порог скидки и не цель правила — повлиять на неё выбором товаров нельзя, WB отбирает товары сам."},
  { k: "promo_tier_boost", label: "Буст след. ступени", num: true, render: (v) => v == null ? "—" : "×" + (Number(v) % 1 === 0 ? Number(v) : Number(v).toFixed(1)) , tip: "Буст, который даст следующая ступень акции. Достаётся ВСЕМ участникам акции, а не отдельному товару — поэтому своей «ступени» у строки нет."},
  { k: "promo_need_rows", label: "Надо в акцию, шт", num: true, render: (v) => v == null ? "—" : fmt(v) , tip: "Сколько наших товаров WB не хватает до следующего буста: нужная доля × ПУЛ WB (в_промо + не_в_промо) − уже в промо. Справочная цифра: раньше считалось от числа всех наших товаров, что завышало оценку в разы."},
  { k: "promo_cap_pct", label: "Потолок промо, %", num: true, render: (v) => v == null ? "—" : fmt(v) + "%" , tip: "Потолок скидки самой акции из описания WB («промо-скидка не более N%»). Справочно; автопилот свой потолок считает от безубытка."},
  { k: "promo_push_applied", label: "Разгружен", render: (v) => v ? "<span class='pos'>да</span>" : "—" , tip: "Правило разгрузки выбрало этот товар: скидка увеличена на шаг promo_push_pct ради продажи залежалого."},
  { k: "promo_delta_discount", label: "Вклад разгрузки, п.п.", num: true, render: promoDeltaCell , tip: "На сколько п.п. разгрузка подняла целевую скидку сверх решения остальных правил. Общая «Дельта скидки» остаётся суммой всех правил."},
  { k: "promo_score", label: "Оценка", num: true, render: (v) => v == null ? "—" : Number(v).toFixed(2) , tip: "Оценка жертвенности 0…2.6: дольше без продаж (до 1.0) + дешевле резать по себестоимости (до 0.6) + больше лежит на складе (до 1.0). Больше = товар лучше подходит под разгрузку."},
  { k: "promo_score_confidence", label: "Полнота оценки", render: promoConfidenceCell , tip: "«Полная» — все сигналы взяты точными значениями. «По фолбэку» — часть данных отсутствовала (например, свой склад не заведён, продаж не было вовсе), оценка приблизительная."},
  { k: "last_sale_days_ago", label: "Дней без продаж", num: true, render: (v) => v == null ? "—" : (Number(v) >= 9999 ? "никогда" : fmt(v)) , tip: "Дней с последней продажи товара. Основной сигнал разгрузки: чем больше, тем сильнее товар просится на распродажу. «никогда» = продаж не было вовсе (или новинка без данных)."},
  { k: "revenue_per_one", label: "Ср. чек, руб", num: true, render: cellFmts.money , tip: "Фактическая цена продажи по детализации WB: выручка ÷ проданные штуки, ₽."},
  { k: "income_per_one", label: "К переч./шт, руб", num: true, render: cellFmts.money , tip: "К перечислению на единицу по детализации WB: сколько денег за одну проданную штуку получает продавец."},
  { k: "commission_per_one", label: "Комиссия/шт, руб", num: true, render: cellFmts.money , tip: "Комиссия КВВ на единицу по детализации WB. Учтена в расчёте пола безубыточности через долю комиссии в выручке."},
  { k: "logistics_per_one", label: "Логистика/шт, руб", num: true, render: cellFmts.money , tip: "Логистика (туда и обратно) на единицу по детализации WB. Входит в формулу минимальной цены."},
  { k: "storage_per_one", label: "Хранение/шт, руб", num: true, render: cellFmts.money , tip: "Хранение на единицу по детализации WB, включая оценку безартикульных плат. Тоже входит в формулу минимальной цены."},
  { k: "price", label: "Цена базовая, руб", num: true, render: cellFmts.money , tip: "Базовая цена карточки WB (price из прайса), от неё считается витринная цена со скидкой."},
  { k: "floor_price", label: "Пол (break-even), руб", num: true, render: cellFmts.money , tip: "Минимальная цена безубыточности: себестоимость + логистика + комиссия + хранение + услуги, ₽/шт."},
  { k: "eff", label: "База расчёта, руб", num: true, render: cellFmts.money , tip: "Цена, от которой считается целевая скидка: витринная или ср. цена факт (если воронки нет)."},
  { k: "max_discount_item", label: "Макс. скидка, %", num: true, render: (v) => v == null ? "—" : fmt(v) + "%" , tip: "Потолок скидки для этого товара, % — ограничение правила, ниже которого цена не опускается."},
  { k: "status", label: "Статус", render: cellFmts.text , tip: "Статус товара в расчёте цен — те же значения, что в служебной колонке решений."},
  { k: "replenishable", label: "Докупаемый", render: (v) => v ? "да" : "нет" , tip: "Флаг каталога «докупаемый»: дефицитный товар автопилот не спешит удорожать."},
  { k: "detail_sells", label: "Продано в детализации, шт", num: true, render: cellFmts.int , tip: "Продано по детализации продаж WB за окно, шт."},
  { k: "detail_returns_qty", label: "Возвращено в детализации, шт", num: true, render: cellFmts.int , tip: "Возвраты по детализации продаж WB за окно, шт."},
];

// ----------------------------------------------------- Регистрация «Вида таблицы» по разделам
registerColView("wb-detail", {
  storageKey: "wbDetailCols", pinnable: true,
  mode: () => { const rawEl = document.getElementById("wbDetailRaw"); return rawEl && rawEl.checked ? "rows" : "summary"; },
  sets: {
    rows: { headers: wbDetailRowHeaders },
    summary: { headers: wbDetailSummaryHeaders },
  },
});
registerColView("oz-detail", {
  storageKey: "ozDetailCols", pinnable: true,
  mode: () => { const rawEl = document.getElementById("ozDetailRaw"); return rawEl && rawEl.checked ? "rows" : "summary"; },
  sets: {
    rows: { headers: ozDetailRowHeaders },
    summary: { headers: ozDetailSummaryHeaders },
  },
});
registerColView("wb-cards", { storageKey: "wbCardsCols", pinnable: true, headers: cardsHeaders });
registerColView("wb-stock", {
  storageKey: "wbStockCols", pinnable: true,
  mode: () => { const agg = document.getElementById("wbStockAgg"); return agg && agg.checked ? "agg" : "base"; },
  sets: {
    base: { headers: wbStockHeaders },
    agg: { headers: wbStockAggHeaders },
  },
});
registerColView("wb-funnel", {
  storageKey: "wbFunnelCols", pinnable: true,
  mode: () => { const exp = document.getElementById("wbFunnelExpanded"); return exp && exp.checked ? "expanded" : "compact"; },
  sets: {
    compact: { headers: wbFunnelCompact },
    expanded: { headers: wbFunnelHeaders },
  },
});
registerColView("wb-sales", { storageKey: "wbSalesCols", pinnable: true, headers: salesHeaders });
registerColView("wb-prices", {
  storageKey: "wbPricesCols", pinnable: true,
  mode: () => { const agg = document.getElementById("wbPriceAgg"); return agg && agg.checked ? "agg" : "base"; },
  sets: {
    base: { headers: wbPricesHeaders },
    agg: { headers: wbPricesAggHeaders },
  },
});
registerColView("wb-storage", { storageKey: "wbStorageCols", pinnable: true, headers: wbStorageHeaders });
registerColView("oz-cards", { storageKey: "ozCardsCols", pinnable: true, headers: cardsHeaders });
registerColView("oz-stock", {
  storageKey: "ozStockCols", pinnable: true,
  mode: () => { const agg = document.getElementById("ozStockAgg"); return agg && agg.checked ? "agg" : "base"; },
  sets: {
    base: { headers: wbStockHeaders },
    agg: { headers: wbStockAggHeaders },
  },
});
registerColView("oz-prices", {
  storageKey: "ozPricesCols", pinnable: true,
  mode: () => { const agg = document.getElementById("ozPriceAgg"); return agg && agg.checked ? "agg" : "base"; },
  sets: {
    base: { headers: wbPricesHeaders },
    agg: { headers: wbPricesAggHeaders },
  },
});
registerColView("oz-realization", { storageKey: "ozRealCols", pinnable: true, headers: salesHeaders });
registerColView("oz-cashflow", { storageKey: "ozCashflowCols", pinnable: true, headers: ozCashflowHeaders });
registerColView("oz-accrual", { storageKey: "ozAccrualCols", pinnable: true, headers: ozAccrualHeaders });
registerColView("oz-placement", {
  storageKey: "ozPlacementCols", pinnable: true,
  mode: () => { const rawEl = document.getElementById("ozPlacementRaw"); return rawEl && rawEl.checked ? "rows" : "summary"; },
  sets: {
    rows: { headers: ozPlacementRowHeaders },
    summary: { headers: ozPlacementSummaryHeaders },
  },
});
registerColView("products", {
  storageKey: "productsCols", pinnable: true,
  mode: () => { const s = document.getElementById("productsSizes"); return s && s.checked ? "sizes" : "agg"; },
  sets: {
    agg: { headers: productsBaseHeaders },
    sizes: { headers: productsSizeHeaders },
  },
});
registerColView("dash-profit", { storageKey: "dashProfitCols", pinnable: true, reloadTab: "dashboard", headers: dashHeaders.tops });
registerColView("dash-loss", { storageKey: "dashLossCols", pinnable: true, reloadTab: "dashboard", headers: dashHeaders.tops });
registerColView("dash-price", { storageKey: "dashPriceCols", pinnable: true, reloadTab: "dashboard", headers: dashHeaders.price });
registerColView("dash-prefix", { storageKey: "dashPrefixCols", pinnable: true, reloadTab: "dashboard", headers: dashHeaders.prefix });
registerColView("margin-funnel", {
  storageKey: "marginFunnelCols", pinnable: true,
  headers: funnelHeaders,
});
registerColView("margin-detail", {
  storageKey: "marginDetailCols", pinnable: true,
  headers: marginHeaders,
});
registerColView("margin-ozon-detail", {
  storageKey: "marginOzonDetailCols", pinnable: true,
  mode: () => (ozBySize("margin-ozon-detail") ? "size" : "base"),
  sets: {
    base: { headers: ozonMarginHeaders },
    size: { headers: ozonMarginHeaders },
  },
});
registerColView("replenish", {
  storageKey: "replenishCols", pinnable: true,
  mode: replenishView,
  sets: {
    article: { headers: replenishHeaders },
    sizes: { headers: replenishSizeHeaders },
  },
});
registerColView("pricing", {
  storageKey: "pricingCols", pinnable: true,
  headers: pricingHeaders,
});

// ----------------------------------------------------- Фильтр по колонкам таблицы автопилота
let _pricingColFilters = loadPricingColFilters();
function loadPricingColFilters() {
  try { return JSON.parse(localStorage.getItem("pricing_filters") || "{}"); }
  catch (e) { return {}; }
}
function savePricingColFilters(f) { localStorage.setItem("pricing_filters", JSON.stringify(f)); }
function pricingFiltersOn() {
  const v = localStorage.getItem("pricing_filters_on");
  return v == null ? false : v === "1";
}
function setPricingFiltersOn(on) { localStorage.setItem("pricing_filters_on", on ? "1" : "0"); }

// Числовой фильтр: диапазон «10..50», оператор «>100», «<5», «>=3», «=200» или точное число.
function numMatch(v, raw) {
  if (v == null || v === "") return false;
  const n = Number(v);
  if (!isFinite(n)) return true;
  const s = String(raw).trim().replace(/\s+/g, "").replace(/,/g, ".");
  if (s === "") return true;
  const parts = s.split("..");
  if (parts.length === 2) {
    if (parts[0] !== "" && n < Number(parts[0])) return false;
    if (parts[1] !== "" && n > Number(parts[1])) return false;
    return true;
  }
  const m = s.match(/^(<=|>=|<|>|=)?([-+]?\d+(?:\.\d+)?)$/);
  if (!m) return true;
  const num = Number(m[2]);
  switch (m[1] || "=") {
    case "<": return n < num;
    case "<=": return n <= num;
    case ">": return n > num;
    case ">=": return n >= num;
    default: return Number(n) === num;
  }
}
function pricingRowMatches(r, filters) {
  for (const k of Object.keys(filters)) {
    const raw = String(filters[k] || "").trim();
    if (!raw) continue;
    const hd = pricingHeaders.find((x) => x.k === k);
    if (hd && hd.num) {
      if (!numMatch(r[k], raw)) return false;
    } else {
      const label = String(r[k] == null ? "" : r[k]).toLowerCase();
      if (!label.includes(raw.toLowerCase())) return false;
    }
  }
  return true;
}

const schedulePricingFilter = debounceRun(() => {
  if (currentTab === "pricing") renderPricing(false);
}, 300);
// Бар фильтров: один input на каждую видимую колонку. Перестраивается только при смене набора
// колонок (чтобы не терять фокус при вводе), значения живут в _pricingColFilters.
function buildPricingFilterBar() {
  const host = $("#pricingFilters");
  if (!host) return;
  const headers = colViewHeaders("pricing", pricingHeaders);
  const sig = headers.map((h) => h.k).join(",");
  if (host._sig === sig) return;
  host._sig = sig;
  host.innerHTML = "";
  for (const h of headers) {
    const lbl = document.createElement("label");
    lbl.className = "pfilter";
    const t = document.createElement("span");
    t.className = "pfilter-lbl";
    t.textContent = h.label;
    lbl.appendChild(t);
    const inp = document.createElement("input");
    inp.dataset.k = h.k;
    inp.placeholder = h.num ? "мин..макс" : "содержит…";
    inp.title = h.num
      ? "Диапазон «10..50», оператор «>100», «<5», «=200» или точное число"
      : "Текст, который должен содержаться в ячейке";
    inp.value = _pricingColFilters[h.k] || "";
    inp.addEventListener("input", (e) => {
      _pricingColFilters[h.k] = e.target.value;
      savePricingColFilters(_pricingColFilters);
      schedulePricingFilter();
    });
    lbl.appendChild(inp);
    host.appendChild(lbl);
  }
}
// Активные фильтры — только по тем колонкам, что сейчас видимы и непусты.
function pricingEffectiveColFilters() {
  const visible = new Set(colViewHeaders("pricing", pricingHeaders).map((h) => h.k));
  const out = {};
  for (const k of Object.keys(_pricingColFilters)) {
    const v = String(_pricingColFilters[k] || "").trim();
    if (visible.has(k) && v) out[k] = v;
  }
  return out;
}

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

let _pricingModeInited = false;

function initPricingModeMenu(saved) {
  const btn = $("#btnpricingMode");
  const menu = $("#pricingModeMenu");
  const panel = $("#pricingModePanel");
  if (!btn || !menu || !panel) return;
  if (pricingModeCurrent === null) {
    pricingModeCurrent = (saved && (saved.mode === "old" || saved.mode === "new"))
      ? saved.mode
      : (pricingDefaults && pricingDefaults.mode === "old" ? "old" : "new");
  }
  const labels = { old: "Старая (не рекомендуется)", new: "Новая (рекомендуется)" };
  const refresh = () => {
    btn.textContent = "Версия \u25BE";
    btn.title = "Версия расчёта: " + (labels[pricingModeCurrent] || "Новая (рекомендуется)");
  };
  if (!_pricingModeInited) {
    _pricingModeInited = true;
    panel.innerHTML = "";
    for (const v of ["new", "old"]) {
      const row = document.createElement("div");
      row.className = "colview-row";
      const lbl = document.createElement("label");
      lbl.className = "chk";
      const rb = document.createElement("input");
      rb.type = "radio";
      rb.name = "pricingMode";
      rb.value = v;
      rb.checked = v === pricingModeCurrent;
      lbl.appendChild(rb);
      lbl.appendChild(document.createTextNode(
        v === "new"
          ? " Новая (рекомендуется) — правила R1-R10"
          : " Старая (не рекомендуется) — эвристика из finance"));
      row.appendChild(lbl);
      rb.addEventListener("change", () => {
        pricingModeCurrent = v;
        refresh();
        panel.classList.add("hidden");
        if (currentTab === "pricing") renderPricing(false);
      });
      panel.appendChild(row);
    }
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      panel.classList.toggle("hidden");
    });
    menu.addEventListener("click", (e) => e.stopPropagation());
    document.addEventListener("click", () => panel.classList.add("hidden"));
  }
  refresh();
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
  if (pricingModeCurrent === "old" || pricingModeCurrent === "new") s.mode = pricingModeCurrent;
  savePricingSettings(s);
  return s;
}

let _pricingShowMenuInited = false;

// Селектор «Показ»: чекбоксы «С нулевыми товарами» и «Скрыть "пропустить"».
function initPricingShowMenu() {
  const btn = $("#btnpricingShow");
  const menu = $("#pricingShowMenu");
  const panel = $("#pricingShowPanel");
  if (!btn || !menu || !panel) return;
  if (_pricingShowMenuInited) return;
  _pricingShowMenuInited = true;

  const mkRow = (text, id, checked) => {
    const row = document.createElement("div");
    row.className = "colview-row";
    const lbl = document.createElement("label");
    lbl.className = "chk";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.id = id;
    cb.checked = checked;
    lbl.appendChild(cb);
    lbl.appendChild(document.createTextNode(" " + text));
    row.appendChild(lbl);
    return { row, cb };
  };

  const zero = mkRow("С нулевыми товарами", "pricingShowZero", loadPricingSettings().show_zero === true);
  panel.appendChild(zero.row);
  zero.cb.addEventListener("change", () => {
    const ss = loadPricingSettings();
    ss.show_zero = zero.cb.checked;
    savePricingSettings(ss);
    panel.classList.add("hidden");
    if (currentTab === "pricing") renderPricing(false);
  });

  const skip = mkRow("Скрыть «держать» и «пропустить»", "pricingHideSkip", localStorage.getItem("pricing_hide_skip") !== "0");
  panel.appendChild(skip.row);
  skip.cb.addEventListener("change", () => {
    localStorage.setItem("pricing_hide_skip", skip.cb.checked ? "1" : "0");
    panel.classList.add("hidden");
    if (currentTab === "pricing") renderPricing(false);
  });

  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    panel.classList.toggle("hidden");
  });
  menu.addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", () => panel.classList.add("hidden"));
}

let _pricingSettingsMenuInited = false;
let _pricingSettingsVisible = localStorage.getItem("pricing_settings_visible") === "1";

// Показать/скрыть панели настроек. Вызывается и кнопкой «Настройки», и крестиком в панели.
function setPricingSettingsVisible(v) {
  _pricingSettingsVisible = !!v;
  localStorage.setItem("pricing_settings_visible", _pricingSettingsVisible ? "1" : "0");
  const box = $("#pricingSettings");
  if (box) box.classList.toggle("hidden", !_pricingSettingsVisible);
  const btn = $("#btnpricingSettings");
  if (btn) {
    btn.classList.toggle("active", _pricingSettingsVisible);
    btn.title = _pricingSettingsVisible ? "Свернуть панели настроек" : "Раскрыть панели настроек";
  }
}

// Селектор «Настройки»: сама кнопка раскрывает/сворачивает панели,
// каретка ▾ открывает меню с «Обновить» и «Развернуть всё».
function initPricingSettingsMenu() {
  const btn = $("#btnpricingSettings");
  const caret = $("#btnpricingSettingsCaret");
  const menu = $("#pricingSettingsMenu");
  const panel = $("#pricingSettingsPanel");
  const box = $("#pricingSettings");
  if (!btn || !caret || !menu || !panel || !box) return;
  if (_pricingSettingsMenuInited) return;
  _pricingSettingsMenuInited = true;

  const itemSave = document.createElement("button");
  itemSave.type = "button";
  itemSave.className = "menu-item";
  itemSave.textContent = "Обновить";
  itemSave.title = "Сохранить значения из формы и пересчитать рекомендации с новыми коэффициентами";
  const itemExpand = document.createElement("button");
  itemExpand.type = "button";
  itemExpand.className = "menu-item";
  panel.appendChild(itemSave);
  panel.appendChild(itemExpand);

  const groups = () => Array.from(box.querySelectorAll("details.p-group"));
  const refresh = () => {
    const g = groups();
    itemExpand.textContent = g.length && g.every((d) => d.open) ? "Свернуть всё" : "Развернуть всё";
  };

  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    setPricingSettingsVisible(!_pricingSettingsVisible);
  });

  caret.addEventListener("click", (e) => {
    e.stopPropagation();
    refresh();
    panel.classList.toggle("hidden");
  });

  itemSave.addEventListener("click", () => {
    collectPricingSettings();
    panel.classList.add("hidden");
    if (currentTab === "pricing") renderPricing(false);
  });

  itemExpand.addEventListener("click", () => {
    const g = groups();
    const anyClosed = g.some((d) => !d.open);
    g.forEach((d) => { d.open = anyClosed; });
    saveOpenPricingGroups(g);
    refresh();
    panel.classList.add("hidden");
  });

  menu.addEventListener("click", (e) => e.stopPropagation());
  document.addEventListener("click", () => panel.classList.add("hidden"));

  setPricingSettingsVisible(_pricingSettingsVisible);
}

// Порядок и группы настроек: каждый параметр в своей строке,
// группы сворачиваются (состояние запоминается в localStorage).
const PRICING_GROUPS = [
  { title: "Какие параметры влияют на цену", col: 2, keys: [
      "use_inventory", "use_sales", "use_orders", "use_margin",
      "use_replenishable", "use_season", "use_reviews", "use_quality",
      "use_returns",
  ] },
  { title: "Разгрузка акциями WB", col: 2, keys: [
      "promo_enabled", "promo_max_rows", "promo_min_stale_days",
      "promo_protect_velocity", "promo_push_pct", "promo_max_beyond_floor_pp",
  ] },
  { title: "Окно и скорость", col: 1, keys: [
      "window_days", "season_adj", "season_damp", "min_days_with_sales",
      "fallback_window_days",
  ] },
  { title: "DOC и запасы", col: 1, keys: [
      "target_doc", "doc_low", "doc_high", "dead_stock_days",
  ] },
  { title: "Цены и шаги", col: 1, keys: [
      "floor_margin_pct", "max_discount_pct", "max_raise_pct", "max_drop_pct",
      "min_delta_pp", "raise_pct_replenishable", "dead_min_discount",
  ] },
  { title: "Спрос и воронка", col: 1, keys: [
      "min_rating_reviews", "hot_conv_pct", "hot_backlog_factor",
      "low_conv_pct", "return_penalty",
  ] },
  { title: "Качество и рост", col: 1, keys: [
      "min_rating_for_raise", "min_conv_buyout_for_raise",
      "max_cancel_ratio_for_raise", "max_return_rate_for_raise",
      "strong_rating", "strong_buyout_conv", "strong_return_rate",
      "strong_margin_pct", "raise_boost_pct",
  ] },
  { title: "Применение и противовес", col: 1, keys: [
      "cooldown_days", "prefer_raise", "prefer_raise_bias",
  ] },
];
const PRICING_GROUPS_CLOSED_BY_DEFAULT = new Set(["Качество и рост"]);

function loadOpenPricingGroups() {
  try {
    const saved = JSON.parse(localStorage.getItem("pricing_open_groups") || "null");
    if (Array.isArray(saved)) return new Set(saved);
  } catch (e) { /* первый запуск */ }
  return null;
}

function saveOpenPricingGroups(detailsNodes) {
  const open = [];
  detailsNodes.forEach((d) => { if (d.open) open.push(d.dataset.group); });
  localStorage.setItem("pricing_open_groups", JSON.stringify(open));
}

function pricingParamRow(key, cur) {
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
    input.step = (key === "return_penalty" || key === "season_damp" || key === "prefer_raise_bias"
      || key === "min_rating_reviews" || key === "promo_push_pct"
      || key === "promo_protect_velocity"
      || key === "promo_max_beyond_floor_pp") ? "0.1" : "1";
    input.value = cur;
  }
  lbl.appendChild(input);
  return lbl;
}

async function buildPricingSettings() {
  if (!pricingDefaults) {
    try {
      const d = await api("/pricing/defaults");
      pricingDefaults = d.defaults;
    } catch (err) {
      $("#headerMsg").textContent = "Ошибка загрузки настроек: " + err.message;
      return;
    }
  }
  const saved = loadPricingSettings();
  const box = $("#pricingSettings");
  box.innerHTML = "";
  initPricingModeMenu(saved);
  const openSaved = loadOpenPricingGroups();
  const defaultOpen = new Set(
    PRICING_GROUPS.filter((g) => !PRICING_GROUPS_CLOSED_BY_DEFAULT.has(g.title)).map((g) => g.title)
  );
  const openSet = openSaved || defaultOpen;
  const detailsNodes = [];
  // Две таблицы-блока: параметры (слева, 1/3) | факторы (справа, 2/3).
  // Колонки создаём строго по данным — никаких лишних пустых контейнеров.
  const maxCol = Math.max(...PRICING_GROUPS.map((g) => g.col || 1));
  const cols = [];
  for (let c = 1; c <= maxCol; c++) {
    const col = document.createElement("div");
    col.className = "pricing-col pricing-col-" + c;
    cols.push(col);
  }
  for (const grp of PRICING_GROUPS) {
    const det = document.createElement("details");
    det.className = "p-group";
    det.dataset.group = grp.title;
    if (openSet.has(grp.title)) det.open = true;
    const sum = document.createElement("summary");
    sum.className = "p-group-sum";
    sum.appendChild(document.createTextNode(grp.title));
    det.appendChild(sum);
    const body = document.createElement("div");
    body.className = "p-group-body";
    for (const key of grp.keys) {
      if (key === "show_zero") continue;
      const cur = saved[key] !== undefined ? saved[key] : pricingDefaults[key];
      body.appendChild(pricingParamRow(key, cur));
    }
    det.appendChild(body);
    detailsNodes.push(det);
    cols[Math.min(grp.col || 2, 3) - 1].appendChild(det);
  }
  // Шапка панели: заголовок и крестик «✕» для сворачивания всех панелей.
  const head = document.createElement("div");
  head.className = "p-settings-ctrl";
  const cap = document.createElement("span");
  cap.className = "p-settings-cap";
  cap.textContent = "Настройки и коэффициенты";
  const closeBtn = document.createElement("button");
  closeBtn.type = "button";
  closeBtn.className = "p-settings-close";
  closeBtn.title = "Свернуть панели настроек";
  closeBtn.textContent = "✕";
  closeBtn.addEventListener("click", () => setPricingSettingsVisible(false));
  head.appendChild(cap);
  head.appendChild(closeBtn);
  box.appendChild(head);
  cols.forEach((col) => box.appendChild(col));
  detailsNodes.forEach((d) =>
    d.addEventListener("toggle", () => saveOpenPricingGroups(detailsNodes))
  );
}

const HIST_PAGE = 10;
let _histRows = null;
let _histShown = HIST_PAGE;

async function renderPricingHistory() {
  const box = $("#pricingHistory");
  let data;
  try {
    data = await api("/pricing/history?limit=200");
  } catch (err) {
    box.innerHTML = '<div class="empty">Не удалось загрузить журнал</div>';
    return;
  }
  _histRows = data.rows;
  _histShown = HIST_PAGE;
  if (!_histRows.length) {
    box.innerHTML = '<div class="empty">Журнал пуст — решения записываются здесь при применении скидок через WB API</div>';
    return;
  }
  renderHistorySlice();
}

function renderHistorySlice() {
  const box = $("#pricingHistory");
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
  const shown = Math.min(_histShown, _histRows.length);
  let html = table(headers, _histRows.slice(0, shown));
  const rest = _histRows.length - shown;
  if (rest > 0) {
    html += '<div style="margin-top:8px"><button id="historyMore" class="btn small">Ещё ' + Math.min(rest, HIST_PAGE) + ' · осталось ' + rest + '</button></div>';
  } else {
    html += '<div style="margin-top:8px;opacity:.65">Показаны все ' + shown + ' записей журнала</div>';
  }
  box.innerHTML = html;
  const btn = $("#historyMore");
  if (btn) btn.addEventListener("click", () => { _histShown += HIST_PAGE; renderHistorySlice(); });
}

function pricingWithDates(s) {
  const f = filters();
  if (f.date_from) s.date_from = f.date_from;
  if (f.date_to) s.date_to = f.date_to;
  return s;
}

let _pricingRows = [];
let _pricingResp = null;
let pricingModeCurrent = null;

function refreshApplyButton() {
  const btn = $("#pricingApply");
  if (!btn) return;
  const actives = _pricingRows.filter((r) => r.action === "RAISE" || r.action === "LOWER" || r.action === "HALVE");
  const canApply = actives.length > 0;
  btn.disabled = !canApply;
  btn.title = canApply
    ? "Применить через WB API только видимые в таблице строки (с учётом фильтров)"
    : "Нет видимых рекомендаций на изменение — применить больше нечего (только строки таблицы)";
}

// Какая агрегация нужна в строке «Итого» для каждой колонки: sum (сумма) или avg (среднее).
const PRICING_FOOT = {
  stock: "sum", stock_wb: "sum", buyouts: "sum", backlog: "sum",
  add_to_wishlist: "sum", cancel_sum: "sum",
  delta_discount: "avg", target_discount: "avg", current_discount: "avg", target_vis: "avg", current_vis: "avg",
  avg_price: "avg", net_cost: "avg", margin_pct: "avg", margin_per_one: "avg", product_rating: "avg",
  margin_pct_at_target: "avg", conv_buyout_percent: "avg", return_rate: "avg", conv_pct: "avg",
  revenue_per_one: "avg", income_per_one: "avg", commission_per_one: "avg",
  logistics_per_one: "avg", storage_per_one: "avg", doc: "avg", velocity: "avg", trend: "avg",
  promo_count: "sum", promo_part_pct: "avg", promo_tier_pct: "avg", promo_tier_boost: "avg", promo_cap_pct: "avg",
  promo_delta_discount: "avg", promo_score: "avg",
};
function pricingFooters(rows) {
  const accum = {};
  for (const k of Object.keys(PRICING_FOOT)) {
    let sum = 0, cnt = 0;
    for (const r of rows) {
      const n = Number(r[k]);
      if (r[k] != null && r[k] !== "" && isFinite(n)) { sum += n; cnt++; }
    }
    if (!cnt) continue;
    accum[k] = PRICING_FOOT[k] === "sum" ? sum : { avg: sum / cnt };
  }
  return accum;
}

const PRICES_STALE_HOURS = 12;

function pricingAgeText(hours) {
  if (hours < 1) return Math.max(1, Math.round(hours * 60)) + " мин";
  if (hours < 48) return Math.round(hours) + " ч";
  return Math.round(hours / 24) + " дн";
}

// Плашка актуальности снимка цен: расчёт идёт по базе (мгновенно), а не по
// живому WB API, поэтому показываем, как давно снимок обновляли.
function renderPricingPricesAge(data) {
  const el = $("#pricingPricesAge");
  if (!el) return;
  const stamp = data && data.prices_updated_at;
  if (!stamp) {
    el.textContent = data && data.prices_source === "wb_api"
      ? "цены WB: из API"
      : "";
    el.removeAttribute("data-stale");
    return;
  }
  const t = Date.parse(stamp);
  if (!isFinite(t)) { el.textContent = ""; el.removeAttribute("data-stale"); return; }
  const hours = (Date.now() - t) / 36e5;
  el.textContent = "цены WB: " + pricingAgeText(hours) + " назад";
  el.setAttribute("data-stale", hours > PRICES_STALE_HOURS ? "1" : "0");
}

async function refreshPricingPrices() {
  const msg = statusEl();
  const btn = $("#pricingPriceRefresh");
  const prev = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "Обновляю цены…"; }
  msg.textContent = "Загружаю цены и скидки WB…";
  try {
    const resp = await fetch("/api/wb/prices?excel=0", { method: "POST" });
    const j = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error((j.detail || resp.status) || "не удалось загрузить цены");
    msg.textContent = "Цены WB загружены: " + fmt(j.count || 0) + " позиций. Пересчитываю…";
    await renderPricing(false);
    msg.textContent = "Цены WB обновлены: " + fmt(j.count || 0) + " позиций";
  } catch (err) {
    msg.textContent = "Ошибка загрузки цен: " + err.message;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = prev; }
  }
}

async function renderPricing(apply) {
  await buildPricingSettings();
  const s = pricingWithDates(collectPricingSettings());
  const showZeroEl = $("#pricingShowZero");
  if (showZeroEl) s.show_zero = showZeroEl.checked;
  const msg = statusEl();
  const likeEl = $("#pricingLike") || { value: "" };
  const q = likeEl.value.trim().toLowerCase();
  const hideSkipEl = $("#pricingHideSkip");
  const hidePassive = hideSkipEl ? hideSkipEl.checked : false;
  buildPricingFilterBar();
  const filtersHost = $("#pricingFilters");
  if (filtersHost) filtersHost.classList.toggle("hidden", !pricingFiltersOn());
  const colFilters = pricingEffectiveColFilters();
  msg.textContent = "Считаю рекомендации…";
  try {
    const data = await apiPost("/pricing/recommendations", s);
    _pricingResp = data;
    renderPricingPricesAge(data);
    const allRows = data.rows || [];
    let pre = allRows;
    if (q) {
      pre = pre.filter((r) =>
        likeMatch(String(r.article || "") + " " + (r.name || ""), q)
      );
    }
    const hiddenPassive = hidePassive ? pre.filter((r) => r.action === "SKIP" || r.action === "HOLD").length : 0;
    const rows = pre.filter((r) =>
      (!hidePassive || (r.action !== "SKIP" && r.action !== "HOLD")) && pricingRowMatches(r, colFilters)
    );
    _pricingRows = rows;
    const actionable = rows.filter((r) => r.action === "RAISE" || r.action === "LOWER" || r.action === "HALVE").length;
    const underCooldown = rows.filter((r) => r.status === "skipped_cooldown").length;
    let summary = "Товаров: " + fmt(rows.length);
    if (allRows.length !== rows.length) summary += " из " + fmt(allRows.length);
    summary += ", решений: " + fmt(actionable);
    if (hiddenPassive > 0) summary += ", скрыто «держать»/«пропустить»: " + fmt(hiddenPassive);
    const hiddenDead = Number(data.hidden_dead || 0);
    if (hiddenDead > 0) summary += ", скрыто нулевых: " + fmt(hiddenDead) + " (вкл. «С нулевыми товарами»)";
    const nonWb = Number(data.non_wb || 0);
    if (nonWb > 0) summary += ", артикулов не из WB-карточек: " + fmt(nonWb) + " (исключены)";
    if (underCooldown) summary += ", в кулдауне: " + fmt(underCooldown);
    if (data.settings && data.settings.mode === "old") summary += " · старая модель (не рекомендуется)";
    if (data.date_from && data.date_to) summary += " · окно " + data.date_from + ".." + data.date_to;
    if (data.as_of) summary += " · на " + data.as_of;
    $("#pricingSummary").textContent = summary;
    msg.textContent = data.note || "";
    pagedTable($("#pricingTable"), colViewHeaders("pricing", pricingHeaders), rows, pricingFooters(rows), null, colViewPinKeys("pricing"));
    await renderPricingHistory();
    refreshApplyButton();
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

async function applyPricing() {
  const msg = statusEl();
  await buildPricingSettings();
  const s = pricingWithDates(collectPricingSettings());
  const showZeroEl = $("#pricingShowZero");
  if (showZeroEl) s.show_zero = showZeroEl.checked;
  // Отправляем ровно то, что видно в таблице: _pricingRows уже отфильтрован
  // по строке поиска, фильтрам колонок, «С нулевыми товарами» и «Скрыть держать/пропустить» — сервер НЕ пересчитывает.
  const toApply = _pricingRows.filter((r) => r.action === "RAISE" || r.action === "LOWER" || r.action === "HALVE");
  const lowers = toApply.filter((r) => r.action === "LOWER");
  const halve = toApply.filter((r) => r.action === "HALVE");
  const parts = [];
  if (lowers.length) {
    const list = lowers.slice(0, 5).map((r) => r.article).join(", ") + (lowers.length > 5 ? "…" : "");
    parts.push("снизить цену у " + lowers.length + " артикулов (" + list + ")");
  }
  if (halve.length) parts.push("разделить скидку пополам у " + halve.length + " мёртвых");
  const confirmText = parts.length
    ? "Применить к видимым в таблице товарам?\n• " + parts.join("\n• ")
    : "У видимых товаров нет рекомендаций на изменение — применить всё равно?";
  const oldMode = pricingModeCurrent === "old";
  const finalConfirm = (oldMode ? "ВНИМАНИЕ — включена старая модель расчёта (не рекомендуется)!\n\n" : "") + confirmText;
  if (!confirm(finalConfirm)) return;
  if (!toApply.length) {
    msg.textContent = "Нет видимых товаров для применения";
    return;
  }
  msg.textContent = "Отправляю скидки в WB API…";
  try {
    const body = Object.assign({}, s, { ui_rows: toApply });
    const resp = await fetch("/api/pricing/apply", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const j = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error((j.detail || resp.status) || "WB API не принял изменения");
    msg.textContent = j.note || ("Применено: " + (j.applied || []).length + " артикулов");
    await renderPricing(false);
  } catch (err) {
    msg.textContent = "Ошибка: " + err.message;
  }
}

// Календарь акций WB → wb_promotions, затем пересчёт рекомендаций.
async function refreshPricingPromos() {
  const msg = statusEl();
  const btn = $("#pricingPromoRefresh");
  const prev = btn ? btn.textContent : "";
  if (btn) { btn.disabled = true; btn.textContent = "Обновляю акции…"; }
  msg.textContent = "Загружаю календарь акций WB…";
  try {
    const resp = await fetch("/api/promo/refresh", { method: "POST" });
    const j = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error((j.detail || resp.status) || "не удалось обновить акции");
    msg.textContent = "Акции WB: " + fmt(j.count || 0) + " записей"
      + (j.window ? " (" + j.window + ")" : "") + " — пересчитываю…";
    await renderPricing(false);
    msg.textContent = "Акции WB обновлены: " + fmt(j.count || 0) + " записей";
  } catch (err) {
    msg.textContent = "Ошибка обновления акций: " + err.message;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = prev || "Обновить акции WB"; }
  }
}

async function exportPricing() {
  await buildPricingSettings();
  const s = pricingWithDates(collectPricingSettings());
  const showZeroEl = $("#pricingShowZero");
  if (showZeroEl) s.show_zero = showZeroEl.checked;
  const cp = colViewParam("pricing").replace(/^cols=/, "");
  if (cp) s.cols = cp;
  const msg = statusEl();
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

async function uploadMarginFunnelToDisk() {
  const msg = statusEl();
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
  const msg = statusEl();
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

async function uploadMarginOzonDetailToDisk() {
  const msg = statusEl();
  const url = $("#exportMarginOzonDetail") ? $("#exportMarginOzonDetail").dataset.url : "";
  if (!url || !msg) return;
  msg.textContent = "Формирую файл…";
  try {
    const resp = await fetch(url);
    if (!resp.ok) throw new Error(resp.status + " " + (await resp.text()));
    const blob = await resp.blob();
    const f = filters();
    const name = "margin_ozon_detail_" + (f.date_from || "na") + "_" + (f.date_to || "na") + "_" + yandexStamp() + ".xlsx";
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
  const msg = statusEl();
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

document.addEventListener("DOMContentLoaded", async () => {
  await loadColViews();
  initDates();
  initStatusWatch();
  initWriteDb();
  initOzBySize();
  initHelp();
  initYandexTab();
  updateCrumb("dashboard");

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
    btn.addEventListener("click", () => busyRun(() => apiDownload(api, kind, "#headerMsg")));
    const yd = document.createElement("button");
    yd.type = "button";
    yd.className = "btn";
    yd.textContent = "Загрузить на диск";
    yd.title = "Заливает свежий Excel-отчёт в папку /agent_market на Яндекс.Диске";
    yd.addEventListener("click", () => busyRun(() => apiUploadDisk(api, kind, "#headerMsg")));
    btn.parentNode.insertBefore(yd, btn.nextSibling);
    if (api !== "wb") return;
    const upd = document.createElement("button");
    upd.type = "button";
    upd.className = "btn";
    upd.textContent = "Обновить базу";
    upd.title = "Тянет данные из WB API и пишет в БД, файл не скачивается";
    upd.addEventListener("click", () => busyRun(() => apiDownload(api, kind, "#headerMsg", true)));
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
  onTabChange("ozDetailRaw", "oz-detail", () => {
    syncOzBySizeDisabled();
    loadTab("oz-detail");
  });
  const btnExportOzPlacement = document.getElementById("btnExportOzPlacement");
  if (btnExportOzPlacement) btnExportOzPlacement.addEventListener("click", () => busyRun(downloadOzPlacementExcel));
  const btnDiskOzPlacement = document.getElementById("btnDiskOzPlacement");
  if (btnDiskOzPlacement) btnDiskOzPlacement.addEventListener("click", () => busyRun(uploadOzPlacementToDisk));
  const btnOzPlacementPull = document.getElementById("btnOzPlacementPull");
  if (btnOzPlacementPull) btnOzPlacementPull.addEventListener("click", () => busyRun(() => {
    const spec = apiPullByTab["oz-placement"];
    if (!spec) return Promise.resolve();
    return apiDownload(spec[0], spec[1], spec[2], true);
  }));
  onTabChange("ozPlacementRaw", "oz-placement", () => {
    syncOzBySizeDisabled();
    loadTab("oz-placement");
  });
  onTabInput("ozPlacementLike", "oz-placement");
  const btnOzAccrualPull = document.getElementById("btnOzAccrualPull");
  if (btnOzAccrualPull) btnOzAccrualPull.addEventListener("click", () => busyRun(() => {
    const spec = apiPullByTab["oz-accrual"];
    if (!spec) return Promise.resolve();
    return apiDownload(spec[0], spec[1], spec[2], true);
  }));
  const btnExportOzAccrual = document.getElementById("btnExportOzAccrual");
  if (btnExportOzAccrual) btnExportOzAccrual.addEventListener("click", () => busyRun(downloadOzAccrualExcel));
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
    "wb-cards": ["wb", "cards", "#headerMsg"],
    "wb-stock": ["wb", "stock", "#headerMsg"],
    "wb-funnel": ["wb", "funnel", "#headerMsg"],
    "wb-sales": ["wb", "sales", "#headerMsg"],
    "wb-prices": ["wb", "prices", "#headerMsg"],
    "wb-storage": ["wb", "storage", "#headerMsg"],
    "wb-detail": ["wb", "detail", "#headerMsg"],
    "oz-cards": ["ozon", "cards", "#headerMsg"],
    "oz-stock": ["ozon", "stock", "#headerMsg"],
    "oz-prices": ["ozon", "prices", "#headerMsg"],
    "oz-realization": ["ozon", "realization", "#headerMsg"],
    "oz-detail": ["ozon", "detail", "#headerMsg"],
    "oz-placement": ["ozon", "placement", "#headerMsg"],
    "oz-accrual": ["ozon", "accrual", "#headerMsg"],
    "oz-cashflow": ["ozon", "cashflow", "#headerMsg"],
    "products": ["products", "refresh", "#headerMsg"],
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

  const dashMp = $("#dashMarketplace");
  if (dashMp) dashMp.addEventListener("change", () => loadTab("dashboard"));
  const dashCmp = $("#dashCompare");
  if (dashCmp) dashCmp.addEventListener("change", () => loadTab("dashboard"));
  ["dashFrom", "dashTo"].forEach((id) => {
    const el = $("#" + id);
    if (el) el.addEventListener("change", () => loadTab("dashboard"));
  });
  document.addEventListener("click", (ev) => {
    const title = ev.target.closest(".dash-sec-title");
    if (title) {
      const sec = title.closest("[data-csec]");
      if (sec) toggleDashSection(sec);
    }
  });
  ["chartMetric", "chartGran", "chartType"].forEach((id) => {
    const el = $("#" + id);
    if (el) el.addEventListener("change", repaintDashCharts);
  });
  ["chartADim", "chartAMetric"].forEach((id) => {
    const el = $("#" + id);
    if (el) el.addEventListener("change", repaintDashCharts);
  });
  initDashCollapse();
  initDashDrag();
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
  [["marginFunnelLike", "margin-funnel"], ["marginDetailLike", "margin-detail"], ["marginOzonDetailLike", "margin-ozon-detail"]]
    .forEach(([id, tab]) => onTabInput(id, tab));
  onTabChange("marginDetailCompare", "margin-detail");
  onTabChange("marginOzonDetailCompare", "margin-ozon-detail");
  ["replenishWindow", "replenishTarget", "replenishSort", "replenishShowInactive", "replenishView"]
    .forEach((id) => onTabChange(id, "replenish"));
  onTabInput("replenishLike", "replenish");
  onTabInput("wbFunnelLike", "wb-funnel");
  onTabInput("wbStockLike", "wb-stock");
  onTabChange("wbStockAgg", "wb-stock");
  onTabChange("wbPriceAgg", "wb-prices");
  [["wbPricesLike", "wb-prices"], ["wbStorageLike", "wb-storage"]]
    .forEach(([id, tab]) => onTabInput(id, tab));
  [["wbSalesLike", "wb-sales"], ["wbDetailLike", "wb-detail"]]
    .forEach(([id, tab]) => onTabInput(id, tab));
  onTabInput("ozDetailLike", "oz-detail");
  [["ozStockLike", "oz-stock"], ["ozPricesLike", "oz-prices"], ["ozRealLike", "oz-realization"]]
    .forEach(([id, tab]) => onTabInput(id, tab));
  onTabChange("ozStockAgg", "oz-stock");
  onTabChange("ozPriceAgg", "oz-prices");
  $("#oursImport").addEventListener("click", () => uploadFile("/import/custom-stock", $("#oursFile"), "#headerMsg", "ours"));
  onTabInput("productsLike", "products");
  onTabChange("productsSizes", "products");
  onTabChange("productsStocks", "products");
  const productsOverwriteEl = $("#productsOverwrite");
  if (productsOverwriteEl) {
    productsOverwriteEl.addEventListener("change", () => {
      const msg = statusEl();
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
  const productsApplyPriceBtn = $("#productsApplyPrice");
  if (productsApplyPriceBtn) productsApplyPriceBtn.addEventListener("click", () => busyRun(sendProductsPrices));
  const productsPriceClose = $("#productsPriceClose");
  if (productsPriceClose) {
    productsPriceClose.addEventListener("click", () => {
      const panel = $("#productsPricePanel");
      if (panel) panel.classList.add("hidden");
    });
  }
  // ── «Наш склад» — импорт/экспорт/диск ──
  $("#whCpImport").addEventListener("click", () => uploadFile("/warehouse/import/counterparties", $("#whCpFile"), "#headerMsg", "wh-cp"));
  $("#whRImport").addEventListener("click", () => uploadFile("/warehouse/import/docs?type=receipt", $("#whRFile"), "#headerMsg", "wh-receipt"));
  $("#whSImport").addEventListener("click", () => uploadFile("/warehouse/import/docs?type=shipment", $("#whSFile"), "#headerMsg", "wh-shipment"));
  wireWhDisk("#whCpToDisk", { kind: "counterparties" }, "#headerMsg", "wh-cp", false);
  wireWhDisk("#whCpFromDisk", { type: "counterparties" }, "#headerMsg", "wh-cp", true);
  wireWhDisk("#whRToDisk", { kind: "docs", type: "receipt" }, "#headerMsg", "wh-receipt", false);
  wireWhDisk("#whRFromDisk", { type: "receipt" }, "#headerMsg", "wh-receipt", true);
  wireWhDisk("#whSToDisk", { kind: "docs", type: "shipment" }, "#headerMsg", "wh-shipment", false);
  wireWhDisk("#whSFromDisk", { type: "shipment" }, "#headerMsg", "wh-shipment", true);
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
  onTabInput("whStockLike", "wh-stock");
  initCardsUpload();
  initDetailUpload();
  initProductsUpload();
  initNetCostUpload();
  const pricingExport = $("#pricingExport");
  if (pricingExport) pricingExport.addEventListener("click", () => exportPricing());
  const pricingApply = $("#pricingApply");
  if (pricingApply) pricingApply.addEventListener("click", () => applyPricing());
  const pricingPromoRefresh = $("#pricingPromoRefresh");
  if (pricingPromoRefresh) pricingPromoRefresh.addEventListener("click", () => refreshPricingPromos());
  const pricingPriceRefresh = $("#pricingPriceRefresh");
  if (pricingPriceRefresh) pricingPriceRefresh.addEventListener("click", () => refreshPricingPrices());
  onTabInput("pricingLike", "pricing", () => renderPricing(false), 350);
  initPricingShowMenu();
  const pricingFiltersBtn = $("#pricingFiltersBtn");
  if (pricingFiltersBtn) {
    pricingFiltersBtn.classList.toggle("active", pricingFiltersOn());
    pricingFiltersBtn.addEventListener("click", () => {
      setPricingFiltersOn(!pricingFiltersOn());
      pricingFiltersBtn.classList.toggle("active", pricingFiltersOn());
      buildPricingFilterBar();
      const fh = $("#pricingFilters");
      if (fh) fh.classList.toggle("hidden", !pricingFiltersOn());
    });
  }
  initPricingSettingsMenu();
  const pricingSettingsBox = $("#pricingSettings");
  if (pricingSettingsBox) {
    // Автосохранение: любое изменение поля в форме сразу пишется в localStorage,
    // чтобы значения не терялись. Пересчёт — только «Обновить» в меню «Настройки».
    const autosave = debounceRun(() => {
      collectPricingSettings();
      const m = statusEl();
      if (m) m.textContent = "Настройки сохранены — примените их кнопкой «Обновить» в меню «Настройки»";
    }, 400);
    pricingSettingsBox.addEventListener("change", (ev) => {
      if (!ev.target.matches || !ev.target.matches("input[data-key]")) return;
      autosave();
    });
  }
  syncHeaderForTab(currentTab);
  for (const t of Object.keys(_COLVIEWS)) initColViewMenu(t);
  initReplenishPdfMenu();
  const btnExportMarginDetail = $("#exportMarginDetail");
  if (btnExportMarginDetail) {
    btnExportMarginDetail.addEventListener("click", () => {
      const url = btnExportMarginDetail.dataset.url;
      if (url) window.open(url, "_blank");
    });
  }
  const btnExportMarginOzonDetail = $("#exportMarginOzonDetail");
  if (btnExportMarginOzonDetail) {
    btnExportMarginOzonDetail.addEventListener("click", () => {
      const url = btnExportMarginOzonDetail.dataset.url;
      if (url) window.open(url, "_blank");
    });
  }
  const uploadMarginFunnel = $("#uploadMarginFunnel");
  if (uploadMarginFunnel) uploadMarginFunnel.addEventListener("click", () => uploadMarginFunnelToDisk());
  const uploadMarginDetail = $("#uploadMarginDetail");
  if (uploadMarginDetail) uploadMarginDetail.addEventListener("click", () => uploadMarginDetailToDisk());
  const uploadMarginOzonDetail = $("#uploadMarginOzonDetail");
  if (uploadMarginOzonDetail) uploadMarginOzonDetail.addEventListener("click", () => uploadMarginOzonDetailToDisk());
  const pricingUpload = $("#pricingUpload");
  if (pricingUpload) pricingUpload.addEventListener("click", () => uploadPricingToDisk());
  initTicketsTab();
  initHeaderTip();
  loadTab(currentTab);
});
