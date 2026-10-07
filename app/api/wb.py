# -*- coding: utf-8 -*-
"""WB API-вьюхи и загрузки: /api/wb/*."""
from app.api._common import *  # noqa: F401,F403
from app.api._common import (
    _MAX_CARDS_UPLOAD,
    _df_totals,
    _parse_window400,
    _pull_json,
    _xlsx_response,
)

router = APIRouter()



@router.post("/wb/cards")
def wb_cards(write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_wb_cards(db, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    if not int(excel):
        return _pull_json(res)
    return _xlsx_response(refresh_service.expand_wb_card_export(res["df"]), "wb_cards.xlsx", res["count"])


@router.post("/wb/stock")
def wb_stock(write_db: int = 1, by_size: int = 1, excel: int = 1,
             db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_wb_stock(db, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        wb_error(e)
    if not int(excel):
        return _pull_json(res)
    df = res["df"]
    if not df.empty and not int(by_size):
        df = df.groupby(["article", "warehouse"], as_index=False).agg({
            "quantity": "sum", "quantity_full": "sum", "in_way": "sum", "date": "first",
        })
    return _xlsx_response(df, "wb_stock.xlsx", len(df))


@router.post("/wb/funnel")
def wb_funnel(date_from: Optional[str] = None, date_to: Optional[str] = None,
              write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    from_, to_ = _parse_window400(date_from, date_to)
    try:
        res = refresh_service.pull_wb_funnel(db, from_, to_, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    if not int(excel):
        return _pull_json(res)
    return _xlsx_response(res["df"], f"wb_funnel_{from_}_{to_}.xlsx", res["count"])


@router.post("/wb/prices")
def wb_prices(write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_wb_prices(db, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    if not int(excel):
        return _pull_json(res)
    return _xlsx_response(res["df"], "wb_prices.xlsx", res["count"])


@router.post("/wb/storage")
def wb_storage(days: int = 7, write_db: int = 1, excel: int = 1,
               db: Session = Depends(get_db)):
    try:
        res = refresh_service.pull_wb_storage(db, days, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    if not int(excel):
        return _pull_json(res)
    return _xlsx_response(res["df"], "wb_storage.xlsx", res["count"])


@router.post("/wb/sales")
def wb_sales(date_from: Optional[str] = None, date_to: Optional[str] = None,
             write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    from_, to_ = _parse_window400(date_from, date_to)
    try:
        res = refresh_service.pull_wb_sales(db, from_, to_, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    if not int(excel):
        return _pull_json(res)
    df = res["df"]
    if "sa_name" in df.columns:
        export = df.rename(columns=V5_RU_COLUMNS)
    elif "supplierArticle" in df.columns:
        export = df.rename(columns=SALES_RU_COLUMNS)
    else:
        export = df
    return _xlsx_response(export, f"wb_sales_{from_}_{to_}.xlsx", res["count"])


@router.post("/wb/detail")
def wb_detail(date_from: Optional[str] = None, date_to: Optional[str] = None,
              write_db: int = 1, excel: int = 1, db: Session = Depends(get_db)):
    from_, to_ = _parse_window400(date_from, date_to)
    try:
        res = refresh_service.pull_wb_detail(db, from_, to_, write_db=bool(write_db))
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    if not int(excel):
        return _pull_json(res)
    df = res["df"]
    export = df.rename(columns=DETAIL_RU_COLUMNS) if not df.empty else df
    return _xlsx_response(export, f"wb_detail_{from_}_{to_}.xlsx", res["count"])


@router.post("/wb/detail-upload")
async def wb_detail_upload(
    files: List[UploadFile] = File(...),
    write_db: int = 1,
    db: Session = Depends(get_db),
):
    """Ручная загрузка детализации продаж WB из Excel/zip (без finance-api, без лимита).

    Принимает файлы WB с русскими заголовками («Детализация продаж»), см.
    DETAIL_UPLOAD_RENAME. Записывает в продажи source='detail'.
    """
    frames = []
    total_rows = 0
    errors = []
    fileinfo = []
    accum = 0
    for upf in files:
        data = await upf.read()
        accum += len(data)
        if accum > _MAX_CARDS_UPLOAD:
            raise HTTPException(status_code=413, detail="Слишком большой объём файлов (> 100 МБ)")
        name = upf.filename or "file"
        if name.lower().endswith(".zip"):
            try:
                with zipfile.ZipFile(BytesIO(data)) as zf:
                    sub = 0
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        if not info.filename.lower().endswith((".xlsx", ".xlsm")):
                            continue
                        try:
                            d = excel_io.read_excel_bytes(zf.read(info))
                            frames.append(d)
                            sub += len(d)
                        except Exception as e:  # noqa: BLE001
                            errors.append(f"{name}/{info.filename}: {e}")
                    fileinfo.append({"name": name, "rows": sub, "ok": True})
                    total_rows += sub
            except zipfile.BadZipFile as e:
                errors.append(f"{name}: не является zip-архивом ({e})")
                fileinfo.append({"name": name, "rows": 0, "ok": False, "error": str(e)})
            continue
        if not name.lower().endswith((".xlsx", ".xlsm")):
            errors.append(f"{name}: пропущен (не Excel)")
            fileinfo.append({"name": name, "rows": 0, "ok": False, "error": "не Excel"})
            continue
        try:
            d = excel_io.read_excel_bytes(data)
            frames.append(d)
            total_rows += len(d)
            fileinfo.append({"name": name, "rows": len(d), "ok": True})
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            fileinfo.append({"name": name, "rows": 0, "ok": False, "error": str(e)})
    if not frames:
        raise HTTPException(status_code=400,
                            detail="Нет данных для импорта: " + ("; ".join(errors) or "нет файлов"))
    df = pd.concat(frames, ignore_index=True)
    rename = {k: v for k, v in DETAIL_UPLOAD_RENAME.items() if k in df.columns}
    df = df.rename(columns=rename)
    ndf = sync_service.normalize_wb_detail(df, source="excel")
    if ndf is None or ndf.empty:
        raise HTTPException(
            status_code=400,
            detail="Не удалось распознать структуру отчёта: нужны колонки "
                   "«Артикул поставщика/продавца» и «Дата продажи». " + "; ".join(errors[:5]),
        )
    n = 0
    if write_db:
        try:
            sync_service.upsert_wb_detail_rows(db, ndf, source="excel")
            n = sync_service.rebuild_sales_from_detail(db)
        except Exception as e:  # noqa: BLE001
            db.rollback()
            raise HTTPException(status_code=500,
                                detail=f"Ошибка записи детализации: {e}") from e
    sync_service.record_api_pull(db, "wb", "detail", int(total_rows), int(n), "ручной импорт")
    return {
        "imported": int(n),
        "rows": int(total_rows),
        "files": fileinfo,
        "errors": errors,
    }


@router.get("/wb/detail-rows")
def api_wb_detail_rows(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Сырые строки «Детализации продаж» WB (wb_detail_rows) с фильтрами и пагинацией."""
    from_, to_ = _parse_window400(date_from, date_to)
    q = select(models.WbDetailRow).where(models.WbDetailRow.sale_dt.isnot(None))
    if from_:
        q = q.where(models.WbDetailRow.sale_dt >= from_)
    if to_:
        q = q.where(models.WbDetailRow.sale_dt <= to_)
    if article_like:
        q = q.where(common_service.like_col(models.WbDetailRow.article, article_like))
    total = db.execute(select(func.count()).select_from(q.subquery())).scalar_one()
    rows = db.execute(
        q.order_by(models.WbDetailRow.sale_dt.desc(), models.WbDetailRow.id.desc())
        .offset(offset).limit(limit)
    ).scalars().all()
    out = [{
        "date": r.sale_dt.isoformat() if r.sale_dt else "",
        "article": r.article,
        "title": r.title,
        "doc_type": r.doc_type_name,
        "quantity": r.quantity,
        "retail_price": float(r.retail_price or 0),
        "retail_amount": float(r.retail_amount or 0),
        "commission": float(r.ppvz_sales_commission or 0),
        "for_pay": float(r.for_pay or 0),
        "logistics": float(r.delivery_service or 0),
        "storage": float(r.paid_storage or 0),
        "services": float((r.penalty or 0) + (r.deduction or 0) + (r.additional_payment or 0)),
        "office": r.office_name,
        "srid": r.srid,
        "source": r.source,
    } for r in rows]
    return {"rows": out, "total": int(total)}


@router.get("/wb/detail-summary")
def api_wb_detail_summary(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    article_like: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Свод «Детализации продаж» WB по артикулам (сырые деньги операций, без маржи)."""
    from_, to_ = _parse_window400(date_from, date_to)
    df = sync_service.detail_summary_dataframe(
        db, date_from=from_, date_to=to_, article_like=article_like
    )
    return {
        "rows": df.replace({None: ""}).to_dict("records"),
        "count": len(df),
        "totals": _df_totals(df),
    }
