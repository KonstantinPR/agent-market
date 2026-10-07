# -*- coding: utf-8 -*-
"""Ozon API-вьюхи и загрузки: /api/ozon/*."""
from app.api._common import *  # noqa: F401,F403
from app.api._common import (
    _cashflow_received,
    _df_totals,
    _ozon_placement_agg,
    _parse_window400,
    _pull_json,
    _xlsx_response,
)

router = APIRouter()



@router.post("/ozon/cards")
def ozon_cards(write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_oz_cards(db, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    if not int(excel):
        return _pull_json(res)
    return _xlsx_response(res["df"], "ozon_cards.xlsx", res["count"])


@router.post("/ozon/stock")
def ozon_stock(write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_oz_stock(db, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    if not int(excel):
        return _pull_json(res)
    return _xlsx_response(res["df"], "ozon_stock.xlsx", res["count"])


@router.post("/ozon/prices")
def ozon_prices(write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_oz_prices(db, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    if not int(excel):
        return _pull_json(res)
    return _xlsx_response(res["df"], "ozon_prices.xlsx", res["count"])


@router.post("/ozon/realization")
def ozon_realization(date_from: Optional[str] = None, date_to: Optional[str] = None,
                     month: Optional[int] = None, year: Optional[int] = None,
                     write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    """Реализация за месяцы, покрывающие окно из шапки (или точный месяц).

    Приоритет: явные month/year → месяцы интервала date_from..date_to →
    прошлый месяц по умолчанию.
    """
    if month is not None and year is not None:
        res = refresh_service.pull_oz_realization(db, month, year, write_db=bool(write_db))
        label = f"{year:04d}-{month:02d}"
    elif date_from and date_to:
        from_, to_ = _parse_window400(date_from, date_to)
        res = refresh_service.pull_oz_realizations(db, from_, to_, write_db=bool(write_db))
        label = f"{from_}_{to_}"
    else:
        today = date.today().replace(day=1) - timedelta(days=1)
        res = refresh_service.pull_oz_realization(db, today.month, today.year,
                                                  write_db=bool(write_db))
        label = f"{today.year:04d}-{today.month:02d}"
    if not int(excel):
        return _pull_json(res)
    df = res["df"]
    export = df.rename(columns=OZON_RU_COLUMNS) if not df.empty else df
    return _xlsx_response(export, f"ozon_realization_{label}.xlsx", res["count"])


@router.post("/ozon/cashflow")
def ozon_cashflow(date_from: Optional[str] = None, date_to: Optional[str] = None,
                  excel: int = 1, write_db: int = 1, db: Session = Depends(get_db)):
    from_, to_ = _parse_window400(date_from, date_to)
    try:
        res = refresh_service.pull_oz_cashflow(db, from_, to_, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    if not int(excel):
        return _pull_json(res)
    return _xlsx_response(res["df"], f"ozon_cashflow_{from_}_{to_}.xlsx", res["count"])


@router.get("/ozon/cashflow-rows")
def api_ozon_cashflow_rows(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Периоды «Движения средств» Ozon (ozon_cash_flows) за окно + итоги.

    payments_amount хранится отрицательным (деньги ушли на расчётный счёт),
    поэтому «фактически получено» = -(сумма payments_amount)."""
    from_, to_ = _parse_window400(date_from, date_to)
    q = select(models.OzonCashFlow).where(models.OzonCashFlow.period_begin.isnot(None))
    if from_:
        q = q.where(models.OzonCashFlow.period_end >= from_)
    if to_:
        q = q.where(models.OzonCashFlow.period_begin <= to_)
    rows = db.execute(
        q.order_by(models.OzonCashFlow.period_begin.asc())
    ).scalars().all()
    out = [{
        "period_begin": r.period_begin.isoformat() if r.period_begin else "",
        "period_end": r.period_end.isoformat() if r.period_end else "",
        "begin_balance": float(r.begin_balance or 0),
        "payments_amount": float(r.payments_amount or 0),
        "delivery_total": float(r.delivery_total or 0),
        "return_total": float(r.return_total or 0),
        "services_total": float(r.services_total or 0),
        "others_total": float(r.others_total or 0),
        "end_balance": float(r.end_balance or 0),
    } for r in rows]
    df = pd.DataFrame(out) if out else pd.DataFrame()
    totals = _df_totals(df, extra_skip=("begin_balance", "end_balance")) if len(df) else {}
    received = round(-(totals.get("payments_amount") or 0), 2) if totals else 0.0
    return {"rows": out, "count": len(out), "totals": totals,
            "received": received,
            "window": req_window(date_from, date_to),
            "detail_range": ozon_date_range(db, models.OzonCashFlow,
                                            models.OzonCashFlow.period_begin)}


@router.post("/ozon/accrual")
def ozon_accrual(date_from: Optional[str] = None, date_to: Optional[str] = None,
                 excel: int = 1, write_db: int = 1, db: Session = Depends(get_db)):
    """Начисления по товарам (аккруалы) Ozon за окно: /v1/finance/accrual/by-day.

    Побуквенно: продажа (sale), комиссия (commission), логистика (logistics) —
    из POSTING; услуги (services) — из ITEM; прочее (other) — NON_ITEM.
    """
    from_, to_ = _parse_window400(date_from, date_to)
    try:
        res = refresh_service.pull_oz_accrual(db, from_, to_, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    if not int(excel):
        return _pull_json(res)
    df = res["df"]
    export = df.rename(columns=OZON_ACCRUAL_RU_COLUMNS) if not df.empty else df
    return _xlsx_response(export, f"ozon_accrual_{from_}_{to_}.xlsx", res["count"])


@router.get("/ozon/accrual-rows")
def api_ozon_accrual_rows(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    bucket: Optional[str] = None,
    article_like: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Строки начислений Ozon (ozon_accruals) за окно + итоги по корзинам.

    amount отрицательный — расход (комиссия/логистика/услуги), положительный —
    продажа. bucket: sale | commission | logistics | services | other.
    """
    from_, to_ = _parse_window400(date_from, date_to)
    q = select(models.OzonAccrual)
    if from_:
        q = q.where(models.OzonAccrual.date >= from_)
    if to_:
        q = q.where(models.OzonAccrual.date <= to_)
    if bucket:
        q = q.where(models.OzonAccrual.bucket == bucket)
    if article_like:
        q = q.where(common_service.like_col(models.OzonAccrual.offer_id, article_like)
                    | common_service.like_col(models.OzonAccrual.base_article, article_like))
    rows = db.execute(
        q.order_by(models.OzonAccrual.date.asc())
    ).scalars().all()
    out = [{
        "date": r.date.isoformat() if r.date else "",
        "accrual_id": r.accrual_id or "",
        "bucket": r.bucket or "",
        "type_id": int(r.type_id or 0),
        "sku": r.sku or "",
        "offer_id": r.offer_id or "",
        "unit_number": r.unit_number or "",
        "quantity": int(r.quantity or 0),
        "amount": float(r.amount or 0),
        "seller_price": float(r.seller_price or 0),
        "sale_price": float(r.sale_price or 0),
    } for r in rows]
    df = pd.DataFrame(out) if out else pd.DataFrame()
    totals = _df_totals(df) if len(df) else {}
    return {"rows": out, "count": len(out), "totals": totals,
            "window": req_window(date_from, date_to),
            "detail_range": ozon_date_range(db, models.OzonAccrual)}


@router.post("/ozon/detail")
def ozon_detail(date_from: Optional[str] = None, date_to: Optional[str] = None,
                write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    from_, to_ = _parse_window400(date_from, date_to)
    try:
        res = refresh_service.pull_oz_detail(db, from_, to_, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    if not int(excel):
        return _pull_json(res)
    df = res["df"]
    export = df.rename(columns=OZON_DETAIL_RU_COLUMNS) if not df.empty else df
    return _xlsx_response(export, f"ozon_detail_{from_}_{to_}.xlsx", res["count"])


@router.post("/ozon/buyout")
def ozon_buyout(date_from: Optional[str] = None, date_to: Optional[str] = None,
                write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    from_, to_ = _parse_window400(date_from, date_to)
    try:
        res = refresh_service.pull_oz_buyout(db, from_, to_, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    if not int(excel):
        return _pull_json(res)
    df = res["df"]
    export = df.rename(columns=OZON_BUYOUT_RU_COLUMNS) if not df.empty else df
    return _xlsx_response(export, f"ozon_buyout_{from_}_{to_}.xlsx", res["count"])


@router.post("/ozon/placement")
def ozon_placement(date_from: Optional[str] = None, date_to: Optional[str] = None,
                   write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    from_, to_ = _parse_window400(date_from, date_to)
    try:
        res = refresh_service.pull_oz_placements(db, from_, to_, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.oz_error(e)
    if not int(excel):
        return _pull_json(res)
    df = res["df"]
    export = df.rename(columns=OZON_PLACEMENT_RU_COLUMNS) if not df.empty else df
    return _xlsx_response(export, f"ozon_placement_{from_}_{to_}.xlsx", res["count"])


@router.get("/ozon/placement-rows")
def api_ozon_placement_rows(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Сырые строки «Размещения (хранения)» Ozon (ozon_placements) с фильтрами."""
    from_, to_ = _parse_window400(date_from, date_to)
    q = select(models.OzonPlacement).where(models.OzonPlacement.date.isnot(None))
    if from_:
        q = q.where(models.OzonPlacement.date >= from_)
    if to_:
        q = q.where(models.OzonPlacement.date <= to_)
    if article_like:
        q = q.where(common_service.like_col(models.OzonPlacement.offer_id, article_like)
                    | common_service.like_col(models.OzonPlacement.base_article, article_like))
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = db.execute(
        q.order_by(models.OzonPlacement.date.asc(), models.OzonPlacement.offer_id.asc())
        .offset(offset).limit(limit)
    ).scalars().all()
    out = [{
        "date": r.date.isoformat() if r.date else "",
        "sku": r.sku, "offer_id": r.offer_id, "name": r.offer_id,
        "warehouse": r.warehouse, "paid_quantity": r.paid_quantity,
        "paid_volume": float(r.paid_volume or 0),
        "storage": float(r.storage or 0),
    } for r in rows]
    df = pd.DataFrame(out) if out else pd.DataFrame()
    return {"rows": out, "total": int(total),
            "totals": _df_totals(df) if len(df) else {},
            "window": {"date_from": date_from or "", "date_to": date_to or ""},
            "detail_range": ozon_date_range(db, models.OzonPlacement)}


@router.get("/ozon/placement-summary")
def api_ozon_placement_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    by_size: int = 0,
    db: Session = Depends(get_db),
):
    """Свод «Размещения (хранения)» Ozon по артикулам: дни, кол-во, объём, сумма.

    Показывает ВСЕ SKU из ozon_placements за окно (включая те, у которых
    в окне не было продаж) — в отличие от свода «Детализация продаж».

    by_size=0 (по умолчанию) — строка это товар (артикулы размеров свёрнуты),
    by_size=1 — строка это артикул конкретного размера.
    """
    from_, to_ = _parse_window400(date_from, date_to)
    out = _ozon_placement_agg(db, from_, to_, article_like, by_size=bool(by_size))
    df = pd.DataFrame(out) if out else pd.DataFrame()
    return {"rows": out, "count": len(out),
            "totals": _df_totals(df, extra_skip=(
                "days", "ops_count", "sizes_count", "offers_count")) if len(df) else {},
            "window": {"date_from": date_from or "", "date_to": date_to or ""},
            "detail_range": ozon_date_range(db, models.OzonPlacement)}


@router.get("/ozon/detail-rows")
def api_ozon_detail_rows(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Сырые строки «Детализации реализаций» Ozon (ozon_detail_rows) с фильтрами."""
    from_, to_ = _parse_window400(date_from, date_to)
    q = select(models.OzonDetailRow).where(models.OzonDetailRow.date.isnot(None))
    if from_:
        q = q.where(models.OzonDetailRow.date >= from_)
    if to_:
        q = q.where(models.OzonDetailRow.date <= to_)
    if article_like:
        q = q.where(common_service.like_col(models.OzonDetailRow.offer_id, article_like)
                    | common_service.like_col(models.OzonDetailRow.base_article, article_like))
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = db.execute(
        q.order_by(models.OzonDetailRow.date.desc(), models.OzonDetailRow.id.desc())
        .offset(offset).limit(limit)
    ).scalars().all()
    out = [{
        "date": r.date.isoformat() if r.date else "",
        "posting_number": r.posting_number,
        "offer_id": r.offer_id,
        "name": r.name,
        "sku": r.sku,
        "barcode": r.barcode,
        "quantity": r.quantity,
        "seller_price": float(r.seller_price or 0),
        "amount": float(r.amount or 0),
        "commission_ratio": float(r.commission_ratio or 0),
        "commission": float(r.commission or 0),
        "standard_fee": float(r.standard_fee or 0),
        "income": float(r.income or 0),
        "return_qty": r.return_qty,
        "return_total": float(r.return_total or 0),
        "source": r.source,
    } for r in rows]
    df = pd.DataFrame(out) if out else pd.DataFrame()
    return {
        "rows": out,
        "total": int(total),
        "totals": _df_totals(df) if len(df) else {},
        "window": {"date_from": date_from or "", "date_to": date_to or ""},
        "detail_range": ozon_detail_range(db),
    }


@router.get("/ozon/detail-summary")
def api_ozon_detail_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    by_size: int = 0,
    db: Session = Depends(get_db),
):
    """Свод «Детализации реализаций» Ozon по артикулам (+ выкупы).

    by_size=0 (по умолчанию) — строка это товар (артикулы размеров свёрнуты),
    by_size=1 — строка это артикул конкретного размера.
    """
    from_, to_ = _parse_window400(date_from, date_to)
    df = sync_service.oz_detail_summary_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like,
        by_size=bool(by_size),
    )
    resp = {
        "rows": df.replace({None: ""}).to_dict("records"),
        "count": len(df),
        "totals": _df_totals(df, extra_skip=("sizes_count", "offers_count")),
    }
    # «Фактически получено на р/с» по периодам движения средств,
    # пересекающимся с окном (payments_amount хранится в минусе).
    received, periods = _cashflow_received(db, from_, to_)
    resp["cashflow_received"] = received
    resp["cashflow_periods"] = periods
    resp["window"] = {"date_from": date_from or "", "date_to": date_to or ""}
    resp["detail_range"] = ozon_detail_range(db)
    return resp


@router.get("/ozon/buyout-rows")
def api_ozon_buyout_rows(
    article_like: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Сырые строки «Выкупов» Ozon (ozon_buyouts) с фильтром по артикулу."""
    q = select(models.OzonBuyout)
    if article_like:
        q = q.where(common_service.like_col(models.OzonBuyout.offer_id, article_like)
                    | common_service.like_col(models.OzonBuyout.base_article, article_like))
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = db.execute(q.order_by(models.OzonBuyout.id.desc())
                      .offset(offset).limit(limit)).scalars().all()
    out = [{
        "posting_number": r.posting_number,
        "offer_id": r.offer_id,
        "name": r.name,
        "sku": r.sku,
        "quantity": r.quantity,
        "seller_price": float(r.seller_price or 0),
        "buyout_price": float(r.buyout_price or 0),
        "amount": float(r.amount or 0),
        "deduction_by_category_percent": float(r.deduction_by_category_percent or 0),
        "vat_percent": r.vat_percent,
    } for r in rows]
    return {"rows": out, "total": int(total)}
