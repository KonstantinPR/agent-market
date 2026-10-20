# -*- coding: utf-8 -*-
"""Массовые действия по связкам (/api/batch*).

Кнопка «Запустить» у связки в шапке: за один прогон — обновление данных,
пересчёт автопилота, отправка коррекции цен и шаблон мин. цены автоакций —
по выбранным связкам.
"""
from fastapi import Body, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api._common import *  # noqa: F401,F403
from app.database import get_db
from app.services import batch as batch_service
from app.services import cabinets as cabinet_service

router = APIRouter()

XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("/batch/actions")
def api_batch_actions():
    """Реестр массовых действий для меню (порядок, подписи, необратимость)."""
    return {"actions": batch_service.actions_public(), "scopes": list(batch_service.SCOPES)}


@router.post("/batch/preview")
def api_batch_preview(payload: dict = Body(default={}), db: Session = Depends(get_db)):
    """Предпросмотр: сколько изменений уйдёт по каждой связке (без записи)."""
    user = cabinet_service.seed_user(db)
    actions = payload.get("actions") if isinstance(payload, dict) else None
    scope = (payload.get("scope") if isinstance(payload, dict) else None) or "all"
    return batch_service.preview(db, user, actions, scope)


@router.post("/batch/run")
def api_batch_run(payload: dict = Body(default={})):
    """Запускает массовый прогон. Коррекция цен требует confirmed=true."""
    if not isinstance(payload, dict):
        payload = {}
    res = batch_service.run(
        payload.get("actions"),
        scope=payload.get("scope") or "all",
        include_detail=bool(payload.get("include_detail")),
        confirmed=bool(payload.get("confirmed")),
        date_from=payload.get("date_from"),
        date_to=payload.get("date_to"),
    )
    if res.get("needs_confirm"):
        raise HTTPException(
            status_code=409,
            detail="Отправка коррекции цен меняет цены в кабинете маркетплейса. "
                   "Подтвердите запуск (confirmed=true).",
        )
    if res.get("job_id") is None:
        raise HTTPException(status_code=400, detail=res.get("rejected") or "не запущено")
    return res


@router.get("/batch")
def api_batch_active():
    return {"jobs": batch_service.active_jobs()}


@router.get("/batch/{job_id}")
def api_batch_state(job_id: int):
    state = batch_service.job_state(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Задание не найдено")
    return state


@router.get("/batch/{job_id}/minpromo/{cab_id}")
def api_batch_minpromo(job_id: int, cab_id: int):
    """Скачать сгенерированный шаблон мин. цены автоакций по связке прогона."""
    path = batch_service.minpromo_path(job_id, cab_id)
    if path is None:
        raise HTTPException(status_code=404, detail="Шаблон не найден")
    return FileResponse(
        path, media_type=XLSX_MEDIA,
        filename=f"minpromo_batch{job_id}_{cab_id}.xlsx")
