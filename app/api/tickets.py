# -*- coding: utf-8 -*-
"""Тикеты и акции: /api/tickets/*, /api/promo/*."""
import json

from app.api._common import *  # noqa: F401,F403
from app.api._common import (
    _promo_row,
    _ticket_or_error,
)

router = APIRouter()



@router.get("/tickets")
def api_tickets():
    """Очередь тикетов из TICKETS.md."""
    return tickets_service.load()


@router.post("/tickets")
def api_tickets_create(body: dict = Body(...)):
    """Создаёт тикет в «Открытые»."""
    title = str(body.get("title", "")).strip()
    if not title:
        raise HTTPException(status_code=400, detail="Заголовок тикета обязателен")
    priority = str(body.get("priority", "medium")).strip().lower()
    return _ticket_or_error(
        tickets_service.create, title=title,
        body=str(body.get("body", "")), priority=priority,
    )


@router.post("/tickets/{tid}/start")
def api_ticket_start(tid: str):
    return _ticket_or_error(tickets_service.start, tid)


@router.post("/tickets/{tid}/block")
def api_ticket_block(tid: str):
    return _ticket_or_error(tickets_service.block, tid)


@router.post("/tickets/{tid}/unblock")
def api_ticket_unblock(tid: str):
    return _ticket_or_error(tickets_service.unblock, tid)


@router.post("/tickets/{tid}/close")
def api_ticket_close(tid: str, body: dict = Body(default={})):
    return _ticket_or_error(
        tickets_service.close, tid, commit=str(body.get("commit", "")),
    )


@router.post("/tickets/{tid}/decline")
def api_ticket_decline(tid: str):
    return _ticket_or_error(tickets_service.decline, tid)


@router.post("/tickets/{tid}/reopen")
def api_ticket_reopen(tid: str):
    return _ticket_or_error(tickets_service.reopen, tid)


@router.post("/promo/refresh")
def promo_refresh(db: Session = Depends(get_db)):
    """Обновление акций WB (wb_promotions) и их номенклатуры (planDiscount).

    Календарь акций → wb_promotions; для активных regular-акций дополнительно
    грузится номенклатура (участники + кандидаты, inAction=true/false) →
    wb_promo_nomenclatures — источник потолока скидки автопилота (R19/T-41);
    в ответе поле promo_nom_rows. У автоакций endpoint nomenclatures отвечает
    422, их пропускаем.
    """
    try:
        res = refresh_service.pull_wb_promotions(db, write_db=True)
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    return {
        "rows": res["rows"], "db_rows": res["db_rows"],
        "count": res["count"], "window": res["window"],
        "promo_nom_rows": res.get("promo_nom_rows", 0),
    }


@router.get("/promo/list")
def promo_list(db: Session = Depends(get_db)):
    """Сохранённые акции WB: актуальные и ближайшие (±90 дней), свежие сверху."""
    rows = db.scalars(
        select(models.Promotion)
        .order_by(models.Promotion.starts_at.desc())
        .limit(200)
    ).all()
    return {"promotions": [_promo_row(p) for p in rows]}


@router.post("/promo/limits/import")
async def promo_limits_import(file: UploadFile = File(...), promo_id: int = 0,
                              column_map: str = "",
                              db: Session = Depends(get_db)):
    """Импорт порогов участия «до N ₽» из Excel личного кабинета WB (T-76/R20).

    Публичный API автоакций порогов не отдаёт (422), поэтому единственный
    источник — файл «Цены и скидки» → акция → «Обновить через Excel». Строки
    складываются в `wb_promo_limits` (upsert по promo_id+nm_id), устаревшие
    строки той же акции удаляются. ``promo_id`` = 0 — вычислить по имени файла
    (номер акции или её название), иначе «без привязки».

    ``column_map`` — необязательный JSON вида ``{"nm": "Артикул WB",
    "plan": "Плановая цена"}`` для нестандартной выгрузки.
    """
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Файл пуст")
    cmap = None
    if str(column_map or "").strip():
        try:
            cmap = json.loads(column_map)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="column_map должен быть JSON-объектом {роль: колонка}")
        if not isinstance(cmap, dict):
            raise HTTPException(
                status_code=400,
                detail="column_map должен быть JSON-объектом {роль: колонка}")
    try:
        meta = promo_limits_service.import_limits(
            db, data, file.filename or "", promo_id=promo_id, column_map=cmap)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, **meta,
            "summary": promo_limits_service.summary(db)}


@router.get("/promo/limits")
def promo_limits_list(db: Session = Depends(get_db)):
    """Сводка загруженных порогов акций: акции, товары, даты, файл."""
    return promo_limits_service.summary(db)


@router.delete("/promo/limits")
def promo_limits_clear(db: Session = Depends(get_db)):
    """Удаляет все загруженные пороги акций."""
    return {"deleted": promo_limits_service.clear(db)}


@router.delete("/promo/limits/{promo_id}")
def promo_limits_clear_promo(promo_id: int, db: Session = Depends(get_db)):
    """Удаляет пороги одной акции."""
    return {"deleted": promo_limits_service.clear(db, promo_id=promo_id)}