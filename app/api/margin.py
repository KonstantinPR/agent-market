# -*- coding: utf-8 -*-
"""Маржинальность: /api/margin/*."""
from app.api._common import *  # noqa: F401,F403
from app.api._common import (
    _cashflow_received,
    _margin_detail_totals,
    _parse_window400,
)

router = APIRouter()



@router.get("/margin/detail")
def api_margin_detail(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    compare: int = 0,
    db: Session = Depends(get_db),
):
    """Прибыльность по Детализации Продаж WB напрямую из wb_detail_rows.

    Себестоимость из каталога; без неё — оценка (settings.default_net_cost),
    помечается net_cost_est=True.

    compare=1 добавляет показатели предыдущего аналогичного периода
    (та же длительность окна, сдвинутая назад): sells_pp, margin_pp,
    delta_ru (руб), delta_pct (%). Требует даты date_from/date_to.
    """
    from_, to_ = _parse_window400(date_from, date_to)

    df = margin_service.margin_detail_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like,
        default_net_cost=settings.default_net_cost,
    )
    prev_window = None
    if compare and date_from and date_to:
        try:
            f = date.fromisoformat(date_from)
            t = date.fromisoformat(date_to)
            delta = (t - f).days
            prev_from = f - timedelta(days=delta)
            prev_to = f - timedelta(days=1)
            prev_df = margin_service.margin_detail_dataframe(
                db, date_from=prev_from, date_to=prev_to, article_like=article_like,
                default_net_cost=settings.default_net_cost,
            )
            df = margin_service.compare_margin_periods(df, prev_df)
            prev_window = {"date_from": prev_from.isoformat(), "date_to": prev_to.isoformat()}
        except (ValueError, TypeError):
            prev_window = None
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
    estimated = int(df["net_cost_est"].sum()) if not df.empty and "net_cost_est" in df else 0
    totals = _margin_detail_totals(df)
    return {
        "rows": df.replace({None: ""}).to_dict("records"),
        "count": len(df),
        "detail_articles": detail_articles,
        "estimated": estimated,
        "default_net_cost": settings.default_net_cost,
        "prev_window": prev_window,
        "totals": totals,
    }


@router.get("/margin/ozon-detail")
def api_margin_ozon_detail(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    compare: int = 0,
    by_size: int = 0,
    db: Session = Depends(get_db),
):
    """Прибыльность по Детализации Продаж Ozon напрямую из ozon_detail_rows.

    Аналог margin/detail для WB. income в Ozon уже чистый к перечислению
    (комиссия и услуги вычтены), поэтому Прибыль = income − себестоимость×продано;
    commission/services показываются справочными колонками (в минусе).

    by_size=0 (по умолчанию) — строка это товар: артикулы размеров свёрнуты в
    базовый артикул, себестоимость берётся у базового артикула, размеры и
    артикулы показаны количеством. by_size=1 — строка это артикул размера.

    compare=1 добавляет показатели предыдущего аналогичного периода
    (та же длительность окна, сдвинутая назад): sells_pp, margin_pp,
    delta_ru (руб), delta_pct (%). Требует даты date_from/date_to.
    """
    from_, to_ = _parse_window400(date_from, date_to)

    df = margin_service.ozon_margin_detail_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like,
        default_net_cost=settings.default_net_cost, by_size=bool(by_size),
    )
    prev_window = None
    if compare and date_from and date_to:
        try:
            f = date.fromisoformat(date_from)
            t = date.fromisoformat(date_to)
            delta = (t - f).days
            prev_from = f - timedelta(days=delta + 1)
            prev_to = f - timedelta(days=1)
            prev_df = margin_service.ozon_margin_detail_dataframe(
                db, date_from=prev_from, date_to=prev_to, article_like=article_like,
                default_net_cost=settings.default_net_cost, by_size=bool(by_size),
            )
            df = margin_service.compare_margin_periods(df, prev_df)
            prev_window = {"date_from": prev_from.isoformat(), "date_to": prev_to.isoformat()}
        except (ValueError, TypeError):
            prev_window = None
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
    estimated = int(df["net_cost_est"].sum()) if not df.empty and "net_cost_est" in df else 0
    totals = _margin_detail_totals(df)
    received, periods = _cashflow_received(db, from_, to_)
    # Точные начисления (аккруалы) за окно — «сколько реально перечислит Ozon»:
    # из ozon_accruals (продажа минус комиссия, логистика, услуги, прочее).
    # Сверка: сумма по артикулам + нераспределённые = итог начислений за окно.
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
    # Оценка «на р/с за товар»: доля фактических выплат (движение средств за окно)
    # от начислений «к перечислению», распределённая пропорционально income.
    # Сумма cashflow_est по артикулам сходится с фактически полученным за окно.
    cashflow_ratio = None
    if not df.empty and "income" in df and received is not None:
        sum_income = float(df["income"].sum() or 0)
        if sum_income > 0:
            cashflow_ratio = round(received / sum_income * 100, 1)
            df["cashflow_est"] = (df["income"] * received / sum_income).round(2)
    totals = _margin_detail_totals(df)
    return {
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
        "detail_range": ozon_detail_range(db),
    }


@router.get("/margin/funnel")
def api_margin_funnel(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Прибыльность по Воронке Продаж WB (данные funnel_metric, оценка).

    Срез выбирается через pick_funnel_window, как в /api/funnel: точное окно →
    самый широкий внутри запрошенного → самый свежий пересекающийся → последний
    в базе. date_from/date_to — запрошенный период, snapshot_from/snapshot_to —
    фактически показанный срез, matched — совпали ли они.
    """
    from_, to_ = _parse_window400(date_from, date_to)

    df = margin_service.funnel_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like
    )
    return {
        "rows": df.replace({None: ""}).to_dict("records"),
        "count": len(df),
        "date_from": str(from_),
        "date_to": str(to_),
        "snapshot_from": df.attrs.get("date_from", ""),
        "snapshot_to": df.attrs.get("date_to", ""),
        "matched": bool(df.attrs.get("matched", False)),
    }
