# -*- coding: utf-8 -*-
"""Маржинальность: /api/margin/* (+ сравнение между связками links=id,id,…)."""
import pandas as pd

from app.api._common import *  # noqa: F401,F403
from app.api._common import (
    _cashflow_received,
    _compare_window,  # noqa: F401  (реэкспорт: тесты и детализации)
    _margin_detail_totals,
    _parse_window400,
    _resolve_compare_window,
    _resolve_links_param,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# Детализация продаж WB
# ---------------------------------------------------------------------------
def _wb_detail_payload(db, from_, to_, article_like, compare, compare_window, owner="",
                       tokens=None):
    arts, matched = article_tokens_service.resolve_articles(db, tokens) \
        if tokens else (None, set())
    df = margin_service.margin_detail_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like,
        default_net_cost=settings.default_net_cost, articles=arts,
    )
    prev_window = None
    if compare and compare_window:
        prev_from, prev_to = compare_window
        prev_df = margin_service.margin_detail_dataframe(
            db, date_from=prev_from, date_to=prev_to, article_like=article_like,
            default_net_cost=settings.default_net_cost, articles=arts,
        )
        df = margin_service.compare_margin_periods(df, prev_df)
        prev_window = {"date_from": prev_from.isoformat(), "date_to": prev_to.isoformat()}
    detail_articles = 0
    if from_ and to_:
        q = select(func.count(func.distinct(models.WbDetailRow.article))).where(
            models.WbDetailRow.article != "",
            models.WbDetailRow.sale_dt.isnot(None),
            models.WbDetailRow.sale_dt >= from_,
            models.WbDetailRow.sale_dt <= to_,
        )
        if article_like:
            q = q.where(common_service.like_col(models.WbDetailRow.article, article_like))
        detail_articles = int(db.execute(q).scalar_one() or 0)
    if owner and not df.empty:
        df = df.copy()
        df["owner"] = owner
    return df, detail_articles, prev_window, matched


@router.get("/margin/detail")
def api_margin_detail(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    articles: Optional[str] = None,
    compare: int = 0,
    compare_mode: Optional[str] = None,
    compare_from: Optional[str] = None,
    compare_to: Optional[str] = None,
    links: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Прибыльность по Детализации Продаж WB напрямую из wb_detail_rows.

    Себестоимость из каталога; без неё — оценка (settings.default_net_cost),
    помечается net_cost_est=True.

    compare=1 добавляет показатели периода сравнения: sells_pp, margin_pp,
    delta_ru (руб), delta_pct (%). Сравниваемое окно задаёт ``compare_mode``:
    пусто/prev — предыдущий аналогичный период (по умолчанию), custom —
    явные compare_from/compare_to, last_year — то же окно год назад.
    Требует даты date_from/date_to.

    links=id,id,… — сравнение между WB-связками: строки из всех связок сводятся
    вместе с колонкой «owner» (владельца связки), итоги считаются по сводке.

    articles=ART1,ART2,… — фильтр по списку (поле «Артикулы» на вкладке):
    артикулы, баркоды, nmID WB и артикулы Ozon резолвятся в наши артикулы
    (см. ``article_tokens``); действует вместе с article_like (И). В ответе
    ``articles`` — сколько значений распознано и какие не найдены.
    """
    from_, to_ = _parse_window400(date_from, date_to)
    compare_window = _resolve_compare_window(
        date_from, date_to, compare_mode, compare_from, compare_to,
        ozon_small=False) if compare else None
    tokens = article_tokens_service.split_tokens(articles)

    link_infos = _resolve_links_param(db, links, "wb")
    matched: set = set()
    if len(link_infos) > 1:
        results = cabinet_service.run_per_link(
            db, link_infos,
            lambda db, info: _wb_detail_payload(
                db, from_, to_, article_like, bool(compare), compare_window,
                owner=cabinet_service.link_owner(db, info), tokens=tokens),
        )
        frames = [df for _, (df, _, _, _) in results if df is not None and not df.empty]
        df = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
        detail_articles = sum(da for _, (_, da, _, _) in results)
        prev_window = next((pw for _, (_, _, pw, _) in results if pw), None)
        for _, (_, _, _, m) in results:
            matched |= m
    else:
        df, detail_articles, prev_window, matched = _wb_detail_payload(
            db, from_, to_, article_like, bool(compare), compare_window, owner="",
            tokens=tokens)

    estimated = int(df["net_cost_est"].sum()) if not df.empty and "net_cost_est" in df else 0
    totals = _margin_detail_totals(df)
    payload = {
        "rows": df.replace({None: ""}).to_dict("records"),
        "count": len(df),
        "detail_articles": detail_articles,
        "estimated": estimated,
        "default_net_cost": settings.default_net_cost,
        "prev_window": prev_window,
        "totals": totals,
    }
    if tokens:
        unmatched = [t for t in tokens if t.upper() not in matched]
        payload["articles"] = {
            "tokens": len(tokens),
            "matched": len(tokens) - len(unmatched),
            "unmatched": unmatched[:50],
        }
    return payload


@router.post("/margin/articles-import")
async def api_margin_articles_import(file: UploadFile = File(...)):
    """Excel со списком артикулов → токены для фильтра «Артикулы».

    Первый столбец берём всегда; если есть шапка, дополнительно читаем
    колонки «Артикул/Баркод/Штрихкод/nmID/SKU/Ozon/vendor».
    """
    name = (file.filename or "").strip().lower()
    if not name.endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Нужен файл .xlsx")
    data = await file.read()
    if not data:
        raise HTTPException(400, "Файл пустой")
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(400, "Файл больше 20 МБ — похоже, это не список артикулов")
    try:
        tokens, meta = article_tokens_service.tokens_from_xlsx(data)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, "Не удалось прочитать Excel: " + str(e))
    if not tokens:
        raise HTTPException(400, "В файле не нашлось артикулов")
    meta["file"] = file.filename or ""
    return {"tokens": tokens, "meta": meta}


# ---------------------------------------------------------------------------
# Детализация продаж Ozon
# ---------------------------------------------------------------------------
def _oz_detail_payload(db, from_, to_, article_like, compare, compare_window,
                       by_size, owner="", tokens=None):
    arts, matched = article_tokens_service.resolve_articles(db, tokens) \
        if tokens else (None, set())
    df = margin_service.ozon_margin_detail_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like,
        default_net_cost=settings.default_net_cost, by_size=bool(by_size),
        articles=arts,
    )
    prev_window = None
    if compare and compare_window:
        prev_from, prev_to = compare_window
        prev_df = margin_service.ozon_margin_detail_dataframe(
            db, date_from=prev_from, date_to=prev_to, article_like=article_like,
            default_net_cost=settings.default_net_cost, by_size=bool(by_size),
            articles=arts,
        )
        df = margin_service.compare_margin_periods(df, prev_df)
        prev_window = {"date_from": prev_from.isoformat(), "date_to": prev_to.isoformat()}
    detail_articles = 0
    if from_ and to_:
        # сколько товаров в окне: в свёрнутом режиме считаем базовые артикулы
        art_col = (models.OzonDetailRow.offer_id if by_size
                   else func.coalesce(
                       func.nullif(models.OzonDetailRow.base_article, ""),
                       models.OzonDetailRow.offer_id))
        q = select(func.count(func.distinct(art_col))).where(
            models.OzonDetailRow.offer_id != "",
            models.OzonDetailRow.date.isnot(None),
            models.OzonDetailRow.date >= from_,
            models.OzonDetailRow.date <= to_,
        )
        if article_like:
            q = q.where(common_service.like_col(models.OzonDetailRow.offer_id, article_like)
                        | common_service.like_col(models.OzonDetailRow.base_article, article_like))
        detail_articles = int(db.execute(q).scalar_one() or 0)
    received, periods = _cashflow_received(db, from_, to_)
    # Оценка «на р/с за товар»: доля фактических выплат (движение средств за окно)
    # от начислений «к перечислению», распределённая пропорционально income.
    cashflow_ratio = None
    if not df.empty and "income" in df and received is not None:
        sum_income = float(df["income"].sum() or 0)
        if sum_income > 0:
            cashflow_ratio = round(received / sum_income * 100, 1)
            df = df.copy()
            df["cashflow_est"] = (df["income"] * received / sum_income).round(2)
    # Точные начисления (аккруалы) за окно.
    accrued_total = None
    if not df.empty and "accrued_net" in df:
        accrued_total = round(float(df["accrued_net"].sum() or 0), 2)
    accrued_rows = 0
    accrued_other = 0.0
    accrued_unmapped = 0.0
    if from_ and to_:
        acc_art = (models.OzonAccrual.offer_id if by_size
                   else func.coalesce(
                       func.nullif(models.OzonAccrual.base_article, ""),
                       models.OzonAccrual.offer_id))
        q = select(func.count(func.distinct(acc_art))).where(
            models.OzonAccrual.offer_id != "",
            models.OzonAccrual.date.isnot(None),
            models.OzonAccrual.date >= from_,
            models.OzonAccrual.date <= to_,
        )
        accrued_rows = int(db.execute(q).scalar_one() or 0)
        aq = select(func.coalesce(func.sum(models.OzonAccrual.amount), 0.0)).where(
            models.OzonAccrual.date.isnot(None),
            models.OzonAccrual.date >= from_,
            models.OzonAccrual.date <= to_,
        )
        # «прочее» (NON_ITEM) — расходы без товара; «нераспределённые» — строки
        # со SKU, у которого не нашлось артикула (нет продаж в детализации).
        accrued_other = round(float(db.execute(
            aq.where(models.OzonAccrual.bucket == "other")).scalar_one() or 0), 2)
        accrued_unmapped = round(float(db.execute(
            aq.where(models.OzonAccrual.offer_id == "",
                     models.OzonAccrual.sku != "")).scalar_one() or 0), 2)
    if owner and not df.empty:
        df = df.copy()
        df["owner"] = owner
    return {
        "df": df,
        "detail_articles": detail_articles,
        "prev_window": prev_window,
        "received": received,
        "periods": periods,
        "cashflow_ratio": cashflow_ratio,
        "accrued_total": accrued_total,
        "accrued_rows": accrued_rows,
        "accrued_other": accrued_other,
        "accrued_unmapped": accrued_unmapped,
        "detail_range": ozon_detail_range(db),
        "matched": matched,
    }


@router.get("/margin/ozon-detail")
def api_margin_ozon_detail(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    articles: Optional[str] = None,
    compare: int = 0,
    compare_mode: Optional[str] = None,
    compare_from: Optional[str] = None,
    compare_to: Optional[str] = None,
    by_size: int = 0,
    links: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Прибыльность по Детализации Продаж Ozon напрямую из ozon_detail_rows.

    Аналог margin/detail для WB. income в Ozon уже чистый к перечислению
    (комиссия и услуги вычтены), поэтому Прибыль = income − себестоимость×продано;
    commission/services показываются справочными колонками (в минусе).

    by_size=0 (по умолчанию) — строка это товар: артикулы размеров свёрнуты в
    базовый артикул, себестоимость берётся у базового артикула, размеры и
    артикулы показаны количеством. by_size=1 — строка это артикул размера.

    articles=ART1,ART2,… — фильтр по списку (как на «Детализация Продаж WB»):
    артикулы/баркоды/nmID WB/артикулы Ozon резолвятся в UPPER-артикулы и
    складываются с article_like через «И».

    compare=1 добавляет показатели предыдущего аналогичного периода
    (та же длительность окна, сдвинутая назад): sells_pp, margin_pp,
    delta_ru (руб), delta_pct (%). Требует даты date_from/date_to.

    links=id,id,… — сравнение между Ozon-связками (аналог margin/detail).
    """
    from_, to_ = _parse_window400(date_from, date_to)
    compare_window = _resolve_compare_window(
        date_from, date_to, compare_mode, compare_from, compare_to,
        ozon_small=True) if compare else None
    tokens = article_tokens_service.split_tokens(articles)

    link_infos = _resolve_links_param(db, links, "ozon")
    if len(link_infos) > 1:
        results = cabinet_service.run_per_link(
            db, link_infos,
            lambda db, info: _oz_detail_payload(
                db, from_, to_, article_like, bool(compare), compare_window,
                bool(by_size), owner=cabinet_service.link_owner(db, info),
                tokens=tokens),
        )
        pls = [p for _, p in results]
        frames = [p["df"] for p in pls if p["df"] is not None and not p["df"].empty]
        df = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
        detail_articles = sum(p["detail_articles"] for p in pls)
        prev_window = next((p["prev_window"] for p in pls if p["prev_window"]), None)
        received = sum(p["received"] for p in pls if p["received"] is not None) or None
        periods = sum(p["periods"] or 0 for p in pls)
        accrued_total = sum(p["accrued_total"] for p in pls if p["accrued_total"] is not None) or None
        accrued_rows = sum(p["accrued_rows"] for p in pls)
        accrued_other = round(sum(p["accrued_other"] for p in pls), 2)
        accrued_unmapped = round(sum(p["accrued_unmapped"] for p in pls), 2)
        ranges = [p["detail_range"] for p in pls]
        d1 = min((r["date_from"] for r in ranges if r.get("date_from")), default=None)
        d2 = max((r["date_to"] for r in ranges if r.get("date_to")), default=None)
        detail_range = {"date_from": d1, "date_to": d2,
                        "rows": sum(r.get("rows") or 0 for r in ranges)}
        cashflow_ratio = None
        if received is not None and not df.empty and "income" in df:
            sum_income = float(df["income"].sum() or 0)
            if sum_income > 0:
                cashflow_ratio = round(received / sum_income * 100, 1)
    else:
        one = _oz_detail_payload(
            db, from_, to_, article_like, bool(compare), compare_window,
            bool(by_size), owner="", tokens=tokens)
        df = one["df"]
        detail_articles = one["detail_articles"]
        prev_window = one["prev_window"]
        received = one["received"]
        periods = one["periods"]
        cashflow_ratio = one["cashflow_ratio"]
        accrued_total = one["accrued_total"]
        accrued_rows = one["accrued_rows"]
        accrued_other = one["accrued_other"]
        accrued_unmapped = one["accrued_unmapped"]
        detail_range = one["detail_range"]

    estimated = int(df["net_cost_est"].sum()) if not df.empty and "net_cost_est" in df else 0
    totals = _margin_detail_totals(df)
    payload = {
        "rows": df.replace({None: ""}).to_dict("records"),
        "count": len(df),
        "detail_articles": detail_articles,
        "estimated": estimated,
        "default_net_cost": settings.default_net_cost,
        "prev_window": prev_window,
        "totals": totals,
        "cashflow_received": received,
        "cashflow_periods": periods,
        "cashflow_ratio": cashflow_ratio,
        "accrued_total": accrued_total,
        "accrued_rows": accrued_rows,
        "accrued_other": accrued_other,
        "accrued_unmapped": accrued_unmapped,
        "window": {"date_from": date_from or "", "date_to": date_to or ""},
        "detail_range": detail_range,
    }
    if tokens:
        matched, _ = article_tokens_service.resolve_articles(db, tokens)
        up_all = {t.upper() for t in tokens}
        payload["articles"] = {
            "tokens": len(tokens),
            "matched": len(up_all & {m.upper() for m in matched}),
            "unmatched": sorted(up_all - {m.upper() for m in matched})[:50],
        }
    return payload


# ---------------------------------------------------------------------------
# Воронка продаж WB
# ---------------------------------------------------------------------------
@router.get("/margin/funnel")
def api_margin_funnel(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    articles: Optional[str] = None,
    links: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Прибыльность по Воронке Продаж WB (данные funnel_metric, оценка).

    Срез выбирается через pick_funnel_window, как в /api/funnel: точное окно →
    самый широкий внутри запрошенного → самый свежий пересекающийся → последний
    в базе. date_from/date_to — запрошенный период, snapshot_from/snapshot_to —
    фактически показанный срез, matched — совпали ли они.

    articles=ART1,ART2,… — фильтр по списку (как на «Детализация Продаж WB»):
    артикулы/баркоды/nmID WB/артикулы Ozon резолвятся в UPPER-артикулы и
    складываются с article_like через «И».

    links=id,id,… — сравнение между WB-связками: срезы всех связок сводятся
    вместе с колонкой «owner».
    """
    from_, to_ = _parse_window400(date_from, date_to)
    tokens = article_tokens_service.split_tokens(articles)
    arts, matched = article_tokens_service.resolve_articles(db, tokens) \
        if tokens else (None, set())

    def _funnel(db, info=None):
        owner = cabinet_service.link_owner(db, info) if info is not None else ""
        df = margin_service.funnel_dataframe(
            db, date_from=from_, date_to=to_, article_like=article_like,
            articles=arts,
        )
        if owner and not df.empty:
            df = df.copy()
            df["owner"] = owner
        return df

    link_infos = _resolve_links_param(db, links, "wb")
    if len(link_infos) > 1:
        results = cabinet_service.run_per_link(db, link_infos,
                               lambda db, info: _funnel(db, info))
        frames = [f for _, f in results if f is not None and not f.empty]
        df = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
        snap = next((f for _, f in results if not f.empty), pd.DataFrame())
    else:
        df = _funnel(db)
        snap = df

    payload = {
        "rows": df.replace({None: ""}).to_dict("records"),
        "count": len(df),
        "date_from": str(from_),
        "date_to": str(to_),
        "snapshot_from": snap.attrs.get("date_from", ""),
        "snapshot_to": snap.attrs.get("date_to", ""),
        "matched": bool(snap.attrs.get("matched", False)),
    }
    if tokens:
        up_all = {t.upper() for t in tokens}
        payload["articles"] = {
            "tokens": len(tokens),
            "matched": len(up_all & {m.upper() for m in matched}),
            "unmatched": sorted(up_all - {m.upper() for m in matched})[:50],
        }
    return payload