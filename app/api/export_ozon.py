# -*- coding: utf-8 -*-
"""Excel-выгрузки Ozon: /api/export/ozon/*."""
from app.api._common import *  # noqa: F401,F403
from app.api._common import (
    _ozon_placement_agg,
    _parse_window400,
)

router = APIRouter()



@router.get("/export/ozon/accrual-rows")
def export_ozon_accrual_rows(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    bucket: Optional[str] = None,
    article_like: Optional[str] = None,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
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
    df, ru = excel_io.project_export(df, OZON_ACCRUAL_RU_COLUMNS, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Начисления")
    fname = f"ozon_accrual_{from_}_{to_}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export/ozon/cashflow-rows")
def export_ozon_cashflow_rows(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
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
    df, ru = excel_io.project_export(df, OZON_CASHFLOW_RU_COLUMNS, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Движение средств")
    fname = f"ozon_cashflow_{from_}_{to_}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )

OZON_DETAIL_SUMMARY_RU_COLUMNS = export_cols("OZON_DETAIL_SUMMARY_RU_COLUMNS")


@router.get("/export/ozon/placement-summary")
def export_ozon_placement_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    by_size: int = 0,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)
    out = _ozon_placement_agg(db, from_, to_, article_like, by_size=bool(by_size))
    df = pd.DataFrame(out) if out else pd.DataFrame()
    df, ru = excel_io.project_export(df, OZON_PLACEMENT_SUMMARY_RU_COLUMNS, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Размещение по артикулам")
    fname = f"ozon_placement_{from_}_{to_}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export/ozon/placement-rows")
def export_ozon_placement_rows(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)
    q = select(models.OzonPlacement).where(models.OzonPlacement.date.isnot(None))
    if from_:
        q = q.where(models.OzonPlacement.date >= from_)
    if to_:
        q = q.where(models.OzonPlacement.date <= to_)
    if article_like:
        q = q.where(common_service.like_col(models.OzonPlacement.offer_id, article_like)
                    | common_service.like_col(models.OzonPlacement.base_article, article_like))
    rows = db.execute(
        q.order_by(models.OzonPlacement.date.asc(), models.OzonPlacement.offer_id.asc())
    ).scalars().all()
    out = [{
        "date": r.date.isoformat() if r.date else "",
        "sku": r.sku, "offer_id": r.offer_id, "name": r.offer_id,
        "warehouse": r.warehouse, "paid_quantity": r.paid_quantity,
        "paid_volume": float(r.paid_volume or 0),
        "storage": float(r.storage or 0),
    } for r in rows]
    df = pd.DataFrame(out) if out else pd.DataFrame()
    df, ru = excel_io.project_export(df, OZON_PLACEMENT_RU_COLUMNS, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Размещение по дням")
    fname = f"ozon_placement_rows_{from_}_{to_}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export/ozon/detail-summary")
def export_ozon_detail_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    by_size: int = 0,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    from_, to_ = _parse_window400(date_from, date_to)
    df = sync_service.oz_detail_summary_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like,
        by_size=bool(by_size),
    )
    df, ru = excel_io.project_export(df, OZON_DETAIL_SUMMARY_RU_COLUMNS, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Детализация по артикулам")
    fname = f"ozon_detail_summary_{from_}_{to_}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export/ozon/detail-rows")
def export_ozon_detail_rows(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    limit: int = 5000,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Экспорт сырых строк «Детализации реализаций» Ozon в Excel."""
    from_, to_ = _parse_window400(date_from, date_to)
    q = (
        select(models.OzonDetailRow)
        .where(models.OzonDetailRow.date.isnot(None))
        .order_by(models.OzonDetailRow.date, models.OzonDetailRow.id)
        .limit(limit)
    )
    if from_:
        q = q.where(models.OzonDetailRow.date >= from_)
    if to_:
        q = q.where(models.OzonDetailRow.date <= to_)
    if article_like:
        q = q.where(common_service.like_col(models.OzonDetailRow.offer_id, article_like)
                    | common_service.like_col(models.OzonDetailRow.base_article, article_like))
    rows = db.execute(q).scalars().all()
    recs = [{
        "date": r.date.isoformat() if r.date else "",
        "posting_number": r.posting_number, "offer_id": r.offer_id,
        "name": r.name, "sku": r.sku, "barcode": r.barcode,
        "quantity": r.quantity, "seller_price": float(r.seller_price or 0),
        "amount": float(r.amount or 0),
        "commission_ratio": float(r.commission_ratio or 0),
        "commission": float(r.commission or 0),
        "standard_fee": float(r.standard_fee or 0),
        "income": float(r.income or 0),
        "return_qty": r.return_qty, "return_total": float(r.return_total or 0),
        "source": r.source,
    } for r in rows]
    df = pd.DataFrame(recs, columns=list(OZON_DETAIL_RU_COLUMNS.keys()))
    df, ru = excel_io.project_export(df, OZON_DETAIL_RU_COLUMNS, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Строки детализации")
    fname = f"ozon_detail_rows_{from_}_{to_}.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/export/ozon/buyout-rows")
def export_ozon_buyout_rows(
    article_like: Optional[str] = None,
    limit: int = 5000,
    cols: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Экспорт сырых строк «Выкупов» Ozon в Excel."""
    q = select(models.OzonBuyout).order_by(models.OzonBuyout.id).limit(limit)
    if article_like:
        q = q.where(common_service.like_col(models.OzonBuyout.offer_id, article_like)
                    | common_service.like_col(models.OzonBuyout.base_article, article_like))
    rows = db.execute(q).scalars().all()
    recs = [{
        "posting_number": r.posting_number, "offer_id": r.offer_id,
        "name": r.name, "sku": r.sku, "quantity": r.quantity,
        "seller_price": float(r.seller_price or 0),
        "buyout_price": float(r.buyout_price or 0),
        "amount": float(r.amount or 0),
        "deduction_by_category_percent": float(r.deduction_by_category_percent or 0),
        "vat_percent": r.vat_percent,
    } for r in rows]
    df = pd.DataFrame(recs, columns=list(OZON_BUYOUT_RU_COLUMNS.keys()))
    df, ru = excel_io.project_export(df, OZON_BUYOUT_RU_COLUMNS, cols)
    df = df.rename(columns=ru)
    buf = excel_io.df_to_excel_stream(df, sheet_name="Выкупы")
    fname = "ozon_buyout_rows.xlsx"
    return StreamingResponse(
        buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
