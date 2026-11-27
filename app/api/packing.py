# -*- coding: utf-8 -*-
"""T-101: Упаковочный лист (/api/packing/*)."""
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api._common import _parse_window400, get_db
from app.services import packing as packing_service

router = APIRouter()


@router.get("/packing/draft")
def api_packing_draft(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    target_days: int = Query(30, ge=1, le=365),
    wish: int = Query(0, ge=0),
    wish_step: str = Query(""),
    max_per_size: int = Query(0, ge=0),
    rehab_units: int = Query(0, ge=0),
    article_like: Optional[str] = Query(None),
    cabinet_codes: Optional[str] = Query(None, description="CSV кодов связок"),
    include_ozon: int = Query(0),
    db: Session = Depends(get_db),
):
    try:
        if not date_from or not date_to:
            raise HTTPException(status_code=400, detail="Укажите период: date_from и date_to")
        from_, to_ = _parse_window400(date_from, date_to)
        codes = [c.strip() for c in (cabinet_codes or "").split(",") if c.strip()] or None
        return packing_service.build_draft(
            db,
            date_from=from_,
            date_to=to_,
            target_days=target_days,
            wish=wish,
            wish_step=wish_step,
            max_per_size=max_per_size,
            rehab_units=rehab_units,
            article_like=article_like,
            cabinet_codes=codes,
            include_ozon=bool(include_ozon),
        )
    except packing_service.PackingError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/packing/commit")
def api_packing_commit(payload: dict = Body(...), db: Session = Depends(get_db)):
    try:
        draft = payload.get("draft") if isinstance(payload.get("draft"), dict) else payload
        return packing_service.commit_draft(db, draft)
    except packing_service.PackingError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/packing/history")
def api_packing_history(limit: int = Query(50, ge=1, le=200), db: Session = Depends(get_db)):
    return {"items": packing_service.list_packing_lists(db, limit=limit)}


@router.get("/packing/{packing_id}")
def api_packing_get(packing_id: int, db: Session = Depends(get_db)):
    try:
        return packing_service.get_packing_list(db, packing_id)
    except packing_service.PackingError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/packing/{packing_id}/revert")
def api_packing_revert(packing_id: int, db: Session = Depends(get_db)):
    try:
        return packing_service.revert_packing_list(db, packing_id)
    except packing_service.PackingError as e:
        raise HTTPException(status_code=400, detail=str(e))
