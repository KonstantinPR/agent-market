"""Дашборд общей информации: бизнес-агрегаты из детализаций продаж WB/Ozon.

Верхнеуровневые показатели (KPI, топы по прибыли, дельты цены, рентабельность
по группам-префиксам, сводка остатков и свежесть данных) строятся на данных
маржинальности по детализациям (margin.py). Ничего не пишет в БД.
"""

import logging
from datetime import date
from typing import Optional

import pandas as pd
from sqlalchemy import desc, func, select

from app import models
from app.config import settings
from app.services import margin as margin_service
from app.services import xyz as xyz_service

log = logging.getLogger(__name__)

# Эталонная карта "префикс серии -> группа товаров" (по решению владельца).
# Матчинг — по началу артикула, самый длинный ключ побеждает (SOHO-FRNT
# раньше SOHO, JZ2 раньше JZ/J) — дефисные ключи ловятся у сужений вида
# SOHO-FRNT-2S. Промах — группа «остальные».
PREFIXES_ART_DICT = {
    "SHK": "SH",
    "SH": "SH",
    "SK": "SK",
    "SN": "SK",
    "SF": "SF",
    "J": "J",
    "JZ2": "JZ",
    "JZ": "JZ",
    "MIT": "MIT",
    "AN": "MIT",
    "MK": "MIT",
    "IBZ": "IBZ",
    "TIE": "TIE",
    "FATA": "F",
    "V": "F",
    "GR": "GR",
    "LQ3": "LQ",
    "LQ": "LQ",
    "SOHO-FRNT": "SOHO-FRNT",
    "SOHOCOAT": "SOHO",
    "SOHO": "SOHO",
    "TG": "TG",
    "SEL": "STL",
    "KP": "KR",
    "MHSORT": "MHSORT",
    "MHSL": "MHS",
    "MHSE": "MHS",
    "MHSB": "MHS",
    "MHBB": "MHS",
    "MHS": "MHS",
    "BOL": "BIJ",
    "OM": "BIJ",
    "Y00": "Y00",
    "PRS": "PRS",
    "IANCO": "IANCO",
}

_PREFIX_KEYS = tuple(sorted(PREFIXES_ART_DICT, key=len, reverse=True))

OTHER = "остальные"

# Способы группировки товаров для таблицы «Прибыльность по группам» и
# графика «Показатели». Ключ уходит в API параметром group_by.
GROUPINGS = {
    "prefix": "Группы артикулов",
    "marketplace": "Маркетплейс",
    "supplier": "Поставщик",
    "price": "Ценовой сегмент",
    "margin": "Рентабельность",
    "abc": "ABC по выручке",
    "xyz": "XYZ по стабильности спроса",
}

MP_GROUP_LABELS = {"wb": "WB", "ozon": "Ozon"}


def _price_segment(price: float) -> str:
    if price <= 0:
        return "нет данных"
    if price < 1000:
        return "до 1 000 ₽"
    if price < 2000:
        return "1 000–2 000 ₽"
    if price < 5000:
        return "2 000–5 000 ₽"
    return "5 000 ₽ и выше"


def _margin_segment(pct: float) -> str:
    if pct < 0:
        return "убыточные"
    if pct < 10:
        return "0–10 %"
    if pct < 25:
        return "10–25 %"
    return "25 % и выше"


def _abc_labels(df: pd.DataFrame) -> pd.Series:
    """ABC по выручке: A — первые 80 % выручки, B — до 95 %, C — остальное.

    Артикулы без выручки (только расходные строки) в ранжировании не участвуют
    и сразу попадают в «C (нет выручки)» — иначе они размывали бы долю C.
    Само разбиение считает ``xyz_service.abc_ranks`` — тем же кодом, что и
    матрица ABC×XYZ: иначе группы в таблице и в матрице разъезжаются.
    """
    labels = pd.Series("C (нет выручки)", index=df.index, dtype=object)
    if df is None or df.empty:
        return labels
    ranks = xyz_service.abc_ranks(df)
    has_rev = pd.to_numeric(df["revenue"], errors="coerce").fillna(0) > 0
    labels[has_rev & (ranks == "A")] = "A (до 80 % выручки)"
    labels[has_rev & (ranks == "B")] = "B (80–95 %)"
    labels[has_rev & (ranks == "C")] = "C (остальное)"
    return labels


def _supplier_map(db) -> dict:
    """Артикул (UPPER) -> поставщик из каталога; пусто — «Без поставщика»."""
    rows = db.execute(select(models.Product.article, models.Product.supplier))
    return {
        (a or "").strip().upper(): (s or "").strip() or "Без поставщика"
        for a, s in rows
    }


def _xyz_map(db, date_from, date_to) -> dict:
    """``{article(UPPER): xyz_class}`` для группировки group_by='xyz' (WB)."""
    try:
        res = xyz_service.xyz_rows(db, date_from, date_to)
    except Exception:  # noqa: BLE001 — группировка не должна ронять дашборд
        log.warning("XYZ: не удалось построить карту классов", exc_info=True)
        return {}
    return {str(r["article"]).strip().upper(): r["xyz_class"] for r in res["rows"]}


def _article_groups(cur: pd.DataFrame, db, group_by: str,
                    xyz_map: Optional[dict] = None) -> pd.Series:
    """Подпись группы для каждой строки артикула (cur из _by_article).

    ``xyz_map`` — ``{article(UPPER): xyz_class}`` для group_by='xyz'.
    """
    if group_by == "supplier":
        smap = _supplier_map(db)
        return cur["article"].map(
            lambda a: smap.get(str(a).strip().upper(), "Без поставщика"))
    if group_by == "price":
        return cur.apply(
            lambda r: _price_segment(
                float(r["revenue"] or 0) / r["sells"] if r["sells"] else 0.0),
            axis=1)
    if group_by == "margin":
        return cur["margin_pct"].map(
            lambda v: _margin_segment(float(v or 0)))
    if group_by == "abc":
        return _abc_labels(cur)
    if group_by == "xyz":
        xmap = xyz_map or {}
        return cur["article"].map(
            lambda a: xyz_service.XYZ_LABELS.get(
                xmap.get(str(a).strip().upper()),
                xyz_service.NO_DATA_LABEL))
    return cur["article"].map(prefix_group)

# Единый набор колонок сводных строк дашборда (есть в обеих детализациях).
# margin_gross («Маржа до себестоимости») нужен графику «Показатели» дашборда.
_KPI_COLS = [
    "article", "marketplace", "name", "sells", "returns_qty", "revenue",
    "income", "margin_gross", "margin", "margin_per_one", "margin_pct",
    "net_cost_est",
    # Ozon: строка — товар (базовый артикул), размеры свёрнуты
    "size", "sizes_count", "offers_count",
]


def prefix_group(article) -> str:
    """Группа товара по PREFIXES_ART_DICT (самый длинный ключ у начала артикула)."""
    if article is None:
        return OTHER
    s = str(article).strip().upper()
    if not s:
        return OTHER
    for key in _PREFIX_KEYS:
        if s.startswith(key):
            return PREFIXES_ART_DICT[key]
    return OTHER


def _wb_detail(db, date_from=None, date_to=None) -> pd.DataFrame:
    if date_from is None and date_to is None:
        return pd.DataFrame()
    return margin_service.margin_detail_dataframe(
        db, date_from=date_from, date_to=date_to, article_like=None,
        default_net_cost=settings.default_net_cost,
    )


def _oz_detail(db, date_from=None, date_to=None) -> pd.DataFrame:
    if date_from is None and date_to is None:
        return pd.DataFrame()
    return margin_service.ozon_margin_detail_dataframe(
        db, date_from=date_from, date_to=date_to, article_like=None,
        default_net_cost=settings.default_net_cost,
    )


def _wanted(marketplace) -> list:
    if not marketplace or str(marketplace).strip().lower() in ("", "all"):
        return ["wb", "ozon"]
    return [c.lower().strip() for c in str(marketplace).split(",") if c.strip()]


def combined_detail(db, date_from=None, date_to=None, marketplace=None) -> pd.DataFrame:
    """Сводка по детализациям выбранных МП (одна строка — артикул + МП)."""
    frames = []
    for code in _wanted(marketplace):
        df = (_wb_detail(db, date_from, date_to) if code == "wb"
              else _oz_detail(db, date_from, date_to) if code == "ozon"
              else pd.DataFrame())
        if df is None or df.empty:
            continue
        df = df.copy()
        df["marketplace"] = code
        frames.append(df)
    if not frames:
        return pd.DataFrame(columns=_KPI_COLS)
    union = pd.concat(frames, ignore_index=True, sort=False)
    for c in ("returns_qty", "net_cost_est", "sizes_count", "offers_count"):
        if c not in union.columns:
            union[c] = 0
    for c in ("size",):
        if c not in union.columns:
            union[c] = ""
        else:
            union[c] = union[c].fillna("")
    return union.loc[:, [c for c in _KPI_COLS if c in union.columns]]


def _kpi_row(df: pd.DataFrame, label: str = "") -> dict:
    if df is None or df.empty:
        row = dict.fromkeys(
            ("sells", "returns_qty", "revenue", "income", "margin_gross",
             "margin", "margin_per_one", "margin_pct", "margin_roi_pct",
             "articles", "net_cost_est"), 0)
        row["marketplace"] = label
        return row
    sells = int(df["sells"].sum())
    income = float(df["income"].sum() or 0)
    margin = float(df["margin"].sum() or 0)
    revenue = float(df["revenue"].sum() or 0)
    return {
        "marketplace": label,
        "sells": sells,
        "returns_qty": int(df["returns_qty"].sum()),
        "revenue": round(revenue, 2),
        "income": round(income, 2),
        "margin_gross": round(float(df["margin_gross"].sum() or 0), 2)
        if "margin_gross" in df else 0.0,
        "margin": round(margin, 2),
        "margin_per_one": round(margin / sells, 2) if sells else 0.0,
        # Рентабельность продаж (ROS): прибыль к выручке.
        "margin_pct": round(margin / revenue * 100, 2) if revenue else 0.0,
        # Рентабельность затрат (ROI): прибыль к полным затратам (выручка − прибыль).
        "margin_roi_pct": round(margin / (revenue - margin) * 100, 2)
        if (revenue - margin) > 0 else 0.0,
        "articles": int(df["article"].nunique()) if "article" in df else 0,
        "net_cost_est": int(df["net_cost_est"].sum()) if "net_cost_est" in df else 0,
    }


def dashboard_kpis(db, date_from, date_to, prev=None, marketplace=None) -> dict:
    """KPI по детализациям: всего/по МП + сравнение с предыдущим периодом.

    prev — (date_from, date_to) предыдущего аналогичного окна или None.
    """
    total_df = combined_detail(db, date_from, date_to, marketplace)
    total = _kpi_row(total_df)
    per_mp = []
    for code in ("wb", "ozon"):
        if code not in _wanted(marketplace):
            continue
        sub = total_df[total_df["marketplace"] == code]
        per_mp.append(_kpi_row(sub, code))

    compare = None
    if prev is not None and prev[0] is not None and prev[1] is not None:
        prev_df = combined_detail(db, prev[0], prev[1], marketplace)
        prev_total = _kpi_row(prev_df)
        delta_ru = round(total["margin"] - prev_total["margin"], 2)
        compare = {
            "prev": prev_total,
            "delta_ru": delta_ru,
            "delta_pct": round(delta_ru / abs(prev_total["margin"]) * 100, 2)
            if prev_total["margin"] else None,
        }
    return {"total": total, "per_mp": per_mp, "compare": compare}


def _by_article(df: pd.DataFrame) -> pd.DataFrame:
    """Группирует сводку по артикулу (убирает кросс-МП дубли), сортир. по марже."""
    if df is None or df.empty:
        return pd.DataFrame()
    g = df.groupby("article", as_index=False).agg(
        name=("name", lambda s: next((x for x in s if str(x).strip()), "")),
        marketplace=("marketplace", lambda s: ",".join(sorted(set(
            str(x) for x in s if str(x).strip())))),
        sells=("sells", "sum"),
        returns_qty=("returns_qty", "sum"),
        revenue=("revenue", "sum"),
        income=("income", "sum"),
        margin_gross=("margin_gross", "sum"),
        margin=("margin", "sum"),
        net_cost_est=("net_cost_est", "sum"),
        sizes_count=("sizes_count", "max"),
        offers_count=("offers_count", "max"),
    )
    g["margin_per_one"] = g.apply(
        lambda r: round(r["margin"] / r["sells"], 2) if r["sells"] else 0.0, axis=1)
    g["margin_pct"] = g.apply(
        lambda r: round(r["margin"] / r["revenue"] * 100, 2) if r["revenue"] else 0.0,
        axis=1)
    g["margin_roi_pct"] = g.apply(
        lambda r: round(r["margin"] / (r["revenue"] - r["margin"]) * 100, 2)
        if (r["revenue"] - r["margin"]) > 0 else 0.0, axis=1)
    g = g.sort_values("margin", ascending=False).reset_index(drop=True)
    return g


def top_products(db, date_from, date_to, marketplace=None, limit=None, offset=0,
                 kind="profit") -> dict:
    """Топ прибыльных (kind='profit') или убыточных (kind='loss') артикулов.

    limit=None — весь список (клиенту для сортировки/пагинации/итогов целиком).
    """
    cur = _by_article(combined_detail(db, date_from, date_to, marketplace))
    if cur.empty:
        return {"rows": [], "count": 0}
    cur = cur.sort_values(
        "margin", ascending=(kind == "loss")).reset_index(drop=True)
    rows = cur.iloc[offset:(None if limit is None else offset + limit)]
    out = rows[[
        "article", "name", "marketplace", "sells", "returns_qty",
        "revenue", "income", "margin", "margin_per_one", "margin_pct",
        "margin_roi_pct",
    ]].to_dict("records")
    for rec, (_, src) in zip(out, rows.iterrows()):
        # колонки группировки есть только у Ozon — отдаём 0 для WB
        for c in ("sizes_count", "offers_count"):
            v = src.get(c)
            rec[c] = int(v) if v == v and not pd.isna(v) else 0
    return {
        "rows": out,
        "count": int(len(cur)),
    }


def grouped_margin(db, date_from, date_to, marketplace=None, limit=None, offset=0,
                   group_by="prefix") -> dict:
    """Рентабельность по группам товаров (limit=None — весь список).

    group_by: prefix | marketplace | supplier | price | margin | abc.
    Строка группы всегда отдаётся в ключе `prefix` (совместимость с таблицей
    и Excel-выгрузкой), рядом лежит `sample` — примеры артикулов группы.
    """
    if group_by not in GROUPINGS:
        group_by = "prefix"
    detail = combined_detail(db, date_from, date_to, marketplace)
    if detail.empty:
        return {"rows": [], "count": 0, "group_by": group_by}

    if group_by == "marketplace":
        rows = []
        for code, sub in detail.groupby("marketplace"):
            row = _kpi_row(sub, str(code))
            row["prefix"] = MP_GROUP_LABELS.get(str(code), str(code).upper())
            row["sample"] = ", ".join(
                sub.sort_values("revenue", ascending=False)["article"]
                .head(5).astype(str))
            rows.append(row)
        g = pd.DataFrame(rows)
        if not g.empty:
            g = g.sort_values("margin", ascending=False).reset_index(drop=True)
        cols = ["prefix", "sample", "articles", "sells", "returns_qty",
                "revenue", "income", "margin_gross", "margin",
                "margin_per_one", "margin_pct", "margin_roi_pct"]
        g = g.loc[:, [c for c in cols if c in g.columns]]
    else:
        cur = _by_article(detail).copy()
        xyz_map = None
        if group_by == "xyz":
            xyz_map = _xyz_map(db, date_from, date_to)
        cur["prefix"] = _article_groups(cur, db, group_by, xyz_map)
        samples = (cur.sort_values("revenue", ascending=False)
                   .groupby("prefix")["article"]
                   .apply(lambda s: ", ".join(s.head(5).astype(str))))
        g = cur.groupby("prefix", as_index=False).agg(
            articles=("article", "count"),
            sells=("sells", "sum"),
            returns_qty=("returns_qty", "sum"),
            revenue=("revenue", "sum"),
            income=("income", "sum"),
            margin_gross=("margin_gross", "sum"),
            margin=("margin", "sum"),
        )
        g["sample"] = g["prefix"].map(samples)
        g["margin_per_one"] = g.apply(
            lambda r: round(r["margin"] / r["sells"], 2) if r["sells"] else 0.0,
            axis=1)
        g["margin_pct"] = g.apply(
            lambda r: round(r["margin"] / r["revenue"] * 100, 2)
            if r["revenue"] else 0.0, axis=1)
        g["margin_roi_pct"] = g.apply(
            lambda r: round(r["margin"] / (r["revenue"] - r["margin"]) * 100, 2)
            if (r["revenue"] - r["margin"]) > 0 else 0.0, axis=1)
        g = g.sort_values("margin", ascending=False).reset_index(drop=True)

    rows = g.iloc[offset:(None if limit is None else offset + limit)]
    return {
        "rows": rows.to_dict("records"),
        "count": int(len(g)),
        "group_by": group_by,
    }


def prefix_margin(db, date_from, date_to, marketplace=None, limit=None, offset=0) -> dict:
    """Рентабельность по группам-префиксам артикулов (совместимая обёртка)."""
    return grouped_margin(db, date_from, date_to, marketplace, limit, offset,
                          group_by="prefix")


# ------------------------------------------------------------------ дельты цены
def _wb_price_by_article(db, date_from=None, date_to=None) -> dict:
    q = select(
        models.WbDetailRow.article,
        func.sum(models.WbDetailRow.quantity),
        func.sum(models.WbDetailRow.retail_amount),
    ).where(
        models.WbDetailRow.article != "",
        models.WbDetailRow.quantity > 0,
        models.WbDetailRow.retail_amount != 0,
    )
    if date_from:
        q = q.where(models.WbDetailRow.sale_dt >= date_from)
    if date_to:
        q = q.where(models.WbDetailRow.sale_dt <= date_to)
    q = q.group_by(models.WbDetailRow.article)
    out = {}
    for art, qty, rev in db.execute(q):
        if not art or qty in (None, 0):
            continue
        out[art] = {"qty": int(qty), "rev": float(rev or 0)}
    return out


def _oz_price_by_article(db, date_from=None, date_to=None) -> dict:
    """Ozon: средняя цена по товарам (базовый артикул), а не по размерам.

    Ключ должен совпадать с Ozon-частью combined_detail(), иначе в price_delta
    теряются наименование и маржа строки.
    """
    base = func.coalesce(
        func.nullif(models.OzonDetailRow.base_article, ""),
        models.OzonDetailRow.offer_id)
    q = select(
        base.label("base"),
        func.sum(models.OzonDetailRow.quantity),
        func.sum(models.OzonDetailRow.seller_price * models.OzonDetailRow.quantity),
    ).where(
        models.OzonDetailRow.offer_id != "",
        models.OzonDetailRow.quantity > 0,
        models.OzonDetailRow.seller_price != 0,
    )
    if date_from:
        q = q.where(models.OzonDetailRow.date >= date_from)
    if date_to:
        q = q.where(models.OzonDetailRow.date <= date_to)
    q = q.group_by(base)
    out = {}
    for art, qty, rev in db.execute(q):
        if not art or qty in (None, 0):
            continue
        out[art] = {"qty": int(qty), "rev": float(rev or 0)}
    return out


def _merge_price(*by_mp) -> dict:
    out: dict = {}
    for m in by_mp:
        for art, v in m.items():
            if art not in out:
                out[art] = {"qty": 0, "rev": 0.0}
            out[art]["qty"] += v["qty"]
            out[art]["rev"] += v["rev"]
    return out


def price_delta(db, date_from, date_to, prev=None, marketplace=None, limit=None) -> dict:
    """Средняя цена продажи за окно против предыдущего аналогичного окна.

    Сравниваются артикулы, продававшиеся в обоих окнах. Возвращает два топа:
    up — самые подорожавшие (Δ% по убыв.), down — самые подешевевшие.
    limit=None — полные списки (клиенту — сортировка/пагинация/итоги).
    """
    wanted = _wanted(marketplace)
    cur = _merge_price(
        *[(_wb_price_by_article(db, date_from, date_to) if code == "wb"
           else _oz_price_by_article(db, date_from, date_to)) for code in wanted])
    if not prev or prev[0] is None or prev[1] is None:
        return {"up": {"rows": [], "count": 0}, "down": {"rows": [], "count": 0}}
    old = _merge_price(
        *[(_wb_price_by_article(db, prev[0], prev[1]) if code == "wb"
           else _oz_price_by_article(db, prev[0], prev[1])) for code in wanted])

    detail = _by_article(combined_detail(db, date_from, date_to, marketplace))
    info = {}
    if not detail.empty:
        info = {
            r["article"]: {"name": r["name"], "margin": r["margin"],
                           "sells": int(r["sells"])}
            for r in detail.to_dict("records")
        }

    rows = []
    for art, v in cur.items():
        ref = old.get(art)
        if not ref or ref["qty"] == 0 or not v or v["qty"] == 0:
            continue
        avg_cur = v["rev"] / v["qty"]
        avg_prev = ref["rev"] / ref["qty"]
        if avg_prev == 0:
            continue
        rows.append({
            "article": art,
            "name": (info.get(art) or {}).get("name", ""),
            "avg": round(avg_cur, 2),
            "avg_prev": round(avg_prev, 2),
            "delta_ru": round(avg_cur - avg_prev, 2),
            "delta_pct": round((avg_cur - avg_prev) / avg_prev * 100, 2),
            "margin": round((info.get(art) or {}).get("margin", 0.0), 2) or None,
            "sells": info.get(art, {}).get("sells", 0),
        })
    rows.sort(key=lambda r: r["delta_pct"], reverse=True)
    up = rows if limit is None else rows[:limit]
    down = sorted(rows, key=lambda r: r["delta_pct"])
    if limit is not None:
        down = down[:limit]
    return {
        "up": {"rows": up, "count": len(up)},
        "down": {"rows": down, "count": len(down)},
    }


# ------------------------------------------------------------------ склад и свежесть
def _stock_snapshot(db, mp_code, date_to) -> pd.DataFrame:
    """Остатки МП на последней дате среза <= date_to, сумму по артикулам."""
    mp_id = db.execute(
        select(models.Marketplace.id).where(models.Marketplace.code == mp_code)
    ).scalar_one_or_none()
    if mp_id is None:
        return pd.DataFrame()
    snap = db.execute(
        select(func.max(models.Stock.date)).where(
            models.Stock.marketplace_id == mp_id, models.Stock.date <= date_to)
    ).scalar_one_or_none()
    if snap is None:
        return pd.DataFrame()
    rows = db.execute(select(models.Stock).where(
        models.Stock.marketplace_id == mp_id, models.Stock.date == snap,
        models.Stock.article != "",
    )).scalars().all()
    # Ozon: остатки по товару (базовый артикул), WB — по полному артикулу
    is_ozon = mp_code == "ozon"
    aggr: dict = {}
    members: dict = {}
    for s in rows:
        art = ((s.base_article or "").strip() or s.article) if is_ozon else s.article
        if art not in aggr:
            aggr[art] = [0, 0, 0]
        aggr[art][0] += int(s.quantity or 0)
        aggr[art][1] += int(s.quantity_full or 0)
        aggr[art][2] += int(s.in_way or 0)
        if is_ozon and s.article and s.article != art:
            members.setdefault(art, set()).add(s.article)
    df = pd.DataFrame([
        {"article": art, "quantity": q, "quantity_full": qf, "in_way": iw}
        for art, (q, qf, iw) in aggr.items()
    ])
    df.attrs["members"] = members
    if not df.empty:
        df["stock_date"] = snap
    return df


def _sold_by_mp(db, date_from, date_to) -> dict:
    """Проданное количество (нетто) по маркетплейсам за окно — для запаса в днях."""
    if date_from is None or date_to is None:
        return {}
    q = (
        select(models.Marketplace.code, func.sum(models.Sale.quantity))
        .select_from(models.Sale)
        .join(models.Marketplace,
              models.Sale.marketplace_id == models.Marketplace.id)
        .where(models.Sale.date >= date_from, models.Sale.date <= date_to,
               models.Sale.source != "detail")
        .group_by(models.Marketplace.code)
    )
    return {code: int(qty or 0) for code, qty in db.execute(q)}


def stocks_summary(db, date_to, marketplace=None, date_from=None) -> dict:
    """Остатки на конец окна: шт/в пути/оценка в деньгах по себестоимости."""
    if date_to is None:
        date_to = date.today()
    sold = _sold_by_mp(db, date_from, date_to)
    days = ((date_to - date_from).days + 1) if date_from else 0
    out = {}
    for code in _wanted(marketplace):
        snap = _stock_snapshot(db, code, date_to)
        if snap is None or snap.empty:
            out[code] = {
                "quantity": 0, "quantity_full": 0, "in_way": 0,
                "value": 0.0, "value_unknown": 0, "articles": 0,
                "empty_articles": 0, "avg_cost": 0.0, "in_way_value": 0.0,
                "sold": int(sold.get(code, 0)), "coverage_days": None,
                "snapshot_age_days": None, "date": None,
            }
            continue
        articles = list(snap["article"])
        members = snap.attrs.get("members") or {}
        # себестоимость ищем по базовому артикулу, а если его нет — по любому
        # артикулу размера в группе (у Ozon база часто не заведена в products)
        wanted = {a.upper() for a in articles} | {
            m.upper() for ms in members.values() for m in ms}
        products = db.execute(select(models.Product).where(
            models.Product.article.in_(wanted)
        )).scalars().all()
        cost = {p.article.upper(): float(p.net_cost or 0) for p in products}
        est = 0
        value = 0.0
        for _, r in snap.iterrows():
            key = str(r["article"]).upper()
            c = cost.get(key, 0.0)
            if c <= 0:
                c = next((cost[m.upper()] for m in sorted(members.get(r["article"], ()))
                          if cost.get(m.upper(), 0.0) > 0), 0.0)
            if c > 0:
                value += c * int(r["quantity_full"])
            else:
                est += 1
        quantity_full = int(snap["quantity_full"].sum())
        in_way = int(snap["in_way"].sum())
        avg_cost = round(value / quantity_full, 2) if quantity_full else 0.0
        snap_date = snap["stock_date"].iloc[0] if not snap.empty else None
        sold_qty = int(sold.get(code, 0))
        out[code] = {
            "quantity": int(snap["quantity"].sum()),
            "quantity_full": quantity_full,
            "in_way": in_way,
            "value": round(value, 2),
            "value_unknown": est,
            "articles": int(len(snap)),
            "empty_articles": int((snap["quantity_full"] <= 0).sum()),
            "avg_cost": avg_cost,
            "in_way_value": round(avg_cost * in_way, 2),
            "sold": sold_qty,
            "coverage_days": (round(quantity_full / (sold_qty / days), 1)
                              if sold_qty > 0 and days > 0 else None),
            "snapshot_age_days": ((date_to - snap_date).days
                                  if snap_date is not None else None),
            "date": str(snap_date) if snap_date is not None else None,
        }
    return out


def freshness(db, limit=8) -> list:
    rows = db.execute(
        select(models.ApiPull).order_by(desc(models.ApiPull.last_success_at)).limit(limit)
    ).scalars().all()
    return [{
        "api": p.api,
        "kind": p.kind,
        "last_success_at": p.last_success_at.isoformat(sep=" ") if p.last_success_at else None,
        "rows": p.rows,
        "db_rows": p.db_rows,
        "window": p.window,
    } for p in rows]