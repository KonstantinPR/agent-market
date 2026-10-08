# -*- coding: utf-8 -*-
"""Обзор и обслуживание: dashboard, pulls, sync, refresh, yandex."""
from app.api._common import *  # noqa: F401,F403
from app.api._common import (
    _parse_window400,
)

router = APIRouter()


@router.get("/progress")
async def api_progress(op: Optional[str] = None):
    """Прогресс текущей выкачки для зелёной строки состояния.

    Фронтенд шлёт op-id заголовком X-Progress-Id и опрашивает этот роут раз в
    секунду, пока висит синхронный запрос; пустая строка = прогресса нет
    (показываем прошедшее время). Без БД и threadpool — отвечает мгновенно
    даже при загруженных выкачками воркерах.
    """
    from app.services import progress as progress_service

    snap = progress_service.snapshot(op or "")
    return {"text": snap["text"] if snap else ""}



@router.get("/pulls")
def api_pulls(db: Session = Depends(get_db)):
    """Лог последних успешных загрузок из API маркетплейсов."""
    from sqlalchemy import desc

    rows = db.execute(
        select(
            models.ApiPull.api, models.ApiPull.kind,
            models.ApiPull.last_success_at, models.ApiPull.rows,
            models.ApiPull.db_rows, models.ApiPull.window,
        ).order_by(desc(models.ApiPull.last_success_at))
    ).all()
    return [
        {
            "api": api, "kind": kind,
            "last_success_at": last_success_at.isoformat(sep=" ") if last_success_at else None,
            "rows": rows_n, "db_rows": db_rows, "window": window,
        }
        for api, kind, last_success_at, rows_n, db_rows, window in rows
]


@router.get("/dashboard")
def api_dashboard(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    marketplace: Optional[str] = None,
    compare: int = 0,
    top: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """Обзор: KPI из детализаций + топы + дельты цены + склад + свежесть.

    marketplace: 'wb' | 'ozon' | 'wb,ozon' | 'all' (пусто = все).
    compare=1 добавляет сравнение маржи с предыдущим аналогичным окном.
    top — необязательный размер топа прибыли/убытка и дельт цены (пусто = весь
    список; фронтенд сам делает сортировку/пагинацию/итоги).
    Остаётся совместимым со старым ответом: per_marketplace/total/daily
    (агрегат по таблице Продажи, source != detail).
    """
    from_, to_ = _parse_window400(date_from, date_to)
    mp_param = marketplace or "all"

    prev = None
    if compare and from_ and to_:
        delta = (to_ - from_).days
        prev = (from_ - timedelta(days=delta), from_ - timedelta(days=1))

    query = (
        select(
            models.Marketplace.code.label("marketplace"),
            models.Sale.date,
            func.sum(models.Sale.quantity).label("sells"),
            func.sum(models.Sale.revenue).label("revenue"),
            func.sum(models.Sale.income).label("income"),
            func.sum(models.Sale.commission).label("commission"),
            func.sum(models.Sale.logistics).label("logistics"),
            func.sum(models.Sale.storage).label("storage"),
        )
        .select_from(models.Sale)
        .join(models.Marketplace, models.Sale.marketplace_id == models.Marketplace.id)
        .where(models.Sale.date >= from_, models.Sale.date <= to_,
               models.Sale.source != "detail")
        .group_by(models.Marketplace.code, models.Sale.date)
    )
    per_mp = {}
    total = {"sells": 0, "revenue": 0.0, "income": 0.0}
    daily = {}
    wanted = dashboard_service._wanted(mp_param)
    for r in db.execute(query):
        if r.marketplace not in wanted:
            continue
        per_mp.setdefault(r.marketplace, {
            "marketplace": r.marketplace,
            "sells": 0, "revenue": 0.0, "income": 0.0,
            "commission": 0.0, "logistics": 0.0, "storage": 0.0,
        })
        p = per_mp[r.marketplace]
        p["sells"] += int(r.sells or 0)
        p["revenue"] += float(r.revenue or 0)
        p["income"] += float(r.income or 0)
        p["commission"] += float(r.commission or 0)
        p["logistics"] += float(r.logistics or 0)
        p["storage"] += float(r.storage or 0)
        dk = str(r.date)
        daily.setdefault(dk, {"date": dk, "revenue": 0.0, "income": 0.0, "sells": 0, "profit": 0.0})
        daily[dk]["revenue"] += float(r.revenue or 0)
        daily[dk]["income"] += float(r.income or 0)
        daily[dk]["sells"] += int(r.sells or 0)
        # Операционная прибыль = доход минус комиссия/логистика/хранение.
        # Знак затрат в данных бывает разным (WB пишет «+», Ozon «−») — нормируем abs().
        daily[dk]["profit"] += (float(r.income or 0)
                                - abs(float(r.commission or 0))
                                - abs(float(r.logistics or 0))
                                - abs(float(r.storage or 0)))
    for d in daily.values():
        d["profit"] = round(d["profit"], 2)
    if per_mp:
        total["sells"] = sum(p["sells"] for p in per_mp.values())
        total["revenue"] = round(sum(p["revenue"] for p in per_mp.values()), 2)
        total["income"] = round(sum(p["income"] for p in per_mp.values()), 2)

    return {
        "per_marketplace": list(per_mp.values()),
        "total": total,
        "daily": sorted(daily.values(), key=lambda d: d["date"]),
        "kpis": dashboard_service.dashboard_kpis(db, from_, to_, prev, mp_param),
        "tops": {
            "profit": dashboard_service.top_products(
                db, from_, to_, mp_param, limit=top, kind="profit"),
            "loss": dashboard_service.top_products(
                db, from_, to_, mp_param, limit=top, kind="loss"),
        },
        "price": dashboard_service.price_delta(db, from_, to_, prev, mp_param, limit=top),
        "prefixes": dashboard_service.prefix_margin(
            db, from_, to_, mp_param, limit=top),
        "stocks": dashboard_service.stocks_summary(db, to_, mp_param),
        "freshness": dashboard_service.freshness(db),
        "date_from": str(from_),
        "date_to": str(to_),
    }


@router.post("/sync/{code}")
def api_sync(code: str, date_from: Optional[str] = None, date_to: Optional[str] = None):
    if code not in ("wb", "ozon"):
        return {"error": "unknown marketplace"}, 400
    from_, to_ = _parse_window400(date_from, date_to)
    return sync_service.sync_sales_window(code, from_, to_)


# ------------------------------------------------------- массовое обновление (кнопка)
@router.post("/refresh")
def api_refresh(api: str, detail: int = 0, date_from: Optional[str] = None,
                date_to: Optional[str] = None, cab_id: Optional[int] = None,
                db: Session = Depends(get_db)):
    """Запускает фоновое обновление маркетплейса (wb|ozon). Ошибки изолированы по видам.

    cab_id — кабинет, по умолчанию активный в запросе (кука agent_cabinet).
    """
    if api not in ("wb", "ozon"):
        raise HTTPException(status_code=400, detail='Параметр api должен быть "wb" или "ozon"')
    try:
        return refresh_service.start_refresh(api, include_detail=bool(detail),
                                             date_from=date_from, date_to=date_to,
                                             cab_id=cab_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Некорректная дата: {e}")


@router.get("/refresh")
def api_refresh_list():
    return {"jobs": refresh_service.active_jobs()}


@router.get("/refresh/history")
def api_refresh_history(limit: int = 10, db: Session = Depends(get_db)):
    return {"runs": refresh_service.history(db, limit=min(max(limit, 1), 50))}


@router.get("/refresh/{job_id}")
def api_refresh_state(job_id: int):
    state = refresh_service.job_state(job_id)
    if state is None:
        raise HTTPException(status_code=404, detail="Задание не найдено")
    return state


# ------------------------------------------------------- Яндекс.Диск (файлы отчётов)
@router.post("/yandex/upload")
async def yandex_upload(file: UploadFile = File(...), folder: str = "/agent_market"):
    try:
        token = yandex_service.require_token()
        data = await file.read()
        name = file.filename or "file.xlsx"
        return yandex_service.upload_bytes(token, folder, name, data)
    except yandex_service.YandexDiskError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/yandex/list")
def yandex_list(folder: str = "/agent_market"):
    try:
        token = yandex_service.require_token()
        return yandex_service.list_files(token, folder)
    except yandex_service.YandexDiskError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/yandex/download")
def yandex_download(path: str):
    try:
        token = yandex_service.require_token()
        data = yandex_service.download_bytes(token, path)
    except yandex_service.YandexDiskError as e:
        raise HTTPException(status_code=400, detail=str(e))
    name = path.rsplit("/", 1)[-1] or "file.xlsx"
    return StreamingResponse(
        BytesIO(data),
        media_type=XLSX_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.delete("/yandex/delete")
def yandex_delete(path: str):
    try:
        token = yandex_service.require_token()
        yandex_service.delete_file(token, path)
    except yandex_service.YandexDiskError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "path": path}
