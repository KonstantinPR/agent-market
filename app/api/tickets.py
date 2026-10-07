# -*- coding: utf-8 -*-
"""Тикеты и акции: /api/tickets/*, /api/promo/*."""
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
    """Одноразовое обновление акций WB (Календарь акций) в wb_promotions."""
    try:
        res = refresh_service.pull_wb_promotions(db, write_db=True)
    except Exception as e:  # noqa: BLE001
        refresh_service.wb_error(e)
    return {
        "rows": res["rows"], "db_rows": res["db_rows"],
        "count": res["count"], "window": res["window"],
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