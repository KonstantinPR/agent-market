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
from fastapi import Request

from app.services import cabinets as cabinet_service

router = APIRouter()

# oid продавца WB «вшит» в имя файла отчёта как `_<oid>` (напр. `..._29077.zip`).
_DETAIL_OID_RE = re.compile(r"_(\d{5,})")

# Номер отчёта WB тоже «вшит» в имя файла: «...№841044697_29077.zip». В самой
# Excel-выгрузке ЛК «Детализация продаж» колонки с номером отчёта НЕТ, поэтому
# при ручной загрузке номер берём из имени файла/записи архива, иначе строки
# попадают в базу с пустым report_id и проверка пропусков их «не видит».
_DETAIL_REPORT_NO_RE = re.compile(r"№\s*(\d{4,})")


def _report_oids(*names) -> set:
    """Множество чисел вида `_<digits>` из имён файлов отчёта WB.

    Сравнивается с известными oid связок, чтобы не путать годы/даты в
    произвольных именах с идентификатором продавца.
    """
    out = set()
    for name in names:
        if not name:
            continue
        for m in _DETAIL_OID_RE.finditer(name):
            out.add(int(m.group(1)))
    return out


def _report_number(*names) -> str:
    """Номер отчёта WB из имени файла/записи архива («№<digits>»)."""
    for name in names:
        if not name:
            continue
        m = _DETAIL_REPORT_NO_RE.search(name)
        if m:
            return m.group(1)
    return ""


def _stamp_report_id(df, report_id: str):
    """Проставляет номер отчёта в выгрузку, если он ещё не задан колонкой.

    Лист ЛК «Детализация продаж» не содержит номера отчёта — его приходится
    брать из имени файла. Если колонка с номером уже есть (finance-API/наш
    экспорт), ничего не перезаписываем.
    """
    if not report_id or df is None or df.empty:
        return df
    if any(c in df.columns for c in ("reportId", "report_id", "Номер отчёта")):
        return df
    df = df.copy()
    df["reportId"] = report_id
    return df



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


@router.get("/wb/detail-gaps")
def api_wb_detail_gaps(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    period: str = "weekly",
    db: Session = Depends(get_db),
):
    """Пропущенные (недогруженные) отчёты WB о продажах.

    Запрашивает у finance-API список сформированных отчётов за окно и вычитает
    report_id, уже загруженные в базу. Возвращает первый/последний период и
    перечень пропущенных отчётов, чтобы их можно было догрузить.
    """
    if period not in ("weekly", "daily"):
        raise HTTPException(status_code=400, detail="period: weekly|daily")
    try:
        return refresh_service.wb_detail_gaps(db, date_from=date_from,
                                              date_to=date_to, period=period)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)


@router.post("/wb/detail-upload")
async def wb_detail_upload(
    request: Request,
    files: List[UploadFile] = File(...),
    write_db: int = 1,
    db: Session = Depends(get_db),
):
    """Ручная загрузка детализации продаж WB из Excel/zip (без finance-api, без лимита).

    Принимает файлы WB с русскими заголовками («Детализация продаж»), см.
    DETAIL_UPLOAD_RENAME. Записывает в продажи source='detail'.

    Проверяет владельца: в имени файла WB (новый формат) есть `_<oid>`. Если
    oid принадлежит другой известной связке — загрузка отклоняется.
    """
    user = cabinet_service.seed_user(db)
    active = cabinet_service.resolve_active(
        db, request.cookies.get(cabinet_service.CABINET_COOKIE), user)
    expected_oid = cabinet_service.wb_oid(active) if active is not None else None
    oid_map = None  # {oid: CabinetInfo} — ленивая загрузка
    wrong_owner = []  # [(файл, oid)] чужой связки
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
                    oids = _report_oids(name) | _report_oids(
                        *(i.filename for i in zf.infolist()))
                    if oids:
                        if oid_map is None:
                            oid_map = cabinet_service.wb_oids_by_cabinet(db, user)
                        foreign = (oids & set(oid_map)) - {expected_oid}
                        if foreign:
                            for o in sorted(foreign):
                                wrong_owner.append((name, o))
                            fileinfo.append({"name": name, "rows": 0, "ok": False,
                                             "error": "детализация другой связки"})
                            continue
                    sub = 0
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        if not info.filename.lower().endswith((".xlsx", ".xlsm")):
                            continue
                        try:
                            d = excel_io.read_excel_bytes(zf.read(info))
                            d = _stamp_report_id(
                                d, _report_number(info.filename) or _report_number(name))
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
        oids = _report_oids(name)
        if oids:
            if oid_map is None:
                oid_map = cabinet_service.wb_oids_by_cabinet(db, user)
            foreign = (oids & set(oid_map)) - {expected_oid}
            if foreign:
                for o in sorted(foreign):
                    wrong_owner.append((name, o))
                fileinfo.append({"name": name, "rows": 0, "ok": False,
                                 "error": "детализация другой связки"})
                continue
        try:
            d = excel_io.read_excel_bytes(data)
            d = _stamp_report_id(d, _report_number(name))
            frames.append(d)
            total_rows += len(d)
            fileinfo.append({"name": name, "rows": len(d), "ok": True})
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            fileinfo.append({"name": name, "rows": 0, "ok": False, "error": str(e)})
    if wrong_owner:
        mp_names = cabinet_service.marketplace_names(db)
        chunks = []
        for fname, oid in wrong_owner[:5]:
            info = oid_map.get(oid) if oid_map else None
            who = info.owner(mp_names) if info is not None else f"oid {oid}"
            chunks.append(f"«{fname}» — «{who}»")
        act = active.owner(mp_names) if active is not None else ""
        detail = "Загрузка отменена: это детализация другой связки (" + "; ".join(chunks) + ")."
        if act:
            detail += f" Активна связка «{act}»."
        detail += " Переключите кабинет и повторите."
        raise HTTPException(status_code=400, detail=detail)
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
            n = sync_service.rebuild_sales_from_detail(db, source="v5")
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
