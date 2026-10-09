# -*- coding: utf-8 -*-
"""Личные кабинеты: /api/cabinets*, /api/cabinet/*, /api/refresh-all.

Контракт /api/cabinets с момента введения связок (фирма × маркетплейс):
    {"companies": [{"id", "name", "enabled", "position",
                    "links": [{id, code, name, schema, marketplace, owner, ...}]}],
     "current": <связка>|null,
     "user": {"username", "display_name"}}
"""
from fastapi import Body, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.api._common import *  # noqa: F401,F403
from app.services import cabinets as cabinet_service
from app.services import refresh as refresh_service

router = APIRouter()


def _current_user(db: Session):
    return cabinet_service.seed_user(db)


def _active_info(request: Request, db: Session):
    user = _current_user(db)
    info = cabinet_service.resolve_active(
        db, request.cookies.get(cabinet_service.CABINET_COOKIE), user)
    if info is None:
        raise HTTPException(status_code=404, detail="Активная связка не найдена")
    return info


@router.get("/cabinets")
def api_cabinets(db: Session = Depends(get_db)):
    """Список фирм со связками текущего пользователя (с признаком активной связки)."""
    user = _current_user(db)
    active = cabinet_service.get_active()
    active_id = active.id if active is not None else None
    mp_names = cabinet_service.marketplace_names(db)
    companies = cabinet_service.companies_of(db, user)
    rows = cabinet_service.list_cabinets(db, user)
    links = []
    for c in rows:
        info = cabinet_service.info_by_id(db, c.id)
        if info is not None:
            links.append(info.to_dict(active=(info.id == active_id), mp_names=mp_names))
    grouped = [
        {
            "id": c.id, "name": c.name, "enabled": c.enabled, "position": c.position,
            "links": [lk for lk in links if lk["company_id"] == c.id],
        }
        for c in companies
    ]
    current = active.to_dict(active=True, mp_names=mp_names) if active is not None else None
    return {
        "companies": grouped,
        "current": current,
        "user": {"username": user.username, "display_name": user.display_name},
    }


@router.get("/cabinet/current")
def api_cabinet_current(request: Request, db: Session = Depends(get_db)):
    """Активная связка (по куке agent_cabinet). None — «как раньше» (public)."""
    user = _current_user(db)
    info = cabinet_service.resolve_active(
        db, request.cookies.get(cabinet_service.CABINET_COOKIE), user)
    if info is None:
        return None
    return info.to_dict(active=True, mp_names=cabinet_service.marketplace_names(db))


@router.post("/cabinet/select")
def api_cabinet_select(payload: dict = Body(default={}), response: Response = None,
                       db: Session = Depends(get_db)):
    """Выбирает активную связку (кука agent_cabinet + last_cabinet_id)."""
    cab_id = payload.get("id")
    try:
        cab_id = int(cab_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Не передан id связки")
    user = _current_user(db)
    row = cabinet_service.cabinet_by_id(db, cab_id, user)
    if row is None:
        raise HTTPException(status_code=404, detail="Связка не найдена")
    user.last_cabinet_id = cab_id
    db.commit()
    if response is not None:
        response.set_cookie(
            cabinet_service.CABINET_COOKIE, str(cab_id),
            max_age=60 * 60 * 24 * 365, samesite="lax", path="/",
        )
    info = cabinet_service.info_by_id(db, cab_id)
    if info is None:
        raise HTTPException(status_code=404, detail="Связка не найдена")
    return info.to_dict(active=True, mp_names=cabinet_service.marketplace_names(db))


@router.get("/cabinet/pricing-settings")
def api_pricing_settings_get(request: Request, db: Session = Depends(get_db)):
    """Настройки автопилота цен активной связки (JSON) — {} если не заданы."""
    info = _active_info(request, db)
    return {"settings": cabinet_service.get_pricing_settings(db, info.id)}


@router.put("/cabinet/pricing-settings")
def api_pricing_settings_put(request: Request, payload: dict = Body(default={}),
                             db: Session = Depends(get_db)):
    """Сохраняет настройки автопилота цен за активной связкой."""
    info = _active_info(request, db)
    data = payload.get("settings") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        data = {}
    cabinet_service.save_pricing_settings(db, info.id, data)
    return {"settings": data}


@router.post("/cabinet/pricing-settings/copy")
def api_pricing_settings_copy(request: Request, payload: dict = Body(default={}),
                              db: Session = Depends(get_db)):
    """Копирует настройки автопилота текущей связки в другие связки.

    targets — список id; без него копируется во все связки пользователя.
    """
    info = _active_info(request, db)
    targets = payload.get("targets") if isinstance(payload, dict) else None
    if not isinstance(targets, list):
        targets = None
    n = cabinet_service.copy_pricing_settings(db, info.id, targets)
    return {"copied": n}


@router.post("/refresh-all")
def api_refresh_all(payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """«Обновить всё»: по очереди запускает обновление всех связок по их
    маркетплейсу. Связки без ключей пропускаются (иначе провайдер возьмёт
    ключи по умолчанию из .env и запишет чужие данные в схему связки)."""
    user = _current_user(db)
    include_detail = bool(payload.get("include_detail"))
    jobs = []
    for c in cabinet_service.list_cabinets(db, user):
        if not c.enabled:
            continue
        info = cabinet_service.info_by_id(db, c.id)
        if info is None or not info.creds_for(info.marketplace):
            continue
        res = refresh_service.start_refresh(
            info.marketplace, include_detail=include_detail, cab_id=c.id)
        if res.get("job_id"):
            jobs.append(res)
    return {"jobs": jobs, "count": len(jobs)}