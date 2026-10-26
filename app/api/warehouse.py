# -*- coding: utf-8 -*-
"""«Наш склад»: /api/warehouse/*."""
from app.api._common import *  # noqa: F401,F403
from sqlalchemy import or_

from app.services import warehouse_paths as warehouse_paths_service

router = APIRouter()


def _parse_cabinet_ids(raw: Optional[str]) -> list[int]:
    """«3,5» → [3, 5]. Пусто/мусор = без фильтра (все связки)."""
    return [int(x) for x in (raw or "").replace(" ", "").split(",") if x.isdigit()]



# ── «Наш склад»: контрагенты, приход/отгрузка, остатки ────────────────────────

@router.get("/warehouse/counterparties")
def api_cp_list(db: Session = Depends(get_db)):
    rows = [
        {
            "id": cp.id, "name": cp.name, "inn": cp.inn, "ctype": cp.ctype,
            "phone": cp.phone, "note": cp.note,
        }
        for cp in db.query(models.Counterparty).order_by(models.Counterparty.name).all()
    ]
    return {"rows": rows, "count": len(rows), "labels": warehouse_service.CP_TYPE_LABELS}


@router.post("/warehouse/counterparties")
def api_cp_save(
    id: Optional[int] = None,
    name: str = Body(...),
    inn: str = Body(""),
    ctype: str = Body("other"),
    phone: str = Body(""),
    note: str = Body(""),
    db: Session = Depends(get_db),
):
    if id:
        cp = db.get(models.Counterparty, id)
        if not cp:
            raise HTTPException(404, "Контрагент не найден")
    else:
        existing = db.query(models.Counterparty).filter(models.Counterparty.name == name).first()
        if existing:
            cp = existing
        else:
            cp = models.Counterparty(name=name)
            db.add(cp)
    cp.name = name
    cp.inn = inn
    cp.ctype = ctype
    cp.phone = phone
    cp.note = note
    db.commit()
    return {"id": cp.id, "ok": True}


@router.delete("/warehouse/counterparties/{cp_id}")
def api_cp_delete(cp_id: int, db: Session = Depends(get_db)):
    cp = db.get(models.Counterparty, cp_id)
    if not cp:
        raise HTTPException(404)
    db.delete(cp)
    db.commit()
    return {"ok": True}


@router.post("/warehouse/import/counterparties")
async def import_cp(file: UploadFile = File(...), db: Session = Depends(get_db)):
    df = excel_io.read_excel_bytes(await file.read(), sheet=0)
    r = warehouse_service.import_counterparties(db, df)
    return {"filename": file.filename, **r}


@router.post("/warehouse/import/docs")
async def import_docs(
    type: str = "receipt",
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    if type not in ("receipt", "shipment"):
        raise HTTPException(400, f"Тип {type!r} не поддерживается")
    df = excel_io.read_excel_bytes(await file.read(), sheet=0)
    r = warehouse_service.import_docs(db, df, doc_type=type)
    return {"filename": file.filename, "type": type, **r}


@router.get("/warehouse/docs")
def api_docs_list(
    type: str = "receipt",
    from_: Optional[str] = None,
    to: Optional[str] = None,
    cabinet_ids: Optional[str] = None,
    limit: int = 200,
    db: Session = Depends(get_db),
):
    if type not in ("receipt", "shipment"):
        raise HTTPException(400, "type должен быть receipt или shipment")
    q = db.query(models.WarehouseDoc).filter(models.WarehouseDoc.doc_type == type)
    ids = _parse_cabinet_ids(cabinet_ids)
    if ids:
        # Документы без связки (ручной импорт) не скрываем: они не принадлежат
        # другой связке, иначе такие отправки молча выпадали бы из таблицы.
        q = q.filter(or_(models.WarehouseDoc.cabinet_id.in_(ids),
                         models.WarehouseDoc.cabinet_id.is_(None)))
    if from_:
        q = q.filter(models.WarehouseDoc.doc_date >= from_)
    if to:
        q = q.filter(models.WarehouseDoc.doc_date <= to)
    docs = q.order_by(models.WarehouseDoc.doc_date.desc()).limit(limit).all()
    cp_ids = {d.counterparty_id for d in docs if d.counterparty_id}
    names = {}
    if cp_ids:
        names = {c.id: c.name for c in db.query(models.Counterparty).filter(models.Counterparty.id.in_(cp_ids)).all()}
    cab_names = warehouse_service.cabinet_labels(db, {d.cabinet_id for d in docs if d.cabinet_id})
    rows = []
    for d in docs:
        folder = ""
        if d.source == "paths" and ":" in (d.source_ref or ""):
            folder = d.source_ref.split(":", 1)[1]
        rows.append({
            "id": d.id, "doc_num": d.doc_num, "date": d.doc_date.isoformat() if d.doc_date else "",
            "counterparty": names.get(d.counterparty_id, ""), "total": float(d.total or 0),
            "items_count": len(d.items), "source": d.source,
            "cabinet": cab_names.get(d.cabinet_id, ""), "note": d.note or "",
            "folder": folder,
        })
    return {"rows": rows, "count": len(rows), "type": type}


@router.get("/warehouse/shipment-cabinets")
def api_shipment_cabinets(db: Session = Depends(get_db)):
    """Связки, у которых есть отправки, + количество — для чекбокс-селектора."""
    return {"rows": warehouse_service.shipment_cabinet_counts(db)}


@router.get("/warehouse/shipment-items")
def api_shipment_items(
    cabinet_ids: Optional[str] = None,
    article_like: str = "",
    group: str = "rows",
    limit: int = 5000,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Товарный разрез отправок.

    ``group="rows"`` — плоский список «отправка × товар» (как в исходных
    файлах); ``"sku"`` — сводка по артикулу+размеру; ``"article"`` — сводка по
    артикулу. Фильтр ``cabinet_ids`` — id связок через запятую.
    """
    if group not in ("rows", "sku", "article"):
        raise HTTPException(400, "group должен быть rows, sku или article")
    ids = _parse_cabinet_ids(cabinet_ids)
    if group == "rows":
        all_rows = warehouse_service.shipment_items(
            db, cabinet_ids=ids, article_like=article_like or "")
    else:
        all_rows = warehouse_service.shipment_summary(
            db, cabinet_ids=ids, article_like=article_like or "", group=group)
    total = len(all_rows)
    start = max(0, int(offset or 0))
    lim = int(limit or 0)
    rows = all_rows[start:start + lim] if lim > 0 else all_rows[start:]
    # Итоги считаем по всей выборке, а не по отданной порции: таблица показывает
    # «ИТОГО» в шапке, и при обрезке до 5000 строк оно не должно «врать».
    qty = sum(float(r.get("quantity") or 0) for r in all_rows)
    amount = sum(float(r.get("amount") or 0) for r in all_rows)
    totals = {
        "quantity": qty,
        "amount": amount,
        "price": (amount / qty) if qty else 0.0,
        "rows": total,
        "docs": len({r["doc_id"] for r in all_rows}) if group == "rows" else None,
    }
    return {
        "rows": rows, "total": total, "count": len(rows), "group": group,
        "offset": start, "limit": lim, "truncated": start + len(rows) < total,
        "totals": totals,
    }


@router.get("/warehouse/docs/{doc_id}/items")
def api_doc_items(doc_id: int, db: Session = Depends(get_db)):
    d = db.get(models.WarehouseDoc, doc_id)
    if not d:
        raise HTTPException(404)
    rows = [
        {"article": it.article, "name": it.name, "barcode": it.barcode, "size": it.size,
         "quantity": float(it.quantity), "price": float(it.price), "amount": float(it.amount)}
        for it in d.items
    ]
    return {"doc_id": doc_id, "doc_num": d.doc_num, "date": d.doc_date.isoformat() if d.doc_date else "", "type": d.doc_type, "rows": rows}


@router.post("/warehouse/docs")
def api_doc_create(
    type: str = Body("receipt"),
    doc_num: str = Body(""),
    doc_date: str = Body("..."),
    counterparty_id: Optional[int] = Body(None),
    note: str = Body(""),
    items: List[dict] = Body(...),
    db: Session = Depends(get_db),
):
    from datetime import datetime as _dt
    try:
        dt = _dt.fromisoformat(doc_date).date()
    except Exception:
        raise HTTPException(400, "Некорректная дата")
    if not items:
        raise HTTPException(400, "Строки документа пусты")
    r = warehouse_service.create_doc(db, doc_type=type, doc_num=doc_num, doc_date=dt,
                                     counterparty_id=counterparty_id, note=note, items=items)
    return r


@router.delete("/warehouse/docs/{doc_id}")
def api_doc_delete(doc_id: int, db: Session = Depends(get_db)):
    try:
        warehouse_service.delete_doc(db, doc_id)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@router.get("/warehouse/paths/settings")
def api_wh_paths_settings(cabinet_id: int, db: Session = Depends(get_db)):
    """Настройки импорта из папок + список дефолтных путей для селектора.

    ``roots`` — подсказка UI (оба варианта пути и их доступность), в JSON
    настроек не сохраняется. Отдаём ``settings_payload``, а не «сырые»
    настройки, чтобы фронтенд мог построить выбор пути без знания кодов связок.
    """
    try:
        return warehouse_paths_service.settings_payload(db, cabinet_id)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.put("/warehouse/paths/settings")
def api_wh_paths_settings_put(payload: dict = Body(...), db: Session = Depends(get_db)):
    """Сохранить настройки импорта для связки.

    Сервис сам отбрасывает неизвестные ключи (``SETTING_KEYS``), поэтому
    присланный из UI список ``roots`` в базу не попадёт.
    """
    cabinet_id = payload.get("cabinet_id")
    if cabinet_id is None:
        raise HTTPException(400, "cabinet_id обязателен")
    try:
        return warehouse_paths_service.save_settings(db, int(cabinet_id), payload.get("settings") or {})
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/warehouse/paths/scan")
def api_wh_paths_scan(payload: dict = Body(...), db: Session = Depends(get_db)):
    """Проверить папки в корне связки (без чтения Excel) и вернуть статусы."""
    cabinet_id = payload.get("cabinet_id")
    if cabinet_id is None:
        raise HTTPException(400, "cabinet_id обязателен")
    try:
        return warehouse_paths_service.scan_folders(db, int(cabinet_id))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/warehouse/paths/import")
def api_wh_paths_import(payload: dict = Body(...), db: Session = Depends(get_db)):
    """Импортировать одну папку отправки.

    ``mode="new"`` — только если документа ещё нет; ``"update"`` — перезаписать
    существующий (тот же ``source_ref``); ``dry_run`` — разбор без записи в БД.
    """
    cabinet_id = payload.get("cabinet_id")
    folder = payload.get("folder") or ""
    if cabinet_id is None or not folder:
        raise HTTPException(400, "cabinet_id и folder обязательны")
    mode = payload.get("mode") or "new"
    dry_run = bool(payload.get("dry_run"))
    try:
        return warehouse_paths_service.import_folder(db, int(cabinet_id), folder, mode=mode, dry_run=dry_run)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/warehouse/stock")
def api_stock(article_like: Optional[str] = None, db: Session = Depends(get_db)):
    rows = warehouse_service.stock_view(db, article_like=article_like or "")
    return {"rows": rows, "count": len(rows)}


@router.get("/warehouse/turnover")
def api_turnover(db: Session = Depends(get_db)):
    return {"rows": warehouse_service.turnover_view(db)}


@router.get("/warehouse/export/{kind}")
def api_wh_export(kind: str, type: str = "receipt", cabinet_ids: Optional[str] = None,
                  article_like: str = "", group: str = "rows",
                  db: Session = Depends(get_db)):
    try:
        buf = warehouse_service.file_for(
            kind, db, doc_type=type, cabinet_ids=_parse_cabinet_ids(cabinet_ids),
            article_like=article_like or "", group=group)
    except ValueError as e:
        raise HTTPException(400, str(e))
    filename = f"{kind}_{type}.xlsx" if "docs" in kind else f"{kind}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/warehouse/todisk")
async def api_wh_todisk(kind: str = "docs", type: str = "receipt",
                        cabinet_ids: Optional[str] = None, article_like: str = "",
                        group: str = "rows", db: Session = Depends(get_db)):
    """Сформировать Excel и загрузить на Яндекс.Диск в папку /agent_market/Наш склад/<kind>."""
    if kind == "docs" and type not in ("receipt", "shipment"):
        raise HTTPException(400, "type должен быть receipt или shipment")
    try:
        token = yandex_service.require_token()
        buf = warehouse_service.file_for(
            kind, db, doc_type=type, cabinet_ids=_parse_cabinet_ids(cabinet_ids),
            article_like=article_like or "", group=group)
        filename = f"{kind}_{type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx" if kind == "docs" else f"{kind}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        folder = "/agent_market/Наш склад"
        r = yandex_service.upload_bytes(token, folder, filename, buf.read())
        return {"ok": True, "path": r.get("path", f"{folder}/{filename}")}
    except yandex_service.YandexDiskError as e:
        raise HTTPException(502, str(e))


@router.post("/warehouse/fromdisk")
async def api_wh_fromdisk(
    type: str = "receipt",
    db: Session = Depends(get_db),
):
    """Импорт всех .xlsx из папки «Наш склад/Приход или Отгрузка» на Диске."""
    folder_map = {"receipt": "/agent_market/Наш склад/Приход", "shipment": "/agent_market/Наш склад/Отгрузка",
                  "counterparties": "/agent_market/Наш склад/Контрагенты"}
    folder = folder_map.get(type)
    if not folder:
        raise HTTPException(400, f"type {type!r} не поддерживается")
    results = []
    try:
        token = yandex_service.require_token()
        files = yandex_service.list_files(token, folder).get("items", [])
        for f in files:
            if not f["name"].endswith(".xlsx"):
                continue
            data = yandex_service.download_bytes(token, f["path"])
            df = excel_io.read_excel_bytes(data, sheet=0)
            if type == "counterparties":
                r = warehouse_service.import_counterparties(db, df)
            else:
                r = warehouse_service.import_docs(db, df, doc_type=type, source="disk")
            results.append({"file": f["name"], **r})
    except yandex_service.YandexDiskError as e:
        raise HTTPException(502, str(e))
    return {"results": results}
