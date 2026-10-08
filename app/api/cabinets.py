# -*- coding: utf-8 -*-
"""Личные кабинеты: /api/cabinets*, /api/cabinet/*, /api/refresh-all."""
from fastapi import Body, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.api._common import *  # noqa: F401,F403
from app.services import cabinets as cabinet_service
from app.services import refresh as refresh_service

router = APIRouter()


def _current_user(db: Session):
    return cabinet_service.seed_user(db)


@router.get("/cabinets")
def api_cabinets(db: Session = Depends(get_db)):
    """Список кабинетов текущего пользователя (с признаком активного)."""
    user = _current_user(db)
    active = cabinet_service.get_active()
    active_id = active.id if active is not None else None
    rows = cabinet_service.list_cabinets(db, user)
    cabs = []
    for c in rows:
        info = cabinet_service.info_by_id(db, c.id)
        if info is not None:
            cabs.append(info.to_dict(active=(info.id == active_id)))
    return {
        "cabinets": cabs,
        "current": active.to_dict(active=True) if active is not None else None,
        "user": {"username": user.username, "display_name": user.display_name},
    }


@router.get("/cabinet/current")
def api_cabinet_current(request: Request, db: Session = Depends(get_db)):
    """Активный кабинет (по куке agent_cabinet). None — «как раньше» (public)."""
    user = _current_user(db)
    info = cabinet_service.resolve_active(
        db, request.cookies.get(cabinet_service.CABINET_COOKIE), user)
    return info.to_dict(active=True) if info is not None else None


@router.post("/cabinet/select")
def api_cabinet_select(payload: dict = Body(default={}), response: Response = None,
                       db: Session = Depends(get_db)):
    """Выбирает активный кабинет (кука agent_cabinet + last_cabinet_id)."""
    cab_id = payload.get("id")
    try:
        cab_id = int(cab_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Не передан id кабинета")
    user = _current_user(db)
    row = cabinet_service.cabinet_by_id(db, cab_id, user)
    if row is None:
        raise HTTPException(status_code=404, detail="Кабинет не найден")
    user.last_cabinet_id = cab_id
    db.commit()
    if response is not None:
        response.set_cookie(
            cabinet_service.CABINET_COOKIE, str(cab_id),
            max_age=60 * 60 * 24 * 365, samesite="lax", path="/",
        )
    return cabinet_service.info_by_id(db, cab_id).to_dict(active=True)


@router.post("/refresh-all")
def api_refresh_all(payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """«Обновить всё»: по очереди запускает обновление всех кабинетов
    (по маркетплейсам кабинета). Возвращает список запущенных job_id."""
    user = _current_user(db)
    include_detail = bool(payload.get("include_detail"))
    jobs = []
    for c in cabinet_service.list_cabinets(db, user):
        if not c.enabled:
            continue
        info = cabinet_service.info_by_id(db, c.id)
        for m in info.marketplaces:
            res = refresh_service.start_refresh(
                m, include_detail=include_detail, cab_id=c.id)
            if res.get("job_id"):
                jobs.append(res)
    return {"jobs": jobs, "count": len(jobs)}