# -*- coding: utf-8 -*-
"""ABC/XYZ-анализ ассортимента для раздела «Обзор».

- **ABC** — вклад товара в выручку (A — первые 80 %, B — до 95 %, C — остальное).
  Уже реализован в ``dashboard._abc_labels`` и переиспользуется здесь.
- **XYZ** — стабильность спроса во времени: коэффициент вариации (CV = σ/μ)
  помесячных продаж за окно (по умолчанию 12 месяцев).

Данные берутся из ``models.Sale`` (WB, ``quantity > 0``) — единственного
источника с дневной датой. X/Y/Z — ранговые (терцильные) пороги по CV среди
товаров с достаточной историей; у кого активных месяцев меньше порога —
класс «Новинка (мало истории)». Абсолютные пороги CV (0.5/1.0) на этом
ассортименте не работают: почти всё попадёт в Z.

Ничего не пишет в БД. Чистые функции вынесены отдельно — их удобно тестировать.
"""

from __future__ import annotations

import logging
from datetime import date

import pandas as pd
from sqlalchemy import func, select

from app import models
from app.config import settings

log = logging.getLogger(__name__)

#: Коды классов (стабильные ключи для фронта/экспорта).
XLVL, YLVL, ZLVL, NEWLVL = "X", "Y", "Z", "NEW"

XYZ_LABELS = {
    XLVL: "X (стабильный спрос)",
    YLVL: "Y (умеренные колебания)",
    ZLVL: "Z (скачки спроса)",
    NEWLVL: "Новинка (мало истории)",
}

ABC_LABELS = {
    "A": "A (до 80 % выручки)",
    "B": "B (80–95 %)",
    "C": "C (остальное)",
}

#: Группа для артикулов, у которых в окне нет продаж (в карте классов их нет):
#: не «Новинка» (мало истории), а именно отсутствие данных о спросе.
NO_DATA_LABEL = "Нет данных о спросе"

#: Строки матрицы ABC × XYZ (доли выручки) и столбцы (классы спроса).
ABC_ROWS = ("A", "B", "C")
XYZ_COLS = (XLVL, YLVL, ZLVL, NEWLVL)


def _month_key(value) -> str:
    return str(value)[:7]


def _month_range(date_from, date_to) -> list[str]:
    """Список месяцев YYYY-MM от ``date_from`` до ``date_to`` включительно."""
    if not date_from or not date_to:
        return []
    start = pd.Timestamp(date_from)
    end = pd.Timestamp(date_to)
    return [str(p)[:7] for p in pd.period_range(start, end, freq="M")]


def _window(date_to, months: int) -> tuple[date, date]:
    """Окно: ``months`` календарных месяцев, заканчивая ``date_to``."""
    end = pd.Timestamp(date_to)
    start = (end.to_period("M") - (months - 1)).start_time
    return start.date(), end.date()


def xyz_series(db, date_from, date_to, marketplace: str = "wb") -> pd.DataFrame:
    """Помесячные продажи: колонки ``article, month, qty`` (только qty > 0)."""
    mp = (marketplace or "wb").strip().lower()
    if not date_from or not date_to:
        return pd.DataFrame(columns=["article", "month", "qty"])
    month = func.to_char(models.Sale.date, "YYYY-MM").label("month")
    q = (
        select(models.Sale.article.label("article"), month,
               func.sum(models.Sale.quantity).label("qty"))
        .select_from(models.Sale)
        .join(models.Marketplace, models.Sale.marketplace_id == models.Marketplace.id)
        .where(
            models.Marketplace.code == mp,
            models.Sale.date >= date_from,
            models.Sale.date <= date_to,
            models.Sale.source != "detail",
            models.Sale.quantity > 0,
        )
        .group_by(models.Sale.article, month)
    )
    rows = db.execute(q).all()
    if not rows:
        return pd.DataFrame(columns=["article", "month", "qty"])
    df = pd.DataFrame(
        [(str(a), _month_key(m), int(qty or 0)) for a, m, qty in rows],
        columns=["article", "month", "qty"],
    )
    return df


def _classify_cv(cv: float, q_x: float, q_y: float) -> str:
    if cv <= q_x:
        return XLVL
    if cv <= q_y:
        return YLVL
    return ZLVL


def classify(df: pd.DataFrame, months: list[str], min_months: int,
             q_x: float = None, q_y: float = None) -> pd.DataFrame:
    """Считает метрики и присваивает класс X/Y/Z/Новинка по помесячному ряду.

    ``df`` — вывод ``xyz_series`` (article, month, qty). ``months`` — полное окно
    (пропущенные месяцы заполняются нулями, иначе CV занижается). Возвращает
    DataFrame ``article, avg_month_qty, cv_pct, active_months, xyz_class``.
    """
    q_x = settings.xyz_x_cv_quantile if q_x is None else q_x
    q_y = settings.xyz_y_cv_quantile if q_y is None else q_y
    cols = ["article", "avg_month_qty", "cv_pct", "active_months", "xyz_class"]
    if df is None or df.empty or not months:
        return pd.DataFrame(columns=cols)

    pivot = (df.pivot_table(index="article", columns="month", values="qty",
                            aggfunc="sum", fill_value=0)
             .reindex(columns=months, fill_value=0))
    mu = pivot.mean(axis=1)
    sd = pivot.std(axis=1, ddof=0)
    active = (pivot > 0).sum(axis=1)

    out = pd.DataFrame({
        "article": pivot.index.astype(str),
        "avg_month_qty": mu.values,
        "cv_pct": (sd / mu.replace(0, pd.NA) * 100).fillna(0).values,
        "active_months": active.values,
    })
    out["active_months"] = out["active_months"].astype(int)

    eligible = (out["active_months"] >= min_months) & (out["avg_month_qty"] > 0)
    out["xyz_class"] = NEWLVL
    if eligible.any():
        cvs = out.loc[eligible, "cv_pct"]
        qx = float(cvs.quantile(q_x))
        qy = float(cvs.quantile(q_y))
        out.loc[eligible, "xyz_class"] = [
            _classify_cv(float(v), qx, qy) for v in cvs]

    out["avg_month_qty"] = out["avg_month_qty"].round(2)
    out["cv_pct"] = out["cv_pct"].round(1)
    return out.loc[:, cols]


def xyz_rows(db, date_from, date_to, marketplace: str = "wb",
             window_months: int = None, min_active_months: int = None,
             ab_rows: pd.DataFrame = None) -> dict:
    """Построчная классификация XYZ (+ выручка/прибыль/ABC из сводки периода).

    Окно стабильности — ``window_months`` календарных месяцев, заканчивающихся
    ``date_to``; если выбранный период длиннее окна, окно расширяется до его
    начала (``date_from``). ``ab_rows`` — сводка ``dashboard._by_article``
    (article, name, revenue, margin) за период: чтобы не считать маржу повторно.
    """
    window_months = window_months or settings.xyz_window_months
    min_active_months = min_active_months or settings.xyz_min_active_months
    start, end = _window(date_to or date.today(), window_months)
    if date_from and date_from < start:
        start = date_from
    months = _month_range(start, end)
    series = xyz_series(db, start, end, marketplace)
    cls = classify(series, months, min_active_months)

    if ab_rows is not None and not ab_rows.empty:
        ab = ab_rows.copy()
        ab["_key"] = ab["article"].astype(str).str.strip().str.upper()
        ab["abc"] = abc_ranks(ab)
        if "sells" not in ab.columns:
            ab["sells"] = 0
        ab = ab[["_key", "name", "revenue", "margin", "abc", "sells"]]
        cls["_key"] = cls["article"].astype(str).str.strip().str.upper()
        cls = cls.merge(ab, on="_key", how="left")
        cls = cls.drop(columns=["_key"])
    else:
        cls["name"] = ""
        cls["revenue"] = 0.0
        cls["margin"] = 0.0
        cls["abc"] = ""
        cls["sells"] = 0.0
    cls["name"] = cls["name"].fillna("").astype(str)
    cls["abc"] = cls["abc"].fillna("").astype(str)
    cls["revenue"] = pd.to_numeric(cls["revenue"], errors="coerce").fillna(0)
    cls["margin"] = pd.to_numeric(cls["margin"], errors="coerce").fillna(0)
    cls["sells"] = pd.to_numeric(cls["sells"], errors="coerce").fillna(0)
    cls = cls.sort_values("avg_month_qty", ascending=False).reset_index(drop=True)

    rows = []
    for rec in cls.to_dict("records"):
        rec["xyz_label"] = XYZ_LABELS.get(rec["xyz_class"], rec["xyz_class"])
        rec["article"] = str(rec["article"])
        rec["avg_month_qty"] = float(rec["avg_month_qty"] or 0)
        rec["cv_pct"] = float(rec["cv_pct"] or 0)
        rec["active_months"] = int(rec["active_months"] or 0)
        rec["revenue"] = float(rec["revenue"] or 0)
        rec["margin"] = float(rec["margin"] or 0)
        rec["sells"] = float(rec.get("sells") or 0)
        rec["abc"] = str(rec.get("abc") or "")
        rows.append(rec)
    return {
        "rows": rows,
        "count": int(len(cls)),
        "meta": {
            "window_months": len(months),
            "min_window_months": int(window_months),
            "date_from": str(start),
            "date_to": str(end),
            "period_from": str(date_from or ""),
            "period_to": str(date_to or ""),
            "min_active_months": min_active_months,
            "x_cv_threshold": _quantile_pct(cls, min_active_months,
                                            settings.xyz_x_cv_quantile),
            "y_cv_threshold": _quantile_pct(cls, min_active_months,
                                            settings.xyz_y_cv_quantile),
            "marketplace": (marketplace or "wb").strip().upper(),
        },
    }


def _quantile_pct(cls: pd.DataFrame, min_months: int, q: float) -> float:
    if cls is None or cls.empty:
        return 0.0
    sub = cls[cls["active_months"] >= min_months]
    if sub.empty:
        return 0.0
    return round(float(sub["cv_pct"].quantile(q)), 1)


def abc_ranks(df: pd.DataFrame) -> pd.Series:
    """Короткие метки A/B/C по накопленной доле выручки (без текста).

    Единственный источник правды для ABC: его использует и группировка
    дашборда (``dashboard._abc_labels``), и матрица ABC×XYZ — иначе группы
    в таблице и в матрице разъезжаются. Без выручки — C.
    """
    ranks = pd.Series("C", index=df.index, dtype=object)
    if df is None or df.empty or "revenue" not in df.columns:
        return ranks
    rev = pd.to_numeric(df["revenue"], errors="coerce").fillna(0)
    total = float(rev.sum() or 0)
    if total <= 0:
        return ranks
    order = rev.sort_values(ascending=False).index
    cum = rev.loc[order].cumsum() / total
    ranks.loc[order[cum <= 0.8]] = "A"
    ranks.loc[order[(cum > 0.8) & (cum <= 0.95)]] = "B"
    return ranks


def abc_xyz_matrix(ab_rows: pd.DataFrame, xyz_map: dict) -> dict:
    """Матрица ABC × XYZ: строки A/B/C, столбцы X/Y/Z/«Новинка».

    ``ab_rows`` — таблица ``article, revenue, margin`` (артикулы периода);
    ``xyz_map`` — ``{article(UPPER): xyz_class}``. Артикул без класса в карте
    (нет продаж в окне) попадает в столбец «Новинка» — там же суммы и итоги.
    """
    grid = {(a, x): {"abc": a, "xyz": x, "articles": 0, "revenue": 0.0,
                     "margin": 0.0}
            for a in ABC_ROWS for x in XYZ_COLS}
    if ab_rows is None or ab_rows.empty:
        return {"cells": list(grid.values()),
                "abc_rows": list(ABC_ROWS), "xyz_cols": list(XYZ_COLS),
                "totals": _matrix_totals(grid)}

    df = ab_rows.copy()
    df["_key"] = df["article"].astype(str).str.strip().str.upper()
    df["abc"] = abc_ranks(df)
    df["xyz"] = df["_key"].map(lambda k: (xyz_map or {}).get(k, NEWLVL))
    df["revenue"] = pd.to_numeric(df["revenue"], errors="coerce").fillna(0)
    df["margin"] = pd.to_numeric(df["margin"], errors="coerce").fillna(0)

    for _, r in df.iterrows():
        cell = grid.get((r["abc"], r["xyz"])) or grid[("C", NEWLVL)]
        cell["articles"] += 1
        cell["revenue"] += float(r["revenue"])
        cell["margin"] += float(r["margin"])

    for c in grid.values():
        c["revenue"] = round(c["revenue"], 2)
        c["margin"] = round(c["margin"], 2)
    return {"cells": list(grid.values()),
            "abc_rows": list(ABC_ROWS), "xyz_cols": list(XYZ_COLS),
            "totals": _matrix_totals(grid)}


def _matrix_totals(grid: dict) -> dict:
    return {
        "articles": sum(c["articles"] for c in grid.values()),
        "revenue": round(sum(c["revenue"] for c in grid.values()), 2),
        "margin": round(sum(c["margin"] for c in grid.values()), 2),
    }
